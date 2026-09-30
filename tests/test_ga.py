import random
import unittest

from chaosevo.fitness import Result
from chaosevo.ga import GAConfig, GeneticAlgorithm
from chaosevo.genome import ActionSpec, Experiment, ParamSpec, SearchSpace

SERVICES = ["web", "api", "db"]
ACTIONS = [
    ActionSpec("pod_kill", SERVICES, [ParamSpec("count", 1, 3, integer=True)], cost=1.0),
    ActionSpec("delay", SERVICES, [ParamSpec("ms", 50, 1500)], min_duration=3, max_duration=15, cost=1.0),
]


def damage(exp: Experiment) -> Result:
    """Needs coordination: the worst thing is many db kills landing within 3 s of each other."""
    kills = [f for f in exp.faults if f.action == "pod_kill" and f.target == "db"]
    best = max((sum(g.param("count") for g in kills if abs(g.start - f.start) <= 3) for f in kills), default=0)
    return Result(float(best))


class GATests(unittest.TestCase):
    def space(self):
        return SearchSpace(ACTIONS, window=20, min_faults=1, max_faults=4)

    def test_ga_beats_random_search_with_the_same_budget(self):
        ga_scores, random_scores = [], []
        for seed in range(4):
            ga = GeneticAlgorithm(self.space(), damage, GAConfig(population=20, generations=20, seed=seed, cost_weight=0))
            res = ga.run()
            ga_scores.append(res.best.result.damage)
            rng, space = random.Random(seed + 100), self.space()
            random_scores.append(max(damage(space.random_experiment(rng)).damage for _ in range(res.evaluations)))
        self.assertGreater(sum(ga_scores) / 4, 1.3 * sum(random_scores) / 4)   # measured: ~9.6 vs ~5.4 (max 12)

    def test_best_never_gets_worse_and_elites_are_not_rerun(self):
        calls = []

        def counting(exp):
            calls.append(exp)
            return damage(exp)

        res = GeneticAlgorithm(self.space(), counting, GAConfig(population=8, generations=6, seed=1, elite=2)).run()
        bests = [h.best_fitness for h in res.history]
        self.assertEqual(bests, sorted(bests))                 # elitism => monotonic
        self.assertEqual(len(calls), len(set(calls)))           # no genome evaluated twice
        self.assertEqual(res.evaluations, len(calls))

    def test_same_seed_same_search(self):
        a = GeneticAlgorithm(self.space(), damage, GAConfig(seed=5)).run()
        b = GeneticAlgorithm(self.space(), damage, GAConfig(seed=5)).run()
        self.assertEqual(a.best.experiment, b.best.experiment)
        self.assertEqual([h.best_damage for h in a.history], [h.best_damage for h in b.history])

    def test_cost_penalty_prefers_smaller_experiments(self):
        flat = lambda exp: Result(5.0)                          # every experiment does the same damage
        res = GeneticAlgorithm(self.space(), flat, GAConfig(population=10, generations=8, seed=2, cost_weight=1.0)).run()
        self.assertLessEqual(len(res.best.experiment.faults), 2)

    def test_stagnation_stops_the_search(self):
        res = GeneticAlgorithm(self.space(), lambda e: Result(1.0), GAConfig(generations=50, stagnation=3, seed=1)).run()
        self.assertLess(len(res.history), 50)
        self.assertIn("no improvement", res.stopped_because)

    def test_a_crashing_evaluation_is_recorded_not_fatal(self):
        def flaky(exp):
            raise RuntimeError("cluster on fire")

        res = GeneticAlgorithm(self.space(), flaky, GAConfig(population=4, generations=2, seed=1)).run()
        self.assertIn("cluster on fire", res.best.result.error)
        self.assertEqual(res.best.result.damage, 0.0)

    def test_seed_experiments_join_the_first_generation(self):
        space = self.space()
        seed = space.random_experiment(random.Random(9))
        seen = []
        GeneticAlgorithm(space, lambda e: (seen.append(e), Result(0.0))[1], GAConfig(population=4, generations=1, seed=1),
                         seeds=[seed]).run()
        self.assertIn(space.repair(seed.faults, random.Random(0)), seen)

    def test_results_serialise(self):
        import json
        res = GeneticAlgorithm(self.space(), damage, GAConfig(population=4, generations=2, seed=1)).run()
        json.dumps(res.to_dict())
        self.assertEqual(len(res.hall_of_fame), min(5, res.evaluations))


if __name__ == "__main__":
    unittest.main()
