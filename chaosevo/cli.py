"""Command line: chaosevo run | replay | report | actions"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from .exporter import MetricsServer
from .executor import Executor
from .fitness import FitnessSpec
from .ga import GAConfig, GeneticAlgorithm
from .genome import Experiment, SearchSpace
from .report import render_markdown
from .sources import PrometheusSource, ScrapeSource, Signal

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "simulated": {
        "experiment": {"window": 20, "recovery": 4, "sample_interval": 1, "time_scale": 0.05},
        "objectives": [{"name": "error_rate", "weight": 5, "scale": 0.5},
                       {"name": "p99_latency_s", "weight": 2, "scale": 3.0}]},
    "cellfs": {
        "experiment": {"window": 10, "recovery": 6, "sample_interval": 0.5, "time_scale": 1.0},
        "objectives": [{"name": "error_rate", "weight": 4, "scale": 0.5},
                       {"name": "p99_latency_s", "weight": 1.5, "scale": 5.0},
                       {"name": "write_error_rate", "weight": 1, "scale": 0.5},
                       {"name": "replica_margin_lost", "weight": 3, "scale": 1.0},
                       {"name": "at_risk_fraction", "weight": 3, "scale": 0.6},
                       {"name": "data_loss", "weight": 10, "scale": 1.0},
                       {"name": "lost_chunk_fraction", "weight": 5, "scale": 0.2}]},
    "kubernetes": {
        "experiment": {"window": 40, "recovery": 30, "sample_interval": 2, "time_scale": 1.0},
        "objectives": [{"name": "error_rate", "weight": 5, "scale": 0.5},
                       {"name": "p99_latency_s", "weight": 2, "scale": 2.0}]},
}


def _load_json(path: str) -> Any:
    with open(path) as fh:
        return json.load(fh)


def _load_text(path: str) -> str:
    with open(path) as fh:
        return fh.read().strip()


def load_config(path: Optional[str], target: Optional[str]) -> Dict[str, Any]:
    cfg: Dict[str, Any] = _load_json(path) if path else {}
    cfg.setdefault("target", {})
    if target:
        cfg["target"]["type"] = target
    ttype = cfg["target"].get("type")
    if ttype not in DEFAULTS:
        sys.exit(f"error: choose a target ({', '.join(DEFAULTS)}) with --target or in the config file")
    d = DEFAULTS[ttype]
    cfg["experiment"] = {**d["experiment"], **cfg.get("experiment", {})}
    cfg.setdefault("objectives", d["objectives"])
    cfg.setdefault("ga", {})
    cfg.setdefault("signals", [])
    return cfg


def build_target(cfg: Dict[str, Any]):
    t = dict(cfg["target"])
    kind = t.pop("type")
    if kind == "simulated":
        from .targets.simulated import SimulatedTarget
        return SimulatedTarget(time_scale=cfg["experiment"]["time_scale"])
    if kind == "cellfs":
        from .targets.cellfs_target import CellfsTarget
        return CellfsTarget(**{k: v for k, v in t.items() if k in ("nodes", "base_port", "replicas", "seed_kib", "chunk_kib", "workdir")})
    if kind == "kubernetes":
        from .targets.kubernetes import KubeApi, KubernetesTarget
        api = None
        if t.get("api_server"):
            token = _load_text(t["token_file"]) if t.get("token_file") else None
            api = KubeApi(t["api_server"], token, t.get("ca_file"), bool(t.get("insecure")))
        return KubernetesTarget(t["namespace"], t["deployments"], api, bool(t.get("chaos_mesh")),
                                float(t.get("reset_timeout", 120)), float(t.get("settle", 5)), t.get("http_probe"))
    raise ValueError(kind)


def build_signals(cfg: Dict[str, Any]) -> List[Signal]:
    prom = cfg.get("prometheus", {})
    signals = []
    for s in cfg["signals"]:
        if s.get("source", "prometheus") == "prometheus":
            source = PrometheusSource(prom["url"], token=prom.get("token"))
        else:
            source = ScrapeSource(s["urls"])
        signals.append(Signal(s["name"], s["query"], source, s.get("agg", "max")))
    return signals


def build_executor(cfg: Dict[str, Any], target) -> Executor:
    e = cfg["experiment"]
    ex = Executor(target, FitnessSpec.from_dicts(cfg["objectives"]), build_signals(cfg),
                  window=float(e["window"]), recovery=float(e["recovery"]),
                  sample_interval=float(e["sample_interval"]), time_scale=float(e["time_scale"]))
    return ex


def cmd_run(args) -> None:
    cfg = load_config(args.config, args.target)
    for key in ("generations", "population", "seed"):
        if getattr(args, key) is not None:
            cfg["ga"][key] = getattr(args, key)
    if args.window:
        cfg["experiment"]["window"] = args.window
    gcfg = GAConfig(**cfg["ga"])
    exporter = None
    results_state: Dict[str, Any] = {}
    with build_target(cfg) as target:
        executor = build_executor(cfg, target)
        executor.log = lambda m: print(m, flush=True) if args.verbose else None
        space = SearchSpace(target.actions(), cfg["experiment"]["window"],
                            cfg.get("min_faults", 1), cfg.get("max_faults", 4))
        seeds = [Experiment.from_dict(e) for e in cfg.get("seed_experiments", [])]
        if args.metrics_port is not None:
            exporter = MetricsServer(args.metrics_port, args.metrics_bind, cfg["target"]["type"],
                                     results=lambda: results_state)
            print(f"metrics on http://{args.metrics_bind}:{exporter.port}/metrics", flush=True)
        counter = [0]

        def on_eval(exp, res, fit):
            counter[0] += 1
            print(f"[eval {counter[0]:3d}] damage={res.damage:6.2f} fitness={fit:6.2f} "
                  f"({res.seconds:4.1f}s)  {exp.describe()}" + (f"  !! {res.error}" if res.error else ""), flush=True)
            if exporter:
                exporter.on_evaluation(exp, res, fit)

        def on_gen(stats, pop):
            print(f"== generation {stats.generation}: best damage {stats.best_damage:.2f}, "
                  f"mean {stats.mean_damage:.2f}, experiments run {stats.evaluations}", flush=True)
            if exporter:
                exporter.on_generation(stats, pop)
            partial = {"best": pop[0].to_dict(), "generation": stats.generation}
            results_state.clear(); results_state.update(partial)

        ga = GeneticAlgorithm(space, executor, gcfg, seeds, on_generation=on_gen, on_evaluation=on_eval)
        try:
            result = ga.run()
        finally:
            if exporter:
                exporter.finish()
    out = {"target": cfg["target"]["type"], "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "config": cfg, "ga": result.to_dict()}
    results_state.clear(); results_state.update(out)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"results written to {args.out}")
    print("\n" + render_markdown(out))
    if exporter and args.linger:
        print(f"keeping metrics up for {args.linger}s (Prometheus/Grafana can still scrape them)", flush=True)
        time.sleep(args.linger)
    if exporter:
        exporter.close()


def cmd_replay(args) -> None:
    data = _load_json(args.results)
    cfg = data["config"]
    ind = data["ga"]["hall_of_fame"][args.rank - 1]
    exp = Experiment.from_dict(ind["experiment"])
    with build_target(cfg) as target:
        executor = build_executor(cfg, target)
        executor.log = print
        print(f"replaying #{args.rank}: {exp.describe()}")
        res = executor(exp)
    print(f"damage now {res.damage:.2f}  (was {ind['damage']})   components: {res.to_dict()['components']}")


def cmd_report(args) -> None:
    print(render_markdown(_load_json(args.results)))


def cmd_actions(args) -> None:
    cfg = load_config(args.config, args.target)
    with build_target(cfg) as target:
        for a in target.actions():
            params = ", ".join(f"{p.name}[{p.low:g}..{p.high:g}]" for p in a.params)
            dur = "instant" if a.instantaneous else f"{a.min_duration:g}-{a.max_duration:g}s"
            print(f"{a.name:<16} cost={a.cost:<4} {dur:<9} targets={list(a.targets)} {params}\n    {a.description}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="chaosevo", description="Evolutionary chaos engineering")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", help="JSON config file")
        sp.add_argument("--target", choices=sorted(DEFAULTS), help="system under test")

    r = sub.add_parser("run", help="evolve experiments and report the most damaging ones")
    common(r)
    r.add_argument("--generations", type=int)
    r.add_argument("--population", type=int)
    r.add_argument("--seed", type=int)
    r.add_argument("--window", type=float, help="length of each experiment in seconds")
    r.add_argument("--out", help="write full results (JSON) here")
    r.add_argument("--metrics-port", type=int, help="serve Prometheus metrics about the search")
    r.add_argument("--metrics-bind", default="127.0.0.1")
    r.add_argument("--linger", type=float, default=0, help="keep serving metrics this many seconds after finishing")
    r.add_argument("-v", "--verbose", action="store_true", help="log every injected fault")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("replay", help="re-run one of the experiments from a results file")
    rp.add_argument("results")
    rp.add_argument("--rank", type=int, default=1)
    rp.set_defaults(func=cmd_replay)

    rt = sub.add_parser("report", help="print the Markdown report of a results file")
    rt.add_argument("results")
    rt.set_defaults(func=cmd_report)

    ac = sub.add_parser("actions", help="list the chaos actions a target offers")
    common(ac)
    ac.set_defaults(func=cmd_actions)

    args = p.parse_args(argv)
    args.func(args)
