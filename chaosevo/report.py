"""Human-readable report of a finished search."""
from __future__ import annotations

from typing import List


def render_markdown(results: dict) -> str:
    ga, out = results["ga"], []
    out.append(f"# chaosevo report - target `{results.get('target', '?')}`\n")
    out.append(f"* experiments executed: **{ga['evaluations']}**  (stopped: {ga['stopped_because']})")
    best = ga["best"]
    out.append(f"* most damaging experiment: **damage {best['damage']}**, fitness {best['fitness']}\n")
    out.append("## Evolution\n")
    out.append("| generation | best damage | mean damage | best fitness | experiments run |")
    out.append("|---:|---:|---:|---:|---:|")
    for h in ga["history"]:
        out.append(f"| {h['generation']} | {h['best_damage']:.2f} | {h['mean_damage']:.2f} | "
                   f"{h['best_fitness']:.2f} | {h['evaluations']} |")
    out.append("\n## Most damaging experiments found\n")
    for rank, ind in enumerate(ga["hall_of_fame"], 1):
        out.append(f"### #{rank} - damage {ind['damage']} (fitness {ind['fitness']})\n")
        for f in ind["experiment"]["faults"]:
            extra = "".join(f", {k}={v:g}" for k, v in f["params"].items())
            span = f" for {f['duration']:g}s" if f["duration"] else ""
            out.append(f"* `t={f['start']:g}s` **{f['action']}** on `{f['target']}`{extra}{span}")
        comps = ", ".join(f"{k} {v:.2f}" for k, v in sorted(ind["components"].items(), key=lambda kv: -kv[1]) if v > 0)
        out.append(f"\nWhere the damage came from: {comps or 'nothing measurable'}")
        obs = {k: v for k, v in ind["observations"].items() if not k.startswith("_")}
        out.append("\nMeasured: " + ", ".join(f"`{k}`={v:g}" for k, v in sorted(obs.items())))
        if ind.get("error"):
            out.append(f"\n⚠ run reported: {ind['error']}")
        out.append("")
    out.append("_Fitness = damage - cost_weight x blast radius: the search prefers small experiments "
               "that still hurt. Re-run one with `chaosevo replay <results.json> --rank N`._")
    return "\n".join(out) + "\n"
