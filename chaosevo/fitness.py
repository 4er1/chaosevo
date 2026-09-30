"""Turning observations into a single 'damage' score (the fitness the GA maximises)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Objective:
    """One ingredient of the damage score.

    normalised = (value - baseline) / scale   for direction "up"   (bigger value = worse)
                 (baseline - value) / scale   for direction "down" (smaller value = worse)
    clamped to [0, cap]; damage += weight * normalised. `scale` is the value that counts as
    "fully damaged" for this metric, `cap` stops one metric from dominating without bound.
    """
    name: str
    weight: float = 1.0
    scale: float = 1.0
    cap: float = 1.0
    direction: str = "up"
    baseline: float = 0.0

    def normalise(self, value: float) -> float:
        raw = (value - self.baseline) if self.direction == "up" else (self.baseline - value)
        return min(max(raw / self.scale, 0.0), self.cap)

    @classmethod
    def from_dict(cls, d: dict) -> "Objective":
        if d.get("direction", "up") not in ("up", "down"):
            raise ValueError("objective direction must be 'up' or 'down'")
        return cls(str(d["name"]), float(d.get("weight", 1.0)), float(d.get("scale", 1.0)),
                   float(d.get("cap", 1.0)), str(d.get("direction", "up")), float(d.get("baseline", 0.0)))


@dataclass
class Result:
    damage: float
    components: Dict[str, float] = field(default_factory=dict)     # weighted contribution per objective
    observations: Dict[str, float] = field(default_factory=dict)   # raw aggregated measurements
    error: Optional[str] = None
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"damage": round(self.damage, 4), "components": _rounded(self.components),
                "observations": _rounded(self.observations), "error": self.error,
                "seconds": round(self.seconds, 2)}


def _rounded(d: Dict[str, float]) -> Dict[str, float]:
    return {k: round(v, 4) for k, v in d.items()}


class FitnessSpec:
    def __init__(self, objectives: Iterable[Objective]) -> None:
        self.objectives: List[Objective] = list(objectives)
        if not self.objectives:
            raise ValueError("need at least one objective")

    def score(self, observations: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
        total, parts = 0.0, {}
        for obj in self.objectives:
            value = observations.get(obj.name)
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue                      # not measured: contributes nothing
            parts[obj.name] = obj.weight * obj.normalise(value)
            total += parts[obj.name]
        return total, parts

    @classmethod
    def from_dicts(cls, items: Iterable[dict]) -> "FitnessSpec":
        return cls(Objective.from_dict(d) for d in items)
