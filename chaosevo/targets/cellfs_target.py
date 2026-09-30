"""Chaos target for a `cellfs` cluster (the self-healing distributed file store): 5 cells as local
processes, a read/write workload, and faults such as crashes, freezes, disk loss and bit rot.

Requires the `cellfs` package (pip install -e ../cellfs, or set PYTHONPATH).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..genome import ActionSpec, Fault, ParamSpec
from ..probes import LatencyRecorder
from ..target import Probes, Target


def _import_cellfs():
    try:
        from cellfs.client import Client
        from cellfs.cluster import LocalCluster
        from cellfs.store import ObjectStore
        return Client, LocalCluster, ObjectStore
    except ImportError as exc:
        raise RuntimeError("the cellfs target needs the cellfs package: "
                           "`pip install -e ../cellfs` or set PYTHONPATH") from exc


class _Workload(Probes):
    """Reads the seeded file every ~0.4 s and writes a small file every ~1 s."""

    def __init__(self, client_factory, manifest_id: str, expected_sha: str, workdir: Path) -> None:
        self.reads, self.writes = LatencyRecorder(), LatencyRecorder()
        self._stop = threading.Event()
        self._mk, self._mid, self._sha, self._dir = client_factory, manifest_id, expected_sha, workdir
        self._threads = [threading.Thread(target=self._read_loop, daemon=True),
                         threading.Thread(target=self._write_loop, daemon=True)]
        for t in self._threads:
            t.start()

    def _read_loop(self) -> None:
        out = self._dir / "probe-read.bin"
        while not self._stop.is_set():
            began, ok = time.monotonic(), False
            try:
                asyncio.run(self._mk().get_file(self._mid, out))
                ok = hashlib.sha256(out.read_bytes()).hexdigest() == self._sha
            except Exception:
                ok = False
            self.reads.record(ok, time.monotonic() - began)
            self._stop.wait(max(0.0, 0.4 - (time.monotonic() - began)))

    def _write_loop(self) -> None:
        rng, src = random.Random(99), self._dir / "probe-write.bin"
        while not self._stop.is_set():
            began, ok = time.monotonic(), False
            try:
                src.write_bytes(rng.randbytes(4096))
                asyncio.run(self._mk().put_file(src))
                ok = True
            except Exception:
                ok = False
            self.writes.record(ok, time.monotonic() - began)
            self._stop.wait(max(0.0, 1.0 - (time.monotonic() - began)))

    def stop(self) -> Dict[str, float]:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=8)
        out = self.reads.summary()                      # error_rate, p99_latency_s, requests
        out.update({"write_" + k: v for k, v in self.writes.summary().items()})
        return out


class CellfsTarget(Target):
    name = "cellfs"
    sample_aggs = {"replica_margin_lost": "max", "at_risk_fraction": "max"}

    def __init__(self, nodes: int = 5, base_port: int = 9800, replicas: int = 3,
                 seed_kib: int = 1024, chunk_kib: int = 64, workdir: Optional[str] = None) -> None:
        self.Client, self.LocalCluster, self.ObjectStore = _import_cellfs()
        self.n, self.base_port, self.replicas = nodes, base_port, replicas
        self.seed_kib, self.chunk_kib = seed_kib, chunk_kib
        self.work = Path(workdir or tempfile.mkdtemp(prefix="chaosevo-cellfs-"))
        self.cluster = None
        self.manifest_id = ""
        self.object_ids: List[tuple] = []
        self.chunk_ids: List[str] = []
        self._frozen: set = set()

    # -- lifecycle -----------------------------------------------------------------------
    def _client(self):
        addrs = [self.cluster.addr(n) for n in self.cluster.running()] or [self.cluster.addr("n1")]
        return self.Client(addrs, chunk_size=self.chunk_kib * 1024, timeout=1.5)

    def _build(self) -> None:
        self.cluster = self.LocalCluster(n=self.n, base_port=self.base_port, data_dir=self.work / "cells",
                                         replicas=self.replicas, fast=True)

    def setup(self) -> None:
        self._build()
        self.reset()                                    # also seeds the data set

    def reset(self) -> None:
        for nid in list(self.cluster.procs):
            if self.cluster.procs[nid].poll() is None:
                try:
                    os.kill(self.cluster.procs[nid].pid, signal.SIGCONT)
                except OSError:
                    pass
        self._frozen.clear()
        self.cluster.stop()
        shutil.rmtree(self.cluster.data_dir, ignore_errors=True)
        self._build()                                   # fresh process table, empty disks
        self.cluster.start()
        if not self.cluster.wait_ready(30):
            raise RuntimeError("cellfs cluster did not become ready")
        src = self.work / "seed.bin"
        src.write_bytes(random.Random(1234).randbytes(self.seed_kib * 1024))
        self.sha = hashlib.sha256(src.read_bytes()).hexdigest()
        self.manifest_id = asyncio.run(self._client().put_file(src))
        chunks = asyncio.run(self._client().get_manifest(self.manifest_id))["chunks"]
        self.chunk_ids = chunks
        self.object_ids = [("manifest", self.manifest_id)] + [("chunk", c) for c in chunks]

    def close(self) -> None:
        if self.cluster is not None:
            for nid, proc in self.cluster.procs.items():
                if proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except OSError:
                        pass
            self.cluster.stop()
        shutil.rmtree(self.work, ignore_errors=True)

    # -- chaos ---------------------------------------------------------------------------
    def actions(self) -> List[ActionSpec]:
        nodes = list(self.cluster.ids)
        return [
            ActionSpec("kill_node", nodes, min_duration=2, max_duration=8, cost=1.0,
                       description="SIGKILL a cell; it restarts (with its disk) after `duration`"),
            ActionSpec("pause_node", nodes, min_duration=2, max_duration=8, cost=1.0,
                       description="SIGSTOP a cell (alive but unresponsive, like a partition)"),
            ActionSpec("wipe_node", nodes, min_duration=2, max_duration=8, cost=2.0,
                       description="SIGKILL a cell and destroy its disk; it comes back empty"),
            ActionSpec("corrupt_chunk", nodes, [ParamSpec("chunk_idx", 0, len(self.chunk_ids) - 1, integer=True)],
                       cost=0.5, description="flip a byte of one chunk file on a cell"),
        ]

    def inject(self, fault: Fault) -> None:
        nid, c = fault.target, self.cluster
        if fault.action == "kill_node":
            if c.procs[nid].poll() is None:
                c.kill(nid)
        elif fault.action == "pause_node":
            if c.procs[nid].poll() is None:
                os.kill(c.procs[nid].pid, signal.SIGSTOP)
                self._frozen.add(nid)
        elif fault.action == "wipe_node":
            if c.procs[nid].poll() is None:
                c.kill(nid)
            shutil.rmtree(c.node_dir(nid), ignore_errors=True)
        elif fault.action == "corrupt_chunk":
            path = c.node_dir(nid) / "chunks" / self.chunk_ids[int(fault.param("chunk_idx", 0))]
            if path.is_file():
                raw = bytearray(path.read_bytes())
                raw[len(raw) // 2] ^= 0xFF
                path.write_bytes(bytes(raw))

    def revert(self, fault: Fault) -> None:
        nid, c = fault.target, self.cluster
        if fault.action == "pause_node" and nid in self._frozen:
            os.kill(c.procs[nid].pid, signal.SIGCONT)
            self._frozen.discard(nid)
        elif fault.action in ("kill_node", "wipe_node") and c.procs[nid].poll() is not None:
            c.start_node(nid)

    # -- measuring -----------------------------------------------------------------------
    def start_probes(self) -> Probes:
        return _Workload(self._client, self.manifest_id, self.sha, self.work)

    def _valid_copies(self, kind: str, oid: str) -> int:
        return sum(1 for nid in self.cluster.running()
                   if self.ObjectStore(self.cluster.node_dir(nid)).verify(kind, oid) == "ok")

    def sample(self) -> Dict[str, float]:
        """How close did we get to losing something?
        replica_margin_lost: 0 = every object has all its copies, 1 = some object has no intact copy anywhere.
        at_risk_fraction:    share of objects down to their last copy (or none) - a stepping stone towards
                             data loss, so the search gets credit for getting closer instead of facing a plateau."""
        copies = [self._valid_copies(k, i) for k, i in self.object_ids]
        return {"replica_margin_lost": max(0.0, (self.replicas - min(copies)) / self.replicas),
                "at_risk_fraction": sum(1 for c in copies if c <= 1) / len(copies)}

    def final_check(self) -> Dict[str, float]:
        lost = sum(1 for k, i in self.object_ids if k == "chunk" and self._valid_copies(k, i) == 0)
        readable = 0.0
        for _ in range(3):                              # give the cluster a moment before declaring loss
            try:
                out = self.work / "final.bin"
                asyncio.run(self._client().get_file(self.manifest_id, out))
                readable = 1.0 if hashlib.sha256(out.read_bytes()).hexdigest() == self.sha else 0.0
                break
            except Exception:
                time.sleep(1.0)
        return {"data_loss": 1.0 - readable, "lost_chunk_fraction": lost / max(1, len(self.chunk_ids))}
