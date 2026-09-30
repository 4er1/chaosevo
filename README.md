# ChaosEvo

![CI](https://github.com/41R3/chaosevo/actions/workflows/ci.yml/badge.svg)

**Evolutionary chaos engineering for finding high-impact failure scenarios.**

ChaosEvo uses a **genetic algorithm** to search for combinations of faults that cause the most damage to a system, replacing manual guesswork with automated evolutionary search.

> **Safety:** Start with the built-in `simulated` target. Only run real experiments against systems you own or are explicitly authorized to disrupt.

---

## Why ChaosEvo?

Instead of manual trial-and-error (e.g., *"kill three pods and see what happens"*), ChaosEvo reverses the workflow: 
> *"Given the faults I am allowed to inject, which experiment causes the most damage?"*

It automatically explores complex, overlapping failure conditions (timing, multi-fault interactions) that are hard to design by hand.

---

## Key Features

- **Genetic Search:** Automatically generates, crosses, and mutates timed sequences of 1–4 faults.
- **Smart Fitness Function:** Balances **damage** against **cost** (blast radius), favoring smaller experiments that expose critical weaknesses.
- **Flexible Targets:** Supports `simulated` (in-memory model), `cellfs` (multi-cell cluster), and `Kubernetes`.
- **Diverse Signals:** Integrates with Prometheus, `/metrics` endpoints, HTTP probes, and target-specific checks.
- **Robust Testing:** Includes 62 passing unit/integration tests and automated CI/CD workflows.

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
```bash
python -m chaosevo run \
  --target simulated \
  --generations 8 \
  --population 12 \
  --seed 1 \
  --out results.json
```

### 3. Generate Report & Replay
```bash
# Generate Markdown report
python -m chaosevo report results.json

# Replay the top-ranked experiment
python -m chaosevo replay results.json --rank 1
```

---

## Safety Model

- Start with the local `simulated` target.
- Use staging namespaces before testing on production Kubernetes clusters.
- Ensure all automated cleanup (`reset()`, `close()`) functions properly.