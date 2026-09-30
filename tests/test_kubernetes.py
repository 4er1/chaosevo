import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chaosevo.genome import Fault
from chaosevo.targets.kubernetes import KubeApi, KubernetesTarget


class FakeCluster:
    """Just enough of the Kubernetes API for the target: deployments, pods, scale, Chaos Mesh CRs."""

    def __init__(self):
        self.replicas = {"web": 3}
        self.pods = {"web": [f"web-{i}" for i in range(3)]}
        self.crs = {}                     # (plural, name) -> body
        self.calls, self.auth = [], set()
        self.not_ready_polls = 0

        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _route(self, method):
                url = urlparse(self.path)
                outer.auth.add(self.headers.get("Authorization"))
                parts = url.path.strip("/").split("/")
                body = self._body()
                outer.calls.append((method, url.path, body))
                if parts[:3] == ["apis", "apps", "v1"] and parts[-2] == "deployments" and method == "GET":
                    name, ready = parts[-1], outer.replicas[parts[-1]]
                    if outer.not_ready_polls > 0:
                        outer.not_ready_polls -= 1
                        ready = 0
                    return self._send({"spec": {"replicas": outer.replicas[name], "selector": {"matchLabels": {"app": name}}},
                                       "status": {"readyReplicas": ready, "replicas": outer.replicas[name]}})
                if parts[-1] == "scale" and method == "PATCH":
                    outer.replicas[parts[-2]] = body["spec"]["replicas"]
                    return self._send({})
                if parts[:2] == ["api", "v1"] and parts[-1] == "pods" and method == "GET":
                    app = parse_qs(url.query)["labelSelector"][0].split("=")[1]
                    return self._send({"items": [{"metadata": {"name": n}} for n in outer.pods[app]]})
                if parts[:2] == ["api", "v1"] and parts[-2] == "pods" and method == "DELETE":
                    for lst in outer.pods.values():
                        if parts[-1] in lst:
                            lst.remove(parts[-1])
                    return self._send({})
                if parts[0] == "apis" and parts[1] == "chaos-mesh.org":
                    plural = parts[5]
                    if method == "POST":
                        outer.crs[(plural, body["metadata"]["name"])] = body
                        return self._send(body, 201)
                    if method == "DELETE":
                        outer.crs.pop((plural, parts[6]), None)
                        return self._send({})
                    if method == "GET":
                        return self._send({"items": [{"metadata": {"name": n}} for (p, n) in outer.crs if p == plural]})
                self._send({"message": "not found"}, 404)

            do_GET = lambda self: self._route("GET")
            do_POST = lambda self: self._route("POST")
            do_PATCH = lambda self: self._route("PATCH")
            do_DELETE = lambda self: self._route("DELETE")

            def log_message(self, *a): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown(); self.server.server_close()


class KubernetesTargetTests(unittest.TestCase):
    def setUp(self):
        self.k = FakeCluster()
        self.addCleanup(self.k.stop)
        self.target = KubernetesTarget("demo", ["web"], KubeApi(self.k.url, token="s3cr3t"), chaos_mesh=True,
                                       reset_timeout=5, settle=0)
        self.target.setup()

    def test_setup_learns_replicas_and_selector_and_authenticates(self):
        self.assertEqual(self.target.baseline, {"web": 3})
        self.assertEqual(self.target.selectors, {"web": "app=web"})
        self.assertEqual(self.k.auth, {"Bearer s3cr3t"})

    def test_actions_depend_on_chaos_mesh(self):
        names = {a.name for a in self.target.actions()}
        self.assertEqual(names, {"pod_kill", "pod_kill_loop", "scale_down", "network_delay", "network_loss", "cpu_stress"})
        plain = KubernetesTarget("demo", ["web"], KubeApi(self.k.url))
        self.assertEqual({a.name for a in plain.actions()}, {"pod_kill", "pod_kill_loop", "scale_down"})

    def test_pod_kill_deletes_the_requested_number_of_pods(self):
        self.target.inject(Fault("pod_kill", "web", 0, 0, (("count", 2.0),)))
        self.assertEqual(len(self.k.pods["web"]), 1)
        deletes = [c for c in self.k.calls if c[0] == "DELETE"]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(deletes[0][2]["gracePeriodSeconds"], 0)

    def test_scale_down_and_revert(self):
        f = Fault("scale_down", "web", 0, 10, (("remove", 2.0),))
        self.target.inject(f)
        self.assertEqual(self.k.replicas["web"], 1)
        self.target.revert(f)
        self.assertEqual(self.k.replicas["web"], 3)
        patch = [c for c in self.k.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[1], "/apis/apps/v1/namespaces/demo/deployments/web/scale")

    def test_network_delay_creates_a_chaos_mesh_resource_and_revert_deletes_it(self):
        f = Fault("network_delay", "web", 0, 12, (("latency_ms", 500.0),))
        self.target.inject(f)
        (plural, name), body = next(iter(self.k.crs.items()))
        self.assertEqual(plural, "networkchaos")
        self.assertEqual(body["kind"], "NetworkChaos")
        self.assertEqual(body["spec"]["action"], "delay")
        self.assertEqual(body["spec"]["delay"]["latency"], "500ms")
        self.assertEqual(body["spec"]["duration"], "12s")
        self.assertEqual(body["spec"]["selector"], {"namespaces": ["demo"], "labelSelectors": {"app": "web"}})
        self.assertEqual(body["metadata"]["labels"]["app.kubernetes.io/managed-by"], "chaosevo")
        self.target.revert(f)
        self.assertEqual(self.k.crs, {})

    def test_stress_and_loss_resources(self):
        self.target.inject(Fault("cpu_stress", "web", 0, 8, (("load", 90.0), ("workers", 2.0))))
        self.target.inject(Fault("network_loss", "web", 0, 8, (("loss_pct", 30.0),)))
        kinds = {k[0]: b for k, b in self.k.crs.items()}
        self.assertEqual(kinds["stresschaos"]["spec"]["stressors"], {"cpu": {"workers": 2, "load": 90}})
        self.assertEqual(kinds["networkchaos"]["spec"]["loss"]["loss"], "30")

    def test_pod_kill_loop_keeps_killing_until_reverted(self):
        self.k.pods["web"] = [f"web-{i}" for i in range(10)]
        f = Fault("pod_kill_loop", "web", 0, 5, (("interval", 1.0),))
        self.target.inject(f)
        import time; time.sleep(0.2)
        self.target.revert(f)
        self.assertLess(len(self.k.pods["web"]), 10)
        left = len(self.k.pods["web"])
        time.sleep(1.3)
        self.assertEqual(len(self.k.pods["web"]), left)          # loop really stopped

    def test_reset_restores_replicas_cleans_leftovers_and_waits_for_readiness(self):
        self.target.inject(Fault("scale_down", "web", 0, 10, (("remove", 2.0),)))
        self.target.inject(Fault("network_delay", "web", 0, 10, (("latency_ms", 100.0),)))
        self.k.not_ready_polls = 2
        self.target.reset()
        self.assertEqual(self.k.replicas["web"], 3)
        self.assertEqual(self.k.crs, {})
        self.assertEqual(self.k.not_ready_polls, 0)

    def test_api_errors_surface(self):
        with self.assertRaises(RuntimeError):
            self.target.api.request("GET", "/nope")

    def test_needs_at_least_one_deployment(self):
        with self.assertRaises(ValueError):
            KubernetesTarget("demo", [], KubeApi(self.k.url))


if __name__ == "__main__":
    unittest.main()
