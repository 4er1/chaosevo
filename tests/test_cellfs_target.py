"""Runs a real experiment against a real cellfs cluster (5 processes). Skipped if cellfs is not importable."""
import importlib.util
import unittest

from chaosevo.executor import Executor
from chaosevo.fitness import FitnessSpec
from chaosevo.genome import Experiment, Fault

HAS_CELLFS = importlib.util.find_spec("cellfs") is not None


@unittest.skipUnless(HAS_CELLFS, "install or add the cellfs project to PYTHONPATH")
class CellfsTargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from chaosevo.cli import DEFAULTS
        from chaosevo.targets.cellfs_target import CellfsTarget
        cls.target = CellfsTarget(base_port=9860)
        cls.target.setup()
        e = DEFAULTS["cellfs"]["experiment"]
        cls.executor = Executor(cls.target, FitnessSpec.from_dicts(DEFAULTS["cellfs"]["objectives"]),
                                window=6, recovery=4, sample_interval=0.5)

    @classmethod
    def tearDownClass(cls):
        cls.target.close()

    def test_actions_cover_crash_freeze_disk_loss_and_bit_rot(self):
        names = {a.name for a in self.target.actions()}
        self.assertEqual(names, {"kill_node", "pause_node", "wipe_node", "corrupt_chunk"})

    def test_a_single_corrupted_copy_is_repaired_by_the_guardian(self):
        res = self.executor(Experiment((Fault("corrupt_chunk", "n1", 5.0, 0.0, (("chunk_idx", 0.0),)),)))
        # a single corrupted copy is repaired by the guardian: nothing is lost
        self.assertEqual(res.observations["data_loss"], 0.0)
        self.assertEqual(res.observations["lost_chunk_fraction"], 0.0)

    def test_a_crash_is_visible_in_the_measurements_but_cellfs_survives_it(self):
        res = self.executor(Experiment((Fault("kill_node", "n2", 1.0, 3.0),)))
        self.assertGreaterEqual(res.observations["replica_margin_lost"], 1 / 3 - 1e-9)   # copies were missing for a while
        self.assertEqual(res.observations["at_risk_fraction"], 0.0)                      # never down to a last copy
        self.assertEqual(res.observations["data_loss"], 0.0)                             # ...and nothing was lost
        self.assertGreater(res.observations["requests"], 5)                              # the workload really ran
        self.assertEqual(res.observations["_injection_errors"], 0.0)
        # the target was restored to a clean steady state for the next experiment
        self.assertEqual(len(self.target.cluster.running()), 5)


if __name__ == "__main__":
    unittest.main()
