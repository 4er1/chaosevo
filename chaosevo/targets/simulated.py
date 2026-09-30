"""A tiny in-memory microservice model (web -> api -> db) with retry storms.

Not real chaos - it exists so you can try the search, the metrics and the Grafana dashboard on any
laptop in seconds, and so the tests have a system with a known, non-linear failure landscape.
"""
from __future__ import annotations

import time
from typing import Dict, List, Tuple

from ..genome import ActionSpec, Fault, ParamSpec
from ..target import Target

REPLICAS = {"web": 3, "api": 3, "db": 2}
RESTART_DELAY = 6.0     # virtual seconds a killed pod needs to come back


class SimulatedTarget(Target):
    name = "simulated"
    sample_aggs = {"error_rate": "mean", "p99_latency_s": "max"}   # mean error rate = error budget burnt

    def __init__(self, time_scale: float = 1.0) -> None:
        self.scale = time_scale
        self._t0 = time.monotonic()
        self._kills: List[Tuple[str, float]] = []               # (service, restart_time)
        self._timed: Dict[Fault, Tuple[str, float, float]] = {}  # fault -> (kind, service, value)
        self.injected: List[str] = []                            # for tests

    def actions(self) -> List[ActionSpec]:
        services = sorted(REPLICAS)
        return [
            ActionSpec("pod_kill", services, [ParamSpec("count", 1, 3, integer=True)], cost=1.0,
                       description="kill pods of a service (they restart after a delay)"),
            ActionSpec("network_delay", services, [ParamSpec("latency_ms", 50, 1500)],
                       min_duration=3, max_duration=15, cost=1.0, description="add latency"),
            ActionSpec("cpu_stress", services, [ParamSpec("load", 40, 100)],
                       min_duration=3, max_duration=15, cost=1.0, description="burn CPU"),
        ]

    def _now(self) -> float:
        return (time.monotonic() - self._t0) / self.scale

    def reset(self) -> None:
        self._t0 = time.monotonic()
        self._kills.clear()
        self._timed.clear()

    def inject(self, fault: Fault) -> None:
        self.injected.append(f"inject:{fault.action}:{fault.target}")
        if fault.action == "pod_kill":
            for _ in range(int(fault.param("count", 1))):
                self._kills.append((fault.target, self._now() + RESTART_DELAY))
        else:
            key = "delay" if fault.action == "network_delay" else "cpu"
            self._timed[fault] = (key, fault.target, fault.param("latency_ms" if key == "delay" else "load", 0))

    def revert(self, fault: Fault) -> None:
        self.injected.append(f"revert:{fault.action}:{fault.target}")
        self._timed.pop(fault, None)

    def sample(self) -> Dict[str, float]:
        now = self._now()
        cap: Dict[str, float] = {}
        delay_ms = 0.0
        for svc, replicas in REPLICAS.items():
            dead = sum(1 for s, until in self._kills if s == svc and until > now)
            cpu = max([v for k, s, v in self._timed.values() if k == "cpu" and s == svc] or [0.0])
            cap[svc] = max(replicas - dead, 0) / replicas * (1 - 0.6 * cpu / 100)
            delay_ms += sum(v for k, s, v in self._timed.values() if k == "delay" and s == svc)
        eff_api = cap["api"] * (0.3 + 0.7 * cap["db"])
        eff_web = cap["web"] * (0.3 + 0.7 * eff_api)
        latency = 0.05 + delay_ms / 1000 + 0.05 / max(eff_web, 0.05)
        storm = min(0.3, 0.2 * max(latency - 1.0, 0.0))          # clients retry when things get slow
        return {"error_rate": min(1.0, max(0.0, 1 - eff_web) + storm), "p99_latency_s": latency}
