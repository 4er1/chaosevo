"""The genetic algorithm: evolve chaos experiments towards maximum damage."""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .fitness import Result
from .genome import Experiment, Fault, SearchSpace


@dataclass
class GAConfig:
    population: int = 8
    generations: int = 6
    elite: int = 2                 # best experiments copied unchanged into the next generation
    tournament: int = 3
    crossover_rate: float = 0.8
    mutation_rate: float = 0.3     # per fault
    structural_rate: float = 0.2   # chance of adding/removing a fault
    cost_weight: float = 0.3       # fitness = damage - cost_weight * blast_radius
    stagnation: Optional[int] = None   # stop after this many generations without improvement
    max_seconds: Optional[float] = None
    seed: Optional[int] = None

    def __post_init__(self) -> None:
        if self.population < 2 or self.elite >= self.population:
            raise ValueError("need population >= 2 and elite < population")


@dataclass
class Individual:
    experiment: Experiment
    result: Result
    fitness: float

    def to_dict(self) -> dict:
        return {"experiment": self.experiment.to_dict(), "description": self.experiment.describe(),
                "fitness": round(self.fitness, 4), **self.result.to_dict()}


@dataclass
class GenStats:
    generation: int
    best_fitness: float
    best_damage: float
    mean_fitness: float
    mean_damage: float
    evaluations: int          # cumulative number of experiments actually executed
    best: str


@dataclass
class GAResult:
    best: Individual
    hall_of_fame: List[Individual]
    history: List[GenStats]
    evaluations: int
    stopped_because: str
    config: GAConfig

    def to_dict(self) -> dict:
        return {"config": asdict(self.config), "evaluations": self.evaluations,
                "stopped_because": self.stopped_because, "best": self.best.to_dict(),
                "hall_of_fame": [i.to_dict() for i in self.hall_of_fame],
                "history": [asdict(h) for h in self.history]}


class GeneticAlgorithm:
    def __init__(self, space: SearchSpace, evaluate: Callable[[Experiment], Result],
                 config: Optional[GAConfig] = None, seeds: Sequence[Experiment] = (),
                 on_generation: Optional[Callable[[GenStats, List[Individual]], None]] = None,
                 on_evaluation: Optional[Callable[[Experiment, Result, float], None]] = None) -> None:
        self.space, self.evaluate = space, evaluate
        self.cfg = config or GAConfig()
        self.rng = random.Random(self.cfg.seed)
        self.seeds = list(seeds)
        self.on_generation, self.on_evaluation = on_generation, on_evaluation
        self._cache: Dict[Experiment, Individual] = {}
        self.history: List[GenStats] = []
        self.hall: Dict[Experiment, Individual] = {}

    # -- evaluation ----------------------------------------------------------------------
    def _individual(self, exp: Experiment) -> Individual:
        cached = self._cache.get(exp)
        if cached is not None:              # elites and repeated genomes are not re-run
            return cached
        started = time.monotonic()
        try:
            result = self.evaluate(exp)
        except Exception as exc:            # a broken run is not "damage"
            result = Result(0.0, error=f"{type(exc).__name__}: {exc}")
        result.seconds = result.seconds or time.monotonic() - started
        fitness = result.damage - self.cfg.cost_weight * self.space.cost(exp)
        ind = Individual(exp, result, fitness)
        self._cache[exp] = ind
        if self.on_evaluation:
            self.on_evaluation(exp, result, fitness)
        return ind

    # -- operators -----------------------------------------------------------------------
    def _select(self, scored: List[Individual]) -> Individual:
        k = min(self.cfg.tournament, len(scored))
        return max(self.rng.sample(scored, k), key=lambda i: i.fitness)

    def _crossover(self, a: Experiment, b: Experiment) -> Experiment:
        """Time-point crossover: keep A's faults before a random moment and B's faults after it."""
        cut = self.rng.uniform(0, self.space.max_start)
        child = [f for f in a.faults if f.start < cut] + [f for f in b.faults if f.start >= cut]
        return self.space.repair(child or list(a.faults), self.rng)

    def _mutate_fault(self, f: Fault) -> Fault:
        spec = self.space.spec(f.action)
        ops = ["start", "target", "action"]
        if not spec.instantaneous:
            ops.append("duration")
        if spec.params:
            ops.append("param")
        op = self.rng.choice(ops)
        if op == "start":
            f = Fault(f.action, f.target, f.start + self.rng.gauss(0, self.space.window * 0.15), f.duration, f.params)
        elif op == "duration":
            span = spec.max_duration - spec.min_duration
            f = Fault(f.action, f.target, f.start, f.duration + self.rng.gauss(0, span * 0.25), f.params)
        elif op == "target":
            f = Fault(f.action, self.rng.choice(list(spec.targets)), f.start, f.duration, f.params)
        elif op == "param":
            p = self.rng.choice(list(spec.params))
            params = dict(f.params)
            params[p.name] = params.get(p.name, p.low) + self.rng.gauss(0, (p.high - p.low) * 0.25)
            f = Fault(f.action, f.target, f.start, f.duration, tuple(sorted(params.items())))
        else:
            return self.space.random_fault(self.rng, start=f.start)
        return self.space.clamp_fault(f)

    def _mutate(self, exp: Experiment, boost: float = 1.0) -> Experiment:
        faults = list(exp.faults)
        for i, f in enumerate(faults):
            if self.rng.random() < min(1.0, self.cfg.mutation_rate * boost):
                faults[i] = self._mutate_fault(f)
        if self.rng.random() < min(1.0, self.cfg.structural_rate * boost):
            grow = len(faults) < self.space.max_faults and (
                len(faults) <= self.space.min_faults or self.rng.random() < 0.5)
            if grow:
                faults.append(self.space.random_fault(self.rng))
            elif len(faults) > self.space.min_faults:
                faults.pop(self.rng.randrange(len(faults)))
        return self.space.repair(faults, self.rng)

    # -- generations ---------------------------------------------------------------------
    def _initial(self) -> List[Experiment]:
        pop: List[Experiment] = []
        for s in self.seeds:
            e = self.space.repair(s.faults, self.rng)
            if e not in pop:
                pop.append(e)
        tries = 0
        while len(pop) < self.cfg.population and tries < 1000:
            tries += 1
            e = self.space.random_experiment(self.rng)
            if e not in pop:
                pop.append(e)
        return pop[: max(self.cfg.population, len(self.seeds))]

    def _next(self, scored: List[Individual]) -> List[Experiment]:
        nxt = [i.experiment for i in scored[: self.cfg.elite]]
        while len(nxt) < self.cfg.population:
            a, b = self._select(scored), self._select(scored)
            child = a.experiment
            if self.rng.random() < self.cfg.crossover_rate:
                child = self._crossover(a.experiment, b.experiment)
            child = self._mutate(child)
            boost = 1.0
            while (child in nxt or child in self._cache) and boost < 20:   # never re-run a known genome
                boost *= 1.5
                child = self._mutate(child, boost)
            if child in nxt or child in self._cache:
                child = self.space.random_experiment(self.rng)
            nxt.append(child)
        return nxt

    def run(self) -> GAResult:
        cfg, started = self.cfg, time.monotonic()
        population, best_so_far, since_improved, reason = self._initial(), float("-inf"), 0, "generations"
        for gen in range(cfg.generations):
            scored = sorted((self._individual(e) for e in population), key=lambda i: i.fitness, reverse=True)
            for ind in scored:
                self.hall[ind.experiment] = ind
            stats = GenStats(gen, scored[0].fitness, scored[0].result.damage,
                             sum(i.fitness for i in scored) / len(scored),
                             sum(i.result.damage for i in scored) / len(scored),
                             len(self._cache), scored[0].experiment.describe())
            self.history.append(stats)
            if self.on_generation:
                self.on_generation(stats, scored)
            if stats.best_fitness > best_so_far + 1e-9:
                best_so_far, since_improved = stats.best_fitness, 0
            else:
                since_improved += 1
            if cfg.stagnation and since_improved >= cfg.stagnation:
                reason = f"no improvement for {cfg.stagnation} generations"
                break
            if cfg.max_seconds and time.monotonic() - started > cfg.max_seconds:
                reason = "time budget"
                break
            if gen == cfg.generations - 1:
                break
            population = self._next(scored)
        ranked = sorted(self.hall.values(), key=lambda i: i.fitness, reverse=True)
        return GAResult(ranked[0], ranked[:5], self.history, len(self._cache), reason, cfg)
