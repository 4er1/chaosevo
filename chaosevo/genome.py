"""Chaos experiments as genomes.

An *experiment* is a small schedule of *faults* (genes): "at t=3s kill n2 for 5s", "at t=4s add
400ms latency to web for 10s"... A target declares which actions exist, on which targets and with
which parameter ranges (its `ActionSpec`s); the `SearchSpace` built from them can draw random
experiments and keep any mutated/crossed experiment inside the legal ranges.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ParamSpec:
    name: str
    low: float
    high: float
    integer: bool = False

    def clamp(self, value: float) -> float:
        value = min(max(float(value), self.low), self.high)
        return float(round(value)) if self.integer else round(value, 3)

    def sample(self, rng: random.Random) -> float:
        if self.integer:
            return float(rng.randint(int(self.low), int(self.high)))
        return self.clamp(rng.uniform(self.low, self.high))


@dataclass(frozen=True)
class ActionSpec:
    name: str
    targets: Sequence[str]
    params: Sequence[ParamSpec] = ()
    min_duration: float = 0.0
    max_duration: float = 0.0      # 0 => instantaneous (kill, corrupt...)
    cost: float = 1.0              # "blast radius": how expensive/risky this fault is to inject
    description: str = ""

    @property
    def instantaneous(self) -> bool:
        return self.max_duration <= 0


@dataclass(frozen=True)
class Fault:
    action: str
    target: str
    start: float
    duration: float = 0.0
    params: Tuple[Tuple[str, float], ...] = ()

    def param(self, name: str, default: Optional[float] = None) -> Optional[float]:
        return dict(self.params).get(name, default)

    def to_dict(self) -> dict:
        return {"action": self.action, "target": self.target, "start": self.start,
                "duration": self.duration, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "Fault":
        params = tuple(sorted((str(k), float(v)) for k, v in d.get("params", {}).items()))
        return cls(str(d["action"]), str(d["target"]), float(d["start"]), float(d.get("duration", 0.0)), params)

    def describe(self) -> str:
        extra = "".join(f", {k}={v:g}" for k, v in self.params)
        span = f" for {self.duration:g}s" if self.duration else ""
        return f"t={self.start:g}s {self.action}({self.target}{extra}){span}"


@dataclass(frozen=True)
class Experiment:
    faults: Tuple[Fault, ...]

    def to_dict(self) -> dict:
        return {"faults": [f.to_dict() for f in self.faults]}

    @classmethod
    def from_dict(cls, d: dict) -> "Experiment":
        return cls(tuple(Fault.from_dict(f) for f in d["faults"]))

    def describe(self) -> str:
        return "; ".join(f.describe() for f in self.faults) or "(no faults)"


class SearchSpace:
    def __init__(self, actions: Sequence[ActionSpec], window: float,
                 min_faults: int = 1, max_faults: int = 4) -> None:
        if not actions:
            raise ValueError("the target exposes no chaos actions")
        if min_faults < 1 or max_faults < min_faults:
            raise ValueError("need 1 <= min_faults <= max_faults")
        self.actions = list(actions)
        self.by_name: Dict[str, ActionSpec] = {a.name: a for a in actions}
        self.window = float(window)
        self.min_faults, self.max_faults = min_faults, max_faults
        self.max_start = round(self.window * 0.85, 1)

    def spec(self, action: str) -> ActionSpec:
        try:
            return self.by_name[action]
        except KeyError:
            raise ValueError(f"unknown action {action!r}; known: {sorted(self.by_name)}") from None

    # -- sampling ------------------------------------------------------------------------
    def random_fault(self, rng: random.Random, action: Optional[str] = None,
                     start: Optional[float] = None) -> Fault:
        spec = self.spec(action) if action else rng.choice(self.actions)
        duration = 0.0 if spec.instantaneous else round(rng.uniform(spec.min_duration, spec.max_duration), 1)
        params = tuple((p.name, p.sample(rng)) for p in spec.params)
        when = round(rng.uniform(0, self.max_start), 1) if start is None else start
        return self.clamp_fault(Fault(spec.name, rng.choice(list(spec.targets)), when, duration, params))

    def random_experiment(self, rng: random.Random) -> Experiment:
        n = rng.randint(self.min_faults, self.max_faults)
        return self.repair([self.random_fault(rng) for _ in range(n)], rng)

    # -- keeping things legal ------------------------------------------------------------
    def clamp_fault(self, fault: Fault) -> Fault:
        spec = self.spec(fault.action)
        target = fault.target if fault.target in spec.targets else sorted(spec.targets)[0]
        start = round(min(max(fault.start, 0.0), self.max_start), 1)
        duration = 0.0 if spec.instantaneous else round(
            min(max(fault.duration, spec.min_duration), spec.max_duration), 1)
        given = dict(fault.params)
        params = tuple((p.name, p.clamp(given.get(p.name, (p.low + p.high) / 2))) for p in spec.params)
        return Fault(spec.name, target, start, duration, params)

    def repair(self, faults: Iterable[Fault], rng: random.Random) -> Experiment:
        legal = [self.clamp_fault(f) for f in faults]
        while len(legal) > self.max_faults:
            legal.pop(rng.randrange(len(legal)))
        while len(legal) < self.min_faults:
            legal.append(self.random_fault(rng))
        legal.sort(key=lambda f: (f.start, f.action, f.target))
        return Experiment(tuple(legal))

    # -- misc ----------------------------------------------------------------------------
    def fault_cost(self, fault: Fault) -> float:
        spec = self.spec(fault.action)
        if spec.instantaneous:
            return spec.cost
        return spec.cost * (0.5 + 0.5 * fault.duration / spec.max_duration)

    def cost(self, exp: Experiment) -> float:
        """Blast radius of the whole experiment; lets the search prefer *small* experiments
        that still do a lot of damage."""
        return sum(self.fault_cost(f) for f in exp.faults)
