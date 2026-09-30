import random
import unittest

from chaosevo.genome import ActionSpec, Experiment, Fault, ParamSpec, SearchSpace

ACTIONS = [
    ActionSpec("kill", ["a", "b", "c"], [ParamSpec("count", 1, 3, integer=True)], cost=1.0),
    ActionSpec("delay", ["a", "b"], [ParamSpec("ms", 50, 500)], min_duration=2, max_duration=10, cost=2.0),
]


class GenomeTests(unittest.TestCase):
    def setUp(self):
        self.space = SearchSpace(ACTIONS, window=20, min_faults=1, max_faults=4)
        self.rng = random.Random(0)

    def test_random_experiments_are_always_legal(self):
        for _ in range(300):
            exp = self.space.random_experiment(self.rng)
            self.assertTrue(1 <= len(exp.faults) <= 4)
            self.assertEqual(list(exp.faults), sorted(exp.faults, key=lambda f: (f.start, f.action, f.target)))
            for f in exp.faults:
                spec = self.space.spec(f.action)
                self.assertIn(f.target, spec.targets)
                self.assertTrue(0 <= f.start <= self.space.max_start)
                if spec.instantaneous:
                    self.assertEqual(f.duration, 0)
                else:
                    self.assertTrue(spec.min_duration <= f.duration <= spec.max_duration)
                for p in spec.params:
                    self.assertTrue(p.low <= f.param(p.name) <= p.high)
                    if p.integer:
                        self.assertEqual(f.param(p.name), int(f.param(p.name)))

    def test_clamp_pulls_illegal_values_back(self):
        wild = Fault("delay", "zzz", start=999, duration=999, params=(("ms", 99999.0),))
        f = self.space.clamp_fault(wild)
        self.assertEqual((f.target, f.start, f.duration, f.param("ms")), ("a", 17.0, 10.0, 500.0))

    def test_missing_params_get_a_default(self):
        f = self.space.clamp_fault(Fault("kill", "a", 1.0))
        self.assertEqual(f.param("count"), 2.0)

    def test_repair_enforces_the_size_limits(self):
        many = [self.space.random_fault(self.rng) for _ in range(9)]
        self.assertEqual(len(self.space.repair(many, self.rng).faults), 4)
        self.assertEqual(len(self.space.repair([], self.rng).faults), 1)

    def test_json_roundtrip_and_hashability(self):
        exp = self.space.random_experiment(self.rng)
        again = Experiment.from_dict(exp.to_dict())
        self.assertEqual(exp, again)
        self.assertEqual(len({exp, again}), 1)

    def test_cost_prefers_short_and_cheap_faults(self):
        short = Fault("delay", "a", 0, 2, (("ms", 100.0),))
        long_ = Fault("delay", "a", 0, 10, (("ms", 100.0),))
        kill = Fault("kill", "a", 0, 0, (("count", 1.0),))
        self.assertLess(self.space.fault_cost(short), self.space.fault_cost(long_))
        self.assertLess(self.space.fault_cost(kill), self.space.fault_cost(short))
        self.assertAlmostEqual(self.space.cost(Experiment((short, kill))),
                               self.space.fault_cost(short) + 1.0)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self.space.spec("nope")
        with self.assertRaises(ValueError):
            SearchSpace([], 10)

    def test_describe_is_readable(self):
        f = Fault("delay", "a", 1.5, 4, (("ms", 200.0),))
        self.assertEqual(f.describe(), "t=1.5s delay(a, ms=200) for 4s")


if __name__ == "__main__":
    unittest.main()
