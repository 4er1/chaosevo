"""Runs ONE experiment against a target and turns what happened into a Result."""
from __future__ import annotations

import heapq
import math
import threading
import time
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

from .fitness import FitnessSpec, Result
from .genome import Experiment, Fault
from .sources import Signal, aggregate
from .target import Target


class Executor:
    """reset -> start probes/sampling -> inject faults on schedule -> revert -> recovery -> score.

    `time_scale` shrinks every wait (0.05 = 20x faster) so simulations run quickly; timestamps
    are still reported in 'virtual' seconds. Keep it at 1.0 for real systems."""

    def __init__(self, target: Target, fitness: FitnessSpec, signals: Sequence[Signal] = (),
                 window: float = 30.0, recovery: float = 10.0, sample_interval: float = 1.0,
                 time_scale: float = 1.0) -> None:
        self.target, self.fitness, self.signals = target, fitness, list(signals)
        self.window, self.recovery = window, recovery
        self.sample_interval, self.scale = sample_interval, time_scale
        self.log = lambda msg: None      # the CLI plugs a printer in here

    def __call__(self, exp: Experiment) -> Result:
        return self.run(exp)

    # -- helpers -------------------------------------------------------------------------
    def _wait_until(self, t0: float, virtual: float) -> None:
        delay = t0 + virtual * self.scale - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _sampler(self, series: Dict[str, List[Tuple[float, float]]], t0: float,
                 stop: threading.Event, errors: List[int]) -> None:
        while not stop.is_set():
            now = (time.monotonic() - t0) / self.scale
            try:
                for k, v in self.target.sample().items():
                    series[k].append((now, float(v)))
            except Exception:
                errors[0] += 1
            for sig in self.signals:
                try:
                    series[sig.name].append((now, sig.read()))
                except Exception:
                    errors[0] += 1
            stop.wait(self.sample_interval * self.scale)

    # -- the experiment ------------------------------------------------------------------
    def run(self, exp: Experiment) -> Result:
        began = time.monotonic()
        series: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        sample_errors, inject_errors = [0], 0
        active: List[Fault] = []
        probes, stop, sampler = None, threading.Event(), None
        try:
            self.target.reset()
            probes = self.target.start_probes()
            t0 = time.monotonic()
            sampler = threading.Thread(target=self._sampler, args=(series, t0, stop, sample_errors), daemon=True)
            sampler.start()

            events: List[Tuple[float, int, int, str, Fault]] = []      # (when, order, idx, kind, fault)
            for i, f in enumerate(exp.faults):
                heapq.heappush(events, (f.start, 1, i, "inject", f))
                if f.duration > 0:
                    heapq.heappush(events, (min(f.start + f.duration, self.window), 0, i, "revert", f))
            while events:
                when, _, _, kind, fault = heapq.heappop(events)
                if when >= self.window:
                    break
                self._wait_until(t0, when)
                try:
                    if kind == "inject":
                        self.log(f"  t={when:5.1f}s inject {fault.describe()}")
                        self.target.inject(fault)
                        if fault.duration > 0:          # instantaneous faults have nothing to undo
                            active.append(fault)
                    elif fault in active:
                        self.log(f"  t={when:5.1f}s revert {fault.action}({fault.target})")
                        self.target.revert(fault)
                        active.remove(fault)
                except Exception as exc:
                    inject_errors += 1
                    self.log(f"  ! {kind} failed: {exc}")
            self._wait_until(t0, self.window)
            for fault in list(active):
                try:
                    self.target.revert(fault)
                except Exception:
                    inject_errors += 1
                active.remove(fault)
            self._wait_until(t0, self.window + self.recovery)      # let the system heal while we keep measuring
            stop.set()
            sampler.join(timeout=5)

            obs: Dict[str, float] = {}
            aggs = {**self.target.sample_aggs, **{s.name: s.agg for s in self.signals}}
            for name, points in series.items():
                value = aggregate(points, aggs.get(name, "max"))
                if not math.isnan(value):
                    obs[name] = value
            if probes is not None:
                obs.update(probes.stop())
                probes = None
            obs.update(self.target.final_check())
            damage, parts = self.fitness.score(obs)
            obs["_sample_errors"] = float(sample_errors[0])
            obs["_injection_errors"] = float(inject_errors)
            return Result(damage, parts, obs, seconds=time.monotonic() - began)
        finally:
            stop.set()
            for fault in active:                       # never leave a fault behind, whatever happened
                try:
                    self.target.revert(fault)
                except Exception:
                    pass
            if probes is not None:
                try:
                    probes.stop()
                except Exception:
                    pass
