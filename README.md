# chaosevo

![tests](https://github.com/41R3/chaosevo/actions/workflows/ci.yml/badge.svg)

**Evolutionary chaos engineering.** Instead of running chaos experiments somebody thought of, a genetic
algorithm *searches* for the experiment that hurts your system the most: it breeds a population of
random experiments, measures the damage each one causes, keeps the nastiest, recombines and mutates
them, and repeats. Pure Python (standard library only).

```
  +-------------------+   population of experiments   +---------------------------------+
  |  genetic algorithm|  --------------------------->  |  executor                        |
  |  select / cross / |                                |  reset -> inject -> measure ->   |
  |  mutate / elitism |  <---------  damage ---------  |  revert -> recover -> score      |
  +---------+---------+        (the fitness)           +----------------+----------------+
            |                                                           | faults + probes
            | chaosevo_* metrics                                   +----v-----+
            v                                                      |  target  |  cellfs | kubernetes | simulated
   Prometheus --> Grafana (evolution dashboard)                    +----+-----+
                                                                        | damage signals: PromQL queries,
                                                                        | /metrics scraping, HTTP probes
```

Inspired by the idea behind [krkn-ai](https://github.com/krkn-chaos/krkn-ai); this is an independent,
much smaller implementation (no code copied) meant to be read and understood end to end.

> **Project status:** the genetic algorithm, executor, `simulated` target, metrics exporter, CLI and the
> Grafana/Prometheus stack run locally and are covered by 62 passing tests (plus 3 cellfs integration tests
> that need the companion project). Results on the `simulated` target were reproduced locally (two runs
> below); results on a real `cellfs` cluster were recorded during development and are reproducible with the
> command shown. The `kubernetes` target and the Ansible role were validated statically and against a fake
> Kubernetes API, **not on a live cluster** (see [Limitations and safety](#limitations-and-safety)).

## The pieces

**Genome.** An experiment is a schedule of 1-4 *faults*: `t=3s kill_node(n2) for 5s`,
`t=4s network_delay(web, latency_ms=400) for 10s`... A target declares which actions exist, on what,
and with which parameter ranges (`ActionSpec`); the `SearchSpace` keeps every random, crossed or
mutated experiment inside those ranges.

**Fitness = damage - cost.** Damage is a weighted sum of *objectives* built from measurements
(error rate, p99 latency, data loss, unavailable replicas...). Each objective is normalised
(`(value - baseline) / scale`, clamped to `[0, cap]`) so no single metric dominates. `cost` is the
experiment's blast radius (number, severity and length of faults) times `cost_weight`, so the search
prefers *small* experiments that still do a lot of damage - the ones worth fixing first.

**Genetic algorithm.** Tournament selection, time-point crossover (A's faults before a random moment,
B's after), per-fault mutation (shift the start, change the duration, retarget, tweak a parameter,
replace the fault), structural mutation (add/remove a fault), elitism, and a cache so a genome is never
executed twice. Seedable (`seed`) and stoppable by generations, stagnation or time.

**Where damage is measured** (mix and match):

| Source | For | How |
|---|---|---|
| `prometheus` | real clusters | PromQL instant queries against `/api/v1/query`, sampled every `sample_interval` |
| `scrape` | no Prometheus server | reads `/metrics` endpoints directly and sums a metric |
| probes | user-visible damage | an HTTP probe (or the target's own workload) records error rate and p99 |
| target `sample()` / `final_check()` | system-specific | e.g. "does any object have a single copy left?", "is the data still there?" |

Samples over time are collapsed with `max`, `min`, `mean`, `last` or `integral` (area under the curve).

**Targets.**

* `simulated` - an in-memory web -> api -> db model with retry storms. Not real chaos: a safe playground.
* `cellfs` - a real cluster of 5 cells from the companion *cellfs* project (separate OS processes), a
  read/write workload, and faults `kill_node`, `pause_node` (SIGSTOP), `wipe_node` (crash + disk loss),
  `corrupt_chunk` (bit rot).
* `kubernetes` - Deployments through the Kubernetes REST API (no kubectl): `pod_kill`, `pod_kill_loop`,
  `scale_down`, and with [Chaos Mesh](https://chaos-mesh.org) `network_delay`, `network_loss`, `cpu_stress`.
  Runs in-cluster with a namespaced ServiceAccount.

**Observability.** `--metrics-port` serves `chaosevo_*` metrics (`best_damage`, `generation_mean_damage`,
`experiment_damage`, `best_damage_component{objective}`, ...) and `/results`. Prometheus scrapes them and
the bundled Grafana dashboard draws the evolution.

## Quick start

Requires Python 3.10+ (and Docker for step 2). On Windows, use WSL.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"                        # only jinja2 + pyyaml, for the deploy-file tests

# 1. Try the search on the built-in simulator (about a minute, nothing real is touched)
python -m chaosevo run --target simulated --generations 8 --population 12 --seed 1 --out results.json
python -m chaosevo report results.json          # Markdown report
python -m chaosevo replay results.json --rank 1 # run the #1 experiment again

# 2. Watch it evolve in Grafana - no Kubernetes needed
cp .env.example .env                            # set GRAFANA_PASSWORD (letters and digits, no $ or #)
docker compose --env-file .env -f observability/docker-compose.yml up --build
#   http://localhost:3000  (admin / your password)  ->  "chaosevo - evolution of the search"
#   stop with Ctrl+C, then: docker compose --env-file .env -f observability/docker-compose.yml down
#   (Grafana stores the password on first start: if you change .env later, run `down` before `up`)

# 3. Attack a real (local) system: a cellfs cluster.  Needs the cellfs project importable.
PYTHONPATH=../cellfs python -m chaosevo run --target cellfs --population 8 --generations 6 --out cellfs.json

# 4. Kubernetes (see below)
```

### Kubernetes with Ansible

```bash
docker build -t ghcr.io/<you>/chaosevo:0.1.0 . && docker push ghcr.io/<you>/chaosevo:0.1.0
pip install kubernetes && ansible-galaxy collection install -r ansible/requirements.yml
ansible-playbook ansible/site.yml \
  -e chaosevo_image=ghcr.io/<you>/chaosevo:0.1.0 \
  -e chaosevo_install_monitoring=true -e chaosevo_grafana_admin_password='<12+ chars, use ansible-vault>'
kubectl -n chaosevo logs -f job/chaosevo-<run id>
```

The role creates: the namespaces, a demo target (podinfo x3), **namespaced** RBAC (pods get/list/delete,
deployments get/list, deployments/scale patch - plus the two Chaos Mesh kinds only if enabled), the
search config (ConfigMap), a Service + ServiceMonitor for the metrics, the Grafana dashboard
(sidecar ConfigMap) and the search itself as a Kubernetes `Job`. Optionally it installs
kube-prometheus-stack and Chaos Mesh with Helm. All knobs are in
`ansible/roles/chaosevo/defaults/main.yml`.

## Configuration

JSON, everything optional except the target:

```json
{
  "target": {"type": "kubernetes", "namespace": "shop", "deployments": ["cart", "checkout"],
             "chaos_mesh": true, "http_probe": {"url": "http://checkout.shop.svc:8080/healthz"}},
  "experiment": {"window": 40, "recovery": 30, "sample_interval": 2},
  "ga": {"population": 10, "generations": 8, "cost_weight": 0.3, "seed": 1, "stagnation": 3},
  "prometheus": {"url": "http://prometheus.monitoring:9090"},
  "signals": [{"name": "ready_ratio", "source": "prometheus", "agg": "min",
               "query": "min(kube_deployment_status_replicas_available{namespace=\"shop\"} / kube_deployment_spec_replicas{namespace=\"shop\"})"}],
  "objectives": [
    {"name": "error_rate",    "weight": 5, "scale": 0.5},
    {"name": "p99_latency_s", "weight": 2, "scale": 2.0},
    {"name": "ready_ratio",   "weight": 3, "scale": 1.0, "direction": "down", "baseline": 1.0}
  ],
  "seed_experiments": [{"faults": [{"action": "pod_kill", "target": "cart", "start": 5, "params": {"count": 2}}]}]
}
```

* An objective's `name` is the key of a measurement: a probe result, a target sample, or a `signals` entry.
* `scale` = the value that counts as "fully damaged"; `direction: "down"` is for metrics where *lower* is worse.
* `seed_experiments` inject known scenarios into generation 0 (your team's past incidents, for instance).
* Prometheus signals see data as fresh as the scrape interval, so keep `window` well above it.

`python -m chaosevo actions --config cfg.json` lists what a target can do.

## Example results

### A. Simulated target (reproduced locally)

The built-in `simulated` target is an in-memory web -> api -> db model with retry storms. Both runs use
population 12, seed 1 and the default objectives (`error_rate`, `p99_latency_s`).

**Run 1 - 8 generations (82 experiments, about 2 minutes)**

```bash
python -m chaosevo run --target simulated --generations 8 --population 12 --seed 1 --out results.json
```

| generation | best damage | mean damage | experiments run |
|---:|---:|---:|---:|
| 0 | 4.95 | 2.21 | 12 |
| 1 | 5.69 | 3.96 | 22 |
| 2 | 5.69 | 4.25 | 32 |
| 3 | 5.70 | 4.87 | 42 |
| 4 | 5.70 | 4.90 | 52 |
| 5 | 6.01 | 4.98 | 62 |
| 6 | 6.01 | 5.51 | 72 |
| 7 | 6.48 | 5.14 | 82 |

The most damaging experiment (damage 6.48) is four faults on `web`: CPU stress at t=0.3 s, network delay
(1173 ms) at t=1.7 s, then `pod_kill` (3 pods) at t=3.9 s and again at t=15.6 s. Replaying it with
`chaosevo replay results.json --rank 1` scored **6.48** again (was 6.4822), so the measurement is
reproducible on this target.

**Run 2 - 25 generations through the Grafana stack (252 experiments)**

Run by `docker compose` (Quick start, step 2) with `--generations 25 --population 12 --seed 1`, watched live in Grafana.

| generation | best damage | mean damage | best fitness | experiments run |
|---:|---:|---:|---:|---:|
| 0 | 4.95 | 2.22 | 4.35 | 12 |
| 1 | 5.69 | 3.96 | 4.86 | 22 |
| 2 | 5.69 | 4.22 | 4.86 | 32 |
| 3 | 5.70 | 4.66 | 4.87 | 42 |
| 4 | 5.70 | 5.10 | 4.87 | 52 |
| 5 | 6.01 | 4.94 | 4.91 | 62 |
| 6 | 6.01 | 5.25 | 4.91 | 72 |
| 7 | 6.01 | 5.35 | 4.91 | 82 |
| 8 | 6.07 | 5.31 | 4.97 | 92 |
| 9 | 6.08 | 5.01 | 4.98 | 102 |
| 10 | 6.08 | 4.90 | 4.98 | 112 |
| 11 | 6.08 | 4.71 | 4.98 | 122 |
| 12 | 6.08 | 5.57 | 5.21 | 132 |
| 13 | 6.27 | 5.38 | 5.41 | 142 |
| 14 | 6.27 | 5.48 | 5.41 | 152 |
| 15 | 6.27 | 4.91 | 5.41 | 162 |
| 16 | 6.27 | 4.62 | 5.41 | 172 |
| 17 | 6.47 | 5.54 | 5.61 | 182 |
| 18 | 6.81 | 5.43 | 5.68 | 192 |
| 19 | 6.81 | 5.85 | 5.68 | 202 |
| 20 | 6.81 | 5.49 | 5.68 | 212 |
| 21 | 6.81 | 5.55 | 5.68 | 222 |
| 22 | 6.81 | 5.60 | 5.68 | 232 |
| 23 | 6.81 | 5.92 | 5.68 | 242 |
| 24 | 6.81 | 4.92 | 5.68 | 252 |

The most damaging experiment (damage 6.81, fitness 5.68): `pod_kill` `web` x3 at t=2.9 s and again at
t=12.5 s, then `network_delay` `web` 1155 ms for 11.5 s at t=15.2 s and `network_delay` `api` 512 ms for
11.6 s at t=16.6 s. Measured: `error_rate` = 0.506 and `p99_latency_s` = 2.72 s.

**Reading these numbers**

* **The search works.** The population mean rises from 2.2 (generation 0) to about 5.1 by generation 4,
  and the best experiment climbs from 4.95 to 6.81.
* **It is not monotonic.** The best never gets worse (elitism), but the mean wobbles between 4.6 and 5.9
  after generation 4 because mutation keeps sending offspring into weak regions. Most of the gain comes in
  the first five generations; afterwards the best improves in small steps (6.08 -> 6.27 -> 6.47 -> 6.81).
* **`error_rate` saturates.** In every top experiment its component is pinned at 5.00 (measured error rate
  about 0.5, the objective's `scale`), so further gains can only come from `p99_latency_s` - a reminder that
  objective scales shape what the search can find.
* **Noise.** The same seed gives slightly different runs (run 1 reached 6.48 after 82 experiments, run 2
  reached 6.01 after the same number) because the simulator measures wall-clock time. Use `replay` to confirm a finding.
* These numbers come from a model, not from a real system.

### B. Real cellfs cluster (recorded during development)

A search against a live 5-cell cellfs cluster (separate OS processes) with `--population 8 --generations 6
--seed 11`: 38 experiments, ~17 s each, ~11 minutes in total. These numbers were recorded during development
and have not been re-run for this release; expect similar but not identical values (see the noise note).

```bash
PYTHONPATH=../cellfs python -m chaosevo run --target cellfs --population 8 --generations 6 --seed 11 --out cellfs.json
```

| generation | best damage | mean damage | experiments run |
|---:|---:|---:|---:|
| 0 | 3.48 | 1.48 | 8 |
| 1 | 6.49 | 2.92 | 14 |
| 2 | 7.95 | 3.86 | 20 |
| 3 | 8.50 | 5.73 | 26 |
| 4 | 8.50 | 6.19 | 32 |
| 5 | 8.98 | 7.01 | 38 |

The mean climbs from 1.48 to 7.01: the whole population converges on damaging experiments.
The most damaging one it found (damage 8.98):

* `t=1.9s` **pause_node** `n2` for 6.9s
* `t=2.8s` **wipe_node** `n4` for 2.5s
* `t=4.1s` **kill_node** `n5` for 5.3s
* `t=6.5s` **kill_node** `n1` for 2.8s

Four overlapping outages. At the worst moment some object had **no intact copy on any live cell**
(`replica_margin_lost` = 1), 59% of the objects were down to their last copy, 27% of the workload's reads
failed and p99 read latency reached 3.0 s. Nothing was lost for good - killed cells come back with their
disks and cellfs healed itself - and that *is* the finding: the cluster survives this, but it is only a few
seconds of bad luck away from something worse. Replaying it later scored 9.04 (was 8.98), so the
measurement was reproducible there.

**What the search did not find.** As a sanity check of the fitness function, the known worst case was run
by hand: wiping three cells at the same instant. It scores **21.58** (`data_loss` = 1: one chunk is gone
for good), more than twice the best experiment the search found in 38 tries. A failure that needs three
faults aligned within a second or two is a needle in a haystack; a bigger budget (or a `seed_experiments`
hint) is how you reach it. A first search *without* the `at_risk_fraction` objective was worse - 18
experiments, best damage stuck at 2.01 - because "two cells down" and "three cells down" looked identical
to the fitness function. Giving the search credit for getting closer to the catastrophe is what got it out
of that plateau.

## Tests

```bash
pip install -e ".[test]"
python -m unittest discover -s tests -t . -v      # 65 tests, ~25 s: 62 pass, 3 skipped (cellfs tests need the cellfs project importable)
# or, with pytest:  pip install pytest && pytest -q     ->  62 passed, 3 skipped
PYTHONPATH=../cellfs python -m unittest tests.test_cellfs_target -v     # the 3 cellfs integration tests
```

Unit tests cover the genome, the GA (it beats random search by ~1.8x on a coordination landscape, is
deterministic per seed, never re-runs a genome, honours the cost penalty), fitness maths, Prometheus/
scrape parsing (against fake HTTP servers), the executor's schedule/revert guarantees, the exporter,
the Kubernetes target (against a fake Kubernetes API) and the CLI. `test_cellfs_target` runs a real
experiment against a real cellfs cluster (skipped if cellfs is not importable), and `test_deploy_files`
renders the Ansible templates with Jinja2 and checks they produce valid, least-privilege Kubernetes
objects whose embedded config chaosevo itself accepts (needs `jinja2` and `pyyaml`).

## Limitations and safety

* **Only attack what you own and are allowed to break.** Start with `simulated`, then a staging namespace.
  RBAC is namespaced on purpose; `reset()`/`close()` restore replicas and delete leftover chaos objects.
* **Kubernetes, Chaos Mesh, Ansible and Helm paths were validated statically and against a fake API, not
  on a live cluster.** Expect to adjust image names, Prometheus service URLs and Chaos Mesh runtime settings.
* **Noise.** Real systems are not deterministic: the same experiment can score differently twice.
  Elites are not re-evaluated inside a run; use `replay` to confirm a finding before acting on it.
* **Sequential evaluation.** One system under test means one experiment at a time, so a search costs
  `experiments x (window + recovery + reset)` of wall-clock time.
* **Needle landscapes.** If the worst failure needs several faults aligned in time (e.g. losing all three
  replicas of one object), a small budget may not find it. Objectives that reward *getting closer*
  (see `at_risk_fraction` in the cellfs target) give the search a gradient; expert `seed_experiments` help too.
* Faults are injected only inside `[0, 0.85 x window]`; timed faults are cut at the end of the window.

## Layout

```
chaosevo/  genome.py ga.py fitness.py executor.py sources.py probes.py exporter.py report.py cli.py target.py
           targets/{simulated,cellfs_target,kubernetes}.py
ansible/   site.yml + roles/chaosevo (defaults, tasks, Jinja2 templates)
grafana/   dashboards/chaosevo.json + provisioning        prometheus/  scrape config
observability/docker-compose.yml   Dockerfile   tests/
```
