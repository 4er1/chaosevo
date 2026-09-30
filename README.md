# ChaosEvo

![CI](https://github.com/41R3/chaosevo/actions/workflows/ci.yml/badge.svg)

**Evolutionary chaos engineering for finding high-impact failure scenarios.**

ChaosEvo uses a **genetic algorithm** to automatically search for combinations of faults that cause the most damage to a system, replacing manual guesswork with an automated evolutionary search loop.

> **Safety:** Start with the built-in `simulated` target. Only run real experiments against systems you own or are explicitly authorized to disrupt.

---

## Why ChaosEvo?

Traditional chaos testing usually starts with a manual scenario:
> *"Kill three pods and see what happens."*

ChaosEvo reverses that workflow:
> *"Given the faults I am allowed to inject, which experiment causes the most damage?"*

Failures often depend on **timing and combinations**. A single fault may be harmless, but overlapping faults can expose critical system weaknesses. ChaosEvo searches this combinatorial space automatically.

---

## How It Works

```text
  Genetic Algorithm (Select / Cross / Mutate)
         │
         ▼
  Experiment Executor (Reset -> Inject -> Measure -> Revert)
         │
         ▼
  Target System (Simulated / CellFS / Kubernetes)
         │
         ▼
  Damage & Cost Measurements (Prometheus / HTTP probes / Metrics)
         │
         ▼
  Fitness Score ──► Next Generation
```

The search optimization balances **damage** against **cost** (blast radius):
$$\text{fitness} = \text{damage} - \text{cost}$$
This guides the algorithm toward small experiments that still reveal serious weaknesses.

---

## Key Features

- **Genetic Search:** Automatically schedules, crosses, and mutates timed sequences of 1–4 faults.
- **Multiple Targets:** Supports an in-memory `simulated` model, `cellfs` multi-cell clusters, and native `Kubernetes` (with optional Chaos Mesh).
- **Flexible Observability:** Integrates with Prometheus instant queries, `/metrics` scrapers, and HTTP probes.
- **Robust Testing:** Includes 62 unit/integration tests and automated CI/CD workflows.

---

## Quick Start

### Requirements
- Python 3.10+
- Docker (optional, for Prometheus/Grafana stack)

### 1. Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

### 2. Run the Safe Simulator
This runs entirely in memory without touching real infrastructure:
```bash
python -m chaosevo run \
  --target simulated \
  --generations 8 \
  --population 12 \
  --seed 1 \
  --out results.json
```

### 3. Generate a Report & Replay Results
```bash
# Generate a Markdown summary report
python -m chaosevo report results.json

# Replay the top-ranked dangerous experiment
python -m chaosevo replay results.json --rank 1
```

---

## Configuration Example

ChaosEvo uses a JSON configuration file to define targets, objectives, and genetic algorithm parameters:

```json
{
  "target": {
    "type": "simulated"
  },
  "experiment": {
    "window": 40,
    "recovery": 30,
    "sample_interval": 2
  },
  "ga": {
    "population": 10,
    "generations": 8,
    "cost_weight": 0.3,
    "seed": 1
  },
  "objectives": [
    {"name": "error_rate", "weight": 5, "scale": 0.5},
    {"name": "p99_latency_s", "weight": 2, "scale": 2.0}
  ]
}
```

---

## Running Tests

Run the test suite using pytest or standard unittest:
```bash
pytest -q
```

---

## Safety Model

1. **Start safe:** Always begin testing with the `simulated` target.
2. **Use staging:** When moving to Kubernetes, isolate experiments in dedicated staging namespaces.
3. **Clean execution:** Ensure automated `reset()` and cleanup hooks are correctly configured.

---

## License

Add the project's license here.