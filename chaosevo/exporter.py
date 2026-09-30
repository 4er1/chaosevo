"""Prometheus exporter for the search itself, so Grafana can draw the *evolution*."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional

from .fitness import Result
from .ga import GenStats
from .genome import Experiment


class MetricsServer:
    def __init__(self, port: int, bind: str = "127.0.0.1", target: str = "unknown",
                 results: Optional[Callable[[], dict]] = None) -> None:
        self.target, self.results = target, results
        self._lock = threading.Lock()
        self.state: Dict[str, float] = {"generation": -1, "evaluations": 0, "best_damage": 0.0, "best_fitness": 0.0,
                                        "generation_best_damage": 0.0, "generation_mean_damage": 0.0,
                                        "last_damage": 0.0, "last_faults": 0, "last_seconds": 0.0, "last_errors": 0}
        self.best_components: Dict[str, float] = {}
        self._best_seen = float("-inf")
        self.running = 1
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:                      # noqa: N802 (http.server API)
                path = self.path.split("?")[0]
                if path == "/metrics":
                    body, ctype = outer.render().encode(), "text/plain; version=0.0.4; charset=utf-8"
                elif path == "/healthz":
                    body, ctype = b"ok\n", "text/plain"
                elif path == "/results" and outer.results:
                    body, ctype = json.dumps(outer.results(), indent=1).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # -- updates from the GA callbacks -----------------------------------------------------
    def on_evaluation(self, exp: Experiment, result: Result, fitness: float) -> None:
        with self._lock:
            self.state["evaluations"] += 1
            self.state["last_damage"] = result.damage
            self.state["last_faults"] = len(exp.faults)
            self.state["last_seconds"] = result.seconds
            self.state["last_errors"] = 0 if result.error is None else 1
            if fitness > self._best_seen:
                self._best_seen = fitness
                self.state["best_damage"], self.state["best_fitness"] = result.damage, fitness
                self.best_components = dict(result.components)

    def on_generation(self, stats: GenStats, population: List) -> None:
        with self._lock:
            self.state["generation"] = stats.generation
            self.state["generation_best_damage"] = stats.best_damage
            self.state["generation_mean_damage"] = stats.mean_damage

    def finish(self) -> None:
        with self._lock:
            self.running = 0

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- exposition -----------------------------------------------------------------------
    def render(self) -> str:
        with self._lock:
            s, comps, running = dict(self.state), dict(self.best_components), self.running
        t = f'target="{self.target}"'
        lines = [
            "# HELP chaosevo_running 1 while the search is running", "# TYPE chaosevo_running gauge",
            f"chaosevo_running{{{t}}} {running}",
            "# HELP chaosevo_generation Current generation (0-based)", "# TYPE chaosevo_generation gauge",
            f"chaosevo_generation{{{t}}} {s['generation']}",
            "# HELP chaosevo_evaluations_total Experiments executed so far", "# TYPE chaosevo_evaluations_total counter",
            f"chaosevo_evaluations_total{{{t}}} {int(s['evaluations'])}",
            "# HELP chaosevo_best_damage Damage of the best experiment found so far", "# TYPE chaosevo_best_damage gauge",
            f"chaosevo_best_damage{{{t}}} {s['best_damage']:.4f}",
            "# HELP chaosevo_best_fitness Fitness (damage - cost penalty) of the best experiment", "# TYPE chaosevo_best_fitness gauge",
            f"chaosevo_best_fitness{{{t}}} {s['best_fitness']:.4f}",
            "# HELP chaosevo_generation_best_damage Best damage inside the latest generation", "# TYPE chaosevo_generation_best_damage gauge",
            f"chaosevo_generation_best_damage{{{t}}} {s['generation_best_damage']:.4f}",
            "# HELP chaosevo_generation_mean_damage Mean damage of the latest generation", "# TYPE chaosevo_generation_mean_damage gauge",
            f"chaosevo_generation_mean_damage{{{t}}} {s['generation_mean_damage']:.4f}",
            "# HELP chaosevo_experiment_damage Damage of the experiment that ran last", "# TYPE chaosevo_experiment_damage gauge",
            f"chaosevo_experiment_damage{{{t}}} {s['last_damage']:.4f}",
            "# HELP chaosevo_experiment_faults Number of faults in the experiment that ran last", "# TYPE chaosevo_experiment_faults gauge",
            f"chaosevo_experiment_faults{{{t}}} {int(s['last_faults'])}",
            "# HELP chaosevo_experiment_seconds Wall-clock duration of the experiment that ran last", "# TYPE chaosevo_experiment_seconds gauge",
            f"chaosevo_experiment_seconds{{{t}}} {s['last_seconds']:.2f}",
            "# HELP chaosevo_experiment_errors 1 if the last experiment could not be executed properly", "# TYPE chaosevo_experiment_errors gauge",
            f"chaosevo_experiment_errors{{{t}}} {int(s['last_errors'])}",
            "# HELP chaosevo_best_damage_component Weighted damage per objective in the best experiment", "# TYPE chaosevo_best_damage_component gauge",
        ]
        lines += [f'chaosevo_best_damage_component{{{t},objective="{k}"}} {v:.4f}' for k, v in sorted(comps.items())]
        return "\n".join(lines) + "\n"
