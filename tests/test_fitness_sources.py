import json
import math
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from chaosevo.fitness import FitnessSpec, Objective
from chaosevo.sources import PrometheusSource, ScrapeSource, aggregate, parse_prometheus_text


class ObjectiveTests(unittest.TestCase):
    def test_up_objective_is_scaled_and_capped(self):
        o = Objective("err", weight=5, scale=0.5, cap=1.0)
        self.assertEqual(o.weight * o.normalise(0.25), 2.5)
        self.assertEqual(o.normalise(0.9), 1.0)            # capped
        self.assertEqual(o.normalise(-1), 0.0)              # never negative

    def test_down_objective_measures_the_drop_from_baseline(self):
        o = Objective("ready_ratio", weight=4, scale=1.0, direction="down", baseline=1.0)
        self.assertAlmostEqual(o.normalise(0.25), 0.75)
        self.assertEqual(o.normalise(1.2), 0.0)

    def test_score_sums_weighted_parts_and_skips_missing(self):
        spec = FitnessSpec.from_dicts([{"name": "a", "weight": 2, "scale": 1}, {"name": "b", "weight": 3, "scale": 10},
                                       {"name": "c", "weight": 9}])
        total, parts = spec.score({"a": 0.5, "b": 5.0, "c": math.nan})
        self.assertEqual(parts, {"a": 1.0, "b": 1.5})
        self.assertEqual(total, 2.5)

    def test_bad_direction_rejected(self):
        with self.assertRaises(ValueError):
            Objective.from_dict({"name": "x", "direction": "sideways"})


class AggregateTests(unittest.TestCase):
    def test_aggregations(self):
        pts = [(0, 1.0), (1, 3.0), (3, 3.0)]
        self.assertEqual(aggregate(pts, "max"), 3.0)
        self.assertEqual(aggregate(pts, "min"), 1.0)
        self.assertAlmostEqual(aggregate(pts, "mean"), 7 / 3)
        self.assertEqual(aggregate(pts, "last"), 3.0)
        self.assertEqual(aggregate(pts, "integral"), (1 + 3) / 2 * 1 + 3 * 2)
        self.assertTrue(math.isnan(aggregate([(0, math.nan)], "max")))


EXPOSITION = """# HELP cellfs_missing_replicas x
# TYPE cellfs_missing_replicas gauge
cellfs_missing_replicas{node="n1"} 2
cellfs_missing_replicas{node="n2"} 1
http_requests_total{code="200",path="/a b"} 90
http_requests_total{code="500"} 10 1712345678
weird line
"""


def serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ParsingAndSourcesTests(unittest.TestCase):
    def test_parse_text_format(self):
        samples = parse_prometheus_text(EXPOSITION)
        self.assertEqual(len(samples), 4)
        self.assertEqual(samples[2].labels, {"code": "200", "path": "/a b"})
        self.assertEqual(samples[3].value, 10.0)

    def test_scrape_source_sums_and_filters_and_survives_dead_endpoints(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = EXPOSITION.encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a): pass

        server = serve(H)
        try:
            src = ScrapeSource([f"http://127.0.0.1:{server.server_port}/metrics", "http://127.0.0.1:1/metrics"])
            self.assertEqual(src.query("cellfs_missing_replicas"), 3.0)
            self.assertEqual(src.query('http_requests_total{code="500"}'), 10.0)
            self.assertTrue(math.isnan(src.query("nope_total")))
            with self.assertRaises(ValueError):
                src.query("sum(rate(x[1m]))")
        finally:
            server.shutdown(); server.server_close()

    def test_prometheus_source_parses_the_http_api(self):
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                kind = "scalar" if "scalar" in self.path else "vector"
                result = [1.0, "7"] if kind == "scalar" else [{"metric": {}, "value": [1.0, "0.25"]}, {"metric": {}, "value": [1.0, "0.5"]}]
                body = json.dumps({"status": "success", "data": {"resultType": kind, "result": result}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a): pass

        server = serve(H)
        try:
            src = PrometheusSource(f"http://127.0.0.1:{server.server_port}", token="tok")
            self.assertEqual(src.query('sum(rate(http_requests_total{code=~"5.."}[30s]))'), 0.75)   # vector: summed
            self.assertEqual(src.query("scalar(1+6)"), 7.0)
            self.assertTrue(seen[0][0].startswith("/api/v1/query?query=sum%28rate"))
            self.assertEqual(seen[0][1], "Bearer tok")
        finally:
            server.shutdown(); server.server_close()

    def test_prometheus_error_status_raises(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"status": "error", "error": "bad query"}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a): pass

        server = serve(H)
        try:
            with self.assertRaises(RuntimeError):
                PrometheusSource(f"http://127.0.0.1:{server.server_port}").query("x")
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
