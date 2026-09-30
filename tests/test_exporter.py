import json
import unittest
import urllib.error
import urllib.request

from chaosevo.exporter import MetricsServer
from chaosevo.fitness import Result
from chaosevo.ga import GenStats
from chaosevo.genome import Experiment, Fault
from chaosevo.sources import parse_prometheus_text


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.read().decode()


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.srv = MetricsServer(0, "127.0.0.1", target="simulated", results=lambda: {"hello": "world"})
        self.addCleanup(self.srv.close)
        self.exp = Experiment((Fault("pod_kill", "db", 1.0, 0.0, (("count", 2.0),)),))

    def metrics(self):
        return {(s.name, tuple(sorted(s.labels.items()))): s.value for s in parse_prometheus_text(get(self.srv.port, "/metrics"))}

    def test_tracks_the_evolution(self):
        self.srv.on_evaluation(self.exp, Result(2.0, {"error_rate": 2.0}), 1.7)
        self.srv.on_evaluation(self.exp, Result(1.0, {"error_rate": 1.0}), 0.7)     # worse: best must not drop
        self.srv.on_evaluation(self.exp, Result(5.0, {"error_rate": 4.0, "p99_latency_s": 1.0}), 4.5)
        self.srv.on_generation(GenStats(3, 4.5, 5.0, 2.0, 2.7, 3, "x"), [])
        m, t = self.metrics(), ("target", "simulated")
        self.assertEqual(m[("chaosevo_evaluations_total", (t,))], 3)
        self.assertEqual(m[("chaosevo_best_damage", (t,))], 5.0)
        self.assertEqual(m[("chaosevo_experiment_damage", (t,))], 5.0)
        self.assertEqual(m[("chaosevo_generation", (t,))], 3)
        self.assertEqual(m[("chaosevo_generation_mean_damage", (t,))], 2.7)
        self.assertEqual(m[("chaosevo_best_damage_component", (("objective", "p99_latency_s"), t))], 1.0)
        self.assertEqual(m[("chaosevo_running", (t,))], 1)
        self.srv.finish()
        self.assertEqual(self.metrics()[("chaosevo_running", (t,))], 0)

    def test_health_results_and_404(self):
        self.assertEqual(get(self.srv.port, "/healthz").strip(), "ok")
        self.assertEqual(json.loads(get(self.srv.port, "/results")), {"hello": "world"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get(self.srv.port, "/nope")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
