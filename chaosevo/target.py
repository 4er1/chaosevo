"""What a system under test must provide so that chaosevo can attack it."""
from __future__ import annotations

import abc
from typing import Dict, List, Optional

from .genome import ActionSpec, Fault


class Probes(abc.ABC):
    """A workload running during the experiment (e.g. a load generator). `stop()` returns
    measurements such as {'error_rate': 0.12, 'p99_latency_s': 1.4}."""

    @abc.abstractmethod
    def stop(self) -> Dict[str, float]: ...


class Target(abc.ABC):
    name = "target"
    #: how repeated samples of each sampled metric are collapsed: max | min | mean | last | integral
    sample_aggs: Dict[str, str] = {}

    def setup(self) -> None:
        """Called once before the search (deploy, seed data...)."""

    @abc.abstractmethod
    def actions(self) -> List[ActionSpec]:
        """The chaos actions this target supports (and on which sub-targets)."""

    @abc.abstractmethod
    def reset(self) -> None:
        """Bring the system back to a healthy steady state before every experiment."""

    @abc.abstractmethod
    def inject(self, fault: Fault) -> None: ...

    @abc.abstractmethod
    def revert(self, fault: Fault) -> None:
        """Undo a timed fault (restart the pod, remove the latency...). Must be safe to call twice."""

    def start_probes(self) -> Optional[Probes]:
        return None

    def sample(self) -> Dict[str, float]:
        """Extra metrics sampled while the experiment runs (besides Prometheus signals)."""
        return {}

    def final_check(self) -> Dict[str, float]:
        """Measured after the recovery period, e.g. 'is the data still there?'"""
        return {}

    def close(self) -> None:
        """Called at the end of the search: remove anything the search left behind."""

    def __enter__(self) -> "Target":
        self.setup()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
