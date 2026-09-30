"""Chaos target for Kubernetes Deployments, using only the Kubernetes REST API (no kubectl needed).

Faults (targets are Deployment names):
  pod_kill        delete `count` pods (the Deployment controller recreates them)
  pod_kill_loop   delete one pod every `interval` seconds for `duration` seconds (crash loop)
  scale_down      remove `remove` replicas for `duration` seconds, then restore them
  network_delay / network_loss / cpu_stress   via Chaos Mesh CRDs (only when `chaos_mesh` is true)

Runs in-cluster (ServiceAccount token) or against `api_server` (e.g. `kubectl proxy`). The RBAC it
needs is in ansible/roles/chaosevo/templates/rbac.yml.j2.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

from ..genome import ActionSpec, Fault, ParamSpec
from ..probes import HttpProbe
from ..target import Probes, Target

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
CHAOS_MESH = ("chaos-mesh.org", "v1alpha1")
LABEL = {"app.kubernetes.io/managed-by": "chaosevo"}


class KubeApi:
    def __init__(self, server: str, token: Optional[str] = None, ca_file: Optional[str] = None,
                 insecure: bool = False, timeout: float = 10.0) -> None:
        self.server, self.token, self.timeout = server.rstrip("/"), token, timeout
        self.ctx = None
        if server.startswith("https"):
            self.ctx = ssl.create_default_context(cafile=ca_file)
            if insecure:
                self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    @classmethod
    def in_cluster(cls) -> "KubeApi":
        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        with open(f"{SA_DIR}/token") as fh:
            token = fh.read().strip()
        return cls(f"https://{host}:{port}", token, f"{SA_DIR}/ca.crt")

    def request(self, method: str, path: str, body: Optional[dict] = None,
                content_type: str = "application/json") -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        return json.loads(raw) if raw else {}

    # convenience wrappers ---------------------------------------------------------------
    def get_deployment(self, ns: str, name: str) -> dict:
        return self.request("GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")

    def list_pods(self, ns: str, selector: str) -> List[dict]:
        q = urllib.parse.urlencode({"labelSelector": selector})
        return self.request("GET", f"/api/v1/namespaces/{ns}/pods?{q}").get("items", [])

    def delete_pod(self, ns: str, name: str) -> None:
        self.request("DELETE", f"/api/v1/namespaces/{ns}/pods/{name}",
                     {"kind": "DeleteOptions", "apiVersion": "v1", "gracePeriodSeconds": 0})

    def set_replicas(self, ns: str, name: str, replicas: int) -> None:
        self.request("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale",
                     {"spec": {"replicas": int(replicas)}}, "application/merge-patch+json")

    def custom(self, method: str, ns: str, plural: str, body: Optional[dict] = None,
               name: str = "", query: str = "") -> dict:
        group, version = CHAOS_MESH
        path = f"/apis/{group}/{version}/namespaces/{ns}/{plural}" + (f"/{name}" if name else "") + query
        return self.request(method, path, body)


class KubernetesTarget(Target):
    name = "kubernetes"

    def __init__(self, namespace: str, deployments: List[str], api: Optional[KubeApi] = None,
                 chaos_mesh: bool = False, reset_timeout: float = 120.0, settle: float = 5.0,
                 http_probe: Optional[dict] = None) -> None:
        if not deployments:
            raise ValueError("list at least one deployment to attack")
        self.ns, self.deployments = namespace, list(deployments)
        self.api = api or KubeApi.in_cluster()
        self.chaos_mesh, self.reset_timeout, self.settle = chaos_mesh, reset_timeout, settle
        self.http_probe = http_probe
        self.baseline: Dict[str, int] = {}
        self.selectors: Dict[str, str] = {}
        self._loops: Dict[Fault, tuple] = {}
        self._crs: Dict[Fault, tuple] = {}     # fault -> (plural, name)
        self._rng = random.Random()

    # -- lifecycle -----------------------------------------------------------------------
    def setup(self) -> None:
        for d in self.deployments:
            dep = self.api.get_deployment(self.ns, d)
            self.baseline[d] = int(dep["spec"]["replicas"])
            labels = dep["spec"]["selector"]["matchLabels"]
            self.selectors[d] = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def _cleanup_chaos(self) -> None:
        for fault in list(self._loops):
            self._stop_loop(fault)
        for fault in list(self._crs):
            self.revert(fault)
        if self.chaos_mesh:
            sel = ",".join(f"{k}={v}" for k, v in LABEL.items())
            for plural in ("networkchaos", "stresschaos"):
                try:
                    for item in self.api.custom("GET", self.ns, plural, query="?labelSelector=" + urllib.parse.quote(sel)).get("items", []):
                        self.api.custom("DELETE", self.ns, plural, name=item["metadata"]["name"])
                except RuntimeError:
                    pass

    def reset(self) -> None:
        self._cleanup_chaos()
        for d, n in self.baseline.items():
            self.api.set_replicas(self.ns, d, n)
        deadline = time.monotonic() + self.reset_timeout
        while True:
            healthy = True
            for d, n in self.baseline.items():
                status = self.api.get_deployment(self.ns, d).get("status", {})
                if int(status.get("readyReplicas", 0)) != n or int(status.get("replicas", n)) != n:
                    healthy = False
            if healthy:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("deployments did not return to a healthy state in time")
            time.sleep(1.0)
        time.sleep(self.settle)

    def close(self) -> None:
        try:
            self._cleanup_chaos()
            for d, n in self.baseline.items():
                self.api.set_replicas(self.ns, d, n)
        except Exception:
            pass

    # -- chaos ---------------------------------------------------------------------------
    def actions(self) -> List[ActionSpec]:
        deps = self.deployments
        acts = [
            ActionSpec("pod_kill", deps, [ParamSpec("count", 1, 3, integer=True)], cost=1.0,
                       description="delete pods; the controller recreates them"),
            ActionSpec("pod_kill_loop", deps, [ParamSpec("interval", 1, 10)], min_duration=5, max_duration=30,
                       cost=2.0, description="keep deleting a pod every `interval` seconds"),
            ActionSpec("scale_down", deps, [ParamSpec("remove", 1, 3, integer=True)], min_duration=5,
                       max_duration=30, cost=2.0, description="temporarily remove replicas"),
        ]
        if self.chaos_mesh:
            acts += [
                ActionSpec("network_delay", deps, [ParamSpec("latency_ms", 50, 2000)], min_duration=5,
                           max_duration=30, cost=1.5, description="Chaos Mesh NetworkChaos delay"),
                ActionSpec("network_loss", deps, [ParamSpec("loss_pct", 5, 90)], min_duration=5,
                           max_duration=30, cost=1.5, description="Chaos Mesh NetworkChaos packet loss"),
                ActionSpec("cpu_stress", deps, [ParamSpec("load", 50, 100), ParamSpec("workers", 1, 4, integer=True)],
                           min_duration=5, max_duration=30, cost=1.5, description="Chaos Mesh StressChaos"),
            ]
        return acts

    def _kill_pods(self, deployment: str, count: int) -> None:
        pods = [p for p in self.api.list_pods(self.ns, self.selectors[deployment])
                if not p["metadata"].get("deletionTimestamp")]
        self._rng.shuffle(pods)
        for pod in pods[:count]:
            self.api.delete_pod(self.ns, pod["metadata"]["name"])

    def _stop_loop(self, fault: Fault) -> None:
        entry = self._loops.pop(fault, None)
        if entry:
            entry[0].set()
            entry[1].join(timeout=5)

    def _cr(self, fault: Fault) -> tuple:
        dep, spec = fault.target, {"mode": "all", "duration": f"{int(fault.duration)}s",
                                   "selector": {"namespaces": [self.ns], "labelSelectors": {}}}
        for kv in self.selectors[dep].split(","):
            k, v = kv.split("=", 1)
            spec["selector"]["labelSelectors"][k] = v
        if fault.action == "network_delay":
            plural, kind = "networkchaos", "NetworkChaos"
            spec.update(action="delay", delay={"latency": f"{int(fault.param('latency_ms'))}ms", "jitter": "0ms", "correlation": "0"})
        elif fault.action == "network_loss":
            plural, kind = "networkchaos", "NetworkChaos"
            spec.update(action="loss", loss={"loss": str(int(fault.param("loss_pct"))), "correlation": "0"})
        else:
            plural, kind = "stresschaos", "StressChaos"
            spec["stressors"] = {"cpu": {"workers": int(fault.param("workers")), "load": int(fault.param("load"))}}
        name = "chaosevo-" + hashlib.sha1(repr(fault).encode()).hexdigest()[:10]
        body = {"apiVersion": "%s/%s" % CHAOS_MESH, "kind": kind,
                "metadata": {"name": name, "namespace": self.ns, "labels": dict(LABEL)}, "spec": spec}
        return plural, name, body

    def inject(self, fault: Fault) -> None:
        dep = fault.target
        if fault.action == "pod_kill":
            self._kill_pods(dep, int(fault.param("count", 1)))
        elif fault.action == "pod_kill_loop":
            stop = threading.Event()

            def loop() -> None:
                while not stop.is_set():
                    try:
                        self._kill_pods(dep, 1)
                    except Exception:
                        pass
                    stop.wait(fault.param("interval", 3))
            thread = threading.Thread(target=loop, daemon=True)
            self._loops[fault] = (stop, thread)
            thread.start()
        elif fault.action == "scale_down":
            self.api.set_replicas(self.ns, dep, max(0, self.baseline[dep] - int(fault.param("remove", 1))))
        elif fault.action in ("network_delay", "network_loss", "cpu_stress"):
            plural, name, body = self._cr(fault)
            self.api.custom("POST", self.ns, plural, body)
            self._crs[fault] = (plural, name)
        else:
            raise ValueError(f"unknown action {fault.action}")

    def revert(self, fault: Fault) -> None:
        if fault.action == "pod_kill_loop":
            self._stop_loop(fault)
        elif fault.action == "scale_down":
            self.api.set_replicas(self.ns, fault.target, self.baseline[fault.target])
        elif fault in self._crs:
            plural, name = self._crs.pop(fault)
            try:
                self.api.custom("DELETE", self.ns, plural, name=name)
            except RuntimeError:
                pass                                   # already expired on its own

    def start_probes(self) -> Optional[Probes]:
        if not self.http_probe:
            return None
        return HttpProbe(self.http_probe["url"], float(self.http_probe.get("interval", 0.5)),
                         float(self.http_probe.get("timeout", 2.0)))
