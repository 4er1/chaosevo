import math
import threading
import time
import unittest

from chaosevo.executor import Executor
from chaosevo.fitness import FitnessSpec
from chaosevo.genome import Experiment, Fault
from chaosevo.probes import HttpProbe, percentile
from chaosevo.sources import Signal
from chaosevo.targets.simulated import SimulatedTarget

FIT = FitnessSpec.from_dicts([{"name": "error_rate", "weight": 5, "scale": 0.5}])


class Recorder(SimulatedTarget):
    """Records (virtual time, event) so the schedule can be checked."""

    def __init__(self, fail_on=None):
        super().__init__(time_scale=0.02)
        self.events, self.fail_on = [], fail_on

    def reset(self):
        super().reset()
        self.events.append(("reset", 0))

    def inject(self, fault):
        if fault.target == self.fail_on:
            raise RuntimeError("cannot inject")
        self.events.append(("inject", fault.target, round(self._now())))
        super().inject(fault)

    def revert(self, fault):
        self.events.append(("revert", fault.target, round(self._now())))
        super().revert(fault)


def delay(target, start, duration, ms=500.0):
    return Fault("network_delay", target, start, duration, (("latency_ms", ms),))


def kill(target, start, count=1.0):
    return Fault("pod_kill", target, start, 0.0, (("count", count),))


def executor(target, **kw):
    return Executor(target, FIT, window=kw.pop("window", 10), recovery=kw.pop("recovery", 2),
                    sample_interval=1, time_scale=0.02, **kw)


class ExecutorTests(unittest.TestCase):
    def test_faults_run_on_schedule_and_timed_ones_are_reverted(self):
        t = Recorder()
        res = executor(t)(Experiment((delay("db", 2, 3), kill("web", 4), delay("api", 6, 20))))
        names = [(e[0], e[1]) for e in t.events]
        self.assertEqual(names, [("reset", 0), ("inject", "db"), ("inject", "web"), ("revert", "db"),
                                 ("inject", "api"), ("revert", "api")])
        db_inject = next(e for e in t.events if e[:2] == ("inject", "db"))
        db_revert = next(e for e in t.events if e[:2] == ("revert", "db"))
        self.assertAlmostEqual(db_revert[2] - db_inject[2], 3, delta=1)
        self.assertGreater(res.damage, 0)
        self.assertEqual(res.observations["_injection_errors"], 0)

    def test_an_overlong_fault_is_cut_at_the_end_of_the_window(self):
        t = Recorder()
        executor(t)(Experiment((delay("api", 6, 20),)))
        revert = next(e for e in t.events if e[0] == "revert")
        self.assertLessEqual(revert[2], 11)             # window is 10 virtual seconds

    def test_failed_injection_is_counted_and_others_still_run(self):
        t = Recorder(fail_on="db")
        res = executor(t)(Experiment((kill("db", 1), kill("web", 3, 3.0))))
        self.assertEqual(res.observations["_injection_errors"], 1)
        self.assertIn(("inject", "web", 3), [tuple(e) for e in t.events])
        self.assertGreater(res.damage, 0)

    def test_a_worse_experiment_scores_higher(self):
        mild = executor(Recorder())(Experiment((kill("web", 2, 1.0),)))
        harsh = executor(Recorder())(Experiment((kill("web", 2, 3.0), kill("db", 2, 2.0))))
        self.assertGreater(harsh.damage, mild.damage)

    def test_signals_are_sampled_and_aggregated(self):
        class Source:
            def __init__(self): self.calls = 0
            def query(self, q):
                self.calls += 1
                return 2.0

        src = Source()
        res = executor(Recorder(), signals=[Signal("burn", "anything", src, "integral")])(Experiment((kill("web", 1),)))
        self.assertGreater(src.calls, 3)
        self.assertGreater(res.observations["burn"], 10)      # value 2 held for ~12 virtual seconds

    def test_failing_signal_does_not_break_the_run(self):
        class Broken:
            def query(self, q): raise OSError("prometheus unreachable")

        res = executor(Recorder(), signals=[Signal("x", "q", Broken())])(Experiment((kill("web", 1),)))
        self.assertGreater(res.observations["_sample_errors"], 0)
        self.assertNotIn("x", res.observations)

    def test_faults_are_reverted_even_if_reset_of_probes_blow_up(self):
        t = Recorder()

        def boom():
            raise RuntimeError("probe failed to start")

        t.start_probes = boom
        with self.assertRaises(RuntimeError):
            executor(t)(Experiment((delay("db", 1, 5),)))
        self.assertFalse(t._timed)

    def test_probes_and_final_check_feed_the_observations(self):
        class P:
            def stop(self): return {"error_rate": 0.4}

        t = Recorder()
        t.start_probes = lambda: P()
        t.final_check = lambda: {"data_loss": 1.0}
        res = executor(t)(Experiment((kill("web", 1),)))
        self.assertEqual(res.observations["data_loss"], 1.0)
        self.assertEqual(res.observations["error_rate"], 0.4)      # probe value replaces the sampled aggregate


class ProbeTests(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 99), 10)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2)
        self.assertTrue(math.isnan(percentile([], 99)))

    def test_http_probe_measures_errors(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        flip = {"n": 0}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                flip["n"] += 1
                self.send_response(200 if flip["n"] % 2 else 500)
                self.send_header("Content-Length", "0"); self.end_headers()

            def log_message(self, *a): pass

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            probe = HttpProbe(f"http://127.0.0.1:{server.server_port}/", interval=0.02, timeout=1)
            time.sleep(0.5)
            out = probe.stop()
        finally:
            server.shutdown(); server.server_close()
        self.assertGreater(out["requests"], 5)
        self.assertAlmostEqual(out["error_rate"], 0.5, delta=0.15)

    def test_http_probe_counts_unreachable_as_errors(self):
        probe = HttpProbe("http://127.0.0.1:1/", interval=0.02, timeout=0.5)
        time.sleep(0.3)
        self.assertEqual(probe.stop()["error_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
