"""Where the damage is measured: a Prometheus server, or /metrics endpoints scraped directly."""
from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

_SAMPLE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)(?:\s+\d+)?$')
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Dict[str, str]
    value: float


def parse_prometheus_text(text: str) -> List[Sample]:
    """Parse the Prometheus text exposition format (the subset every exporter uses)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        out.append(Sample(m.group(1), dict(_LABEL_RE.findall(m.group(2) or "")), value))
    return out


class ScrapeSource:
    """Reads /metrics endpoints directly and sums a metric across them - handy when there is no
    Prometheus server (local experiments). The query is a metric name with optional label
    filters, e.g.  cellfs_missing_replicas  or  http_requests_total{code="500"}."""

    def __init__(self, urls: Sequence[str], timeout: float = 2.0) -> None:
        self.urls, self.timeout = list(urls), timeout

    def query(self, expr: str) -> float:
        m = re.match(r'^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?\s*$', expr)
        if not m:
            raise ValueError(f"scrape queries are 'metric{{label=\"v\"}}', got {expr!r}")
        name, want = m.group(1), dict(_LABEL_RE.findall(m.group(2) or ""))
        total, seen = 0.0, False
        for url in self.urls:
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    samples = parse_prometheus_text(resp.read().decode())
            except OSError:
                continue                      # an endpoint that is down simply contributes nothing
            for s in samples:
                if s.name == name and all(s.labels.get(k) == v for k, v in want.items()):
                    total, seen = total + s.value, True
        return total if seen else math.nan


class PrometheusSource:
    """Runs PromQL instant queries against a Prometheus server (HTTP API v1). If a query returns
    several series they are summed - write the aggregation you want in the PromQL itself."""

    def __init__(self, url: str, timeout: float = 5.0, token: Optional[str] = None) -> None:
        self.url, self.timeout, self.token = url.rstrip("/"), timeout, token

    def query(self, expr: str) -> float:
        req = urllib.request.Request(f"{self.url}/api/v1/query?" + urllib.parse.urlencode({"query": expr}))
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.load(resp)
        if body.get("status") != "success":
            raise RuntimeError(f"prometheus error: {body.get('error', body)}")
        data = body["data"]
        if data["resultType"] == "scalar":
            return float(data["result"][1])
        values = [float(r["value"][1]) for r in data["result"]]
        values = [v for v in values if not math.isnan(v)]
        return sum(values) if values else math.nan


@dataclass
class Signal:
    """A named measurement taken repeatedly during an experiment and collapsed with `agg`."""
    name: str
    query: str
    source: object            # anything with .query(str) -> float
    agg: str = "max"

    def read(self) -> float:
        return float(self.source.query(self.query))


def aggregate(series: Sequence[Tuple[float, float]], how: str) -> float:
    pts = [(t, v) for t, v in series if not math.isnan(v)]
    if not pts:
        return math.nan
    values = [v for _, v in pts]
    if how == "max":
        return max(values)
    if how == "min":
        return min(values)
    if how == "mean":
        return sum(values) / len(values)
    if how == "last":
        return values[-1]
    if how == "integral":                     # area under the curve (value x seconds), trapezoid rule
        return sum((pts[i][1] + pts[i - 1][1]) / 2 * (pts[i][0] - pts[i - 1][0]) for i in range(1, len(pts)))
    raise ValueError(f"unknown aggregation {how!r}")
