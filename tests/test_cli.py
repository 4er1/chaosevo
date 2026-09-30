import contextlib
import io
import json
import os
import tempfile
import unittest

from chaosevo.cli import main


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(list(argv))
    return buf.getvalue()


FAST = {"target": {"type": "simulated"}, "experiment": {"window": 8, "recovery": 1}}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "fast.json")
        with open(self.cfg, "w") as fh:
            json.dump(FAST, fh)

    def test_run_report_and_replay_on_the_simulated_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "results.json")
            text = run("run", "--config", self.cfg, "--generations", "3", "--population", "6", "--seed", "4", "--out", out)
            self.assertIn("== generation 2", text)
            self.assertIn("# chaosevo report", text)
            with open(out) as fh:
                data = json.load(fh)
            self.assertEqual(data["target"], "simulated")
            self.assertEqual(len(data["ga"]["history"]), 3)
            self.assertGreater(data["ga"]["best"]["damage"], 0)

            report = run("report", out)
            self.assertIn("## Evolution", report)
            self.assertIn("Most damaging experiments", report)

            replay = run("replay", out, "--rank", "1")
            self.assertIn("damage now", replay)
            was = data["ga"]["hall_of_fame"][0]["damage"]
            now = float(replay.split("damage now ")[1].split()[0])
            self.assertAlmostEqual(now, was, delta=max(1.5, 0.35 * was))      # the simulator is reproducible

    def test_metrics_endpoint_during_a_run(self):
        text = run("run", "--config", self.cfg, "--generations", "2", "--population", "4", "--seed", "1",
                   "--metrics-port", "0")
        self.assertIn("metrics on http://127.0.0.1:", text)

    def test_actions_listing(self):
        self.assertIn("pod_kill", run("actions", "--target", "simulated"))

    def test_seed_experiments_from_config(self):
        cfg = os.path.join(self.tmp.name, "c.json")
        with open(cfg, "w") as fh:
            json.dump({**FAST, "ga": {"generations": 1, "population": 4, "seed": 2},
                       "seed_experiments": [{"faults": [{"action": "pod_kill", "target": "db", "start": 1,
                                                          "params": {"count": 2}}]}]}, fh)
        text = run("run", "--config", cfg)
        self.assertIn("pod_kill(db, count=2)", text)


if __name__ == "__main__":
    unittest.main()
