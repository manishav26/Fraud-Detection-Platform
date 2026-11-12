"""One load run's numbers from Prometheus, as markdown tables for docs/results. Design: docs/metrics.md

    python -m observability.snapshot --minutes 10            # the last 10 minutes
    python -m observability.snapshot --minutes 10 --end 2026-11-05T01:30:00Z
    python -m observability.snapshot --seconds 607 --end 1793900000   # an exact run (baseline.sh)

On the cluster: make k8s-snapshot MINUTES=10 (through a tunnel). The numbers cover the whole window,
so start it when the run starts and take it right after the run ends (or pass --end).
"""

import argparse
import json
import math
import urllib.parse
import urllib.request

PROMETHEUS = "http://localhost:9090"
STEP = "5s"   # the scrape interval: one sample per scrape for the breaker's open time

# {w} is the window, e.g. 10m. increase() over the window copes with pods restarting.
QUERIES = {
    "requests": "sum by (service) (increase(http_requests_total[{w}]))",
    "errors": 'sum by (service) (increase(http_requests_total{{status=~"5.."}}[{w}]))',
    "p50": "histogram_quantile(0.5, sum by (le, service) (increase(http_request_duration_seconds_bucket[{w}])))",
    "p95": "histogram_quantile(0.95, sum by (le, service) (increase(http_request_duration_seconds_bucket[{w}])))",
    "p99": "histogram_quantile(0.99, sum by (le, service) (increase(http_request_duration_seconds_bucket[{w}])))",
    "decisions": "sum by (decision) (increase(scoring_decisions_total[{w}]))",
    # A labelled series exists only once incremented: with no degraded answer ever, there is no
    # degraded="true" series and the sum is empty. That means 0, but only if scoring's decisions are
    # counted at all (the `or` side), so a scoring service that reports nothing stays "no data".
    "degraded": ('sum(increase(scoring_decisions_total{{degraded="true"}}[{w}]))'
                 ' or 0 * sum(increase(scoring_decisions_total[{w}]))'),
    "breaker_samples": f"sum_over_time((max(history_breaker_open))[{{w}}:{STEP}])",
    "calls_not_ok": 'sum by (op, result) (increase(history_calls_total{{result!="ok"}}[{w}]))',
    "skipped_writes": "sum(increase(history_skipped_writes_total[{w}]))",
    "wait_scoring_p95": 'histogram_quantile(0.95, sum by (le) (increase(ingest_step_seconds_bucket{{step="wait_scoring"}}[{w}])))',
    "score_route_p95": 'histogram_quantile(0.95, sum by (le) (increase(http_request_duration_seconds_bucket{{service="scoring-svc", route="/score"}}[{w}])))',
    "steps_p95": "histogram_quantile(0.95, sum by (le, step) (increase(scoring_step_seconds_bucket[{w}])))",
    "cpu_node_peak": "max_over_time((sum by (node) (rate(container_cpu_usage_seconds_total{{id=\"/\"}}[1m])))[{w}:15s])",
    # The pods that used the most CPU at their busiest, to see what drives a node's peak
    "cpu_pod_peak": 'topk(5, max_over_time((sum by (pod) (rate(container_cpu_usage_seconds_total{{namespace="fraud", container!=""}}[1m])))[{w}:15s]))',
    # Kong's own share of each request and the time upstream (ingest), in ms (Kong 3.x plugin,
    # latency_metrics on); all Kong routes, which during a load run are almost all payments
    "kong_p95": "histogram_quantile(0.95, sum by (le) (increase(kong_request_latency_ms_bucket[{w}])))",
    "kong_own_p95": "histogram_quantile(0.95, sum by (le) (increase(kong_kong_latency_ms_bucket[{w}])))",
    "kong_upstream_p95": "histogram_quantile(0.95, sum by (le) (increase(kong_upstream_latency_ms_bucket[{w}])))",
    "ingest_steps_p95": "histogram_quantile(0.95, sum by (le, step) (increase(ingest_step_seconds_bucket[{w}])))",
    # Feature job: txn.raw -> features written, and how many payments it handled
    "freshness_p95": "histogram_quantile(0.95, sum by (le) (increase(feature_job_delay_seconds_bucket[{w}])))",
    "feature_job_payments": "sum(increase(feature_job_payments_total[{w}]))",
}

# What a ramp step needs to exist before it starts (load/ramp.py checks): without one of these a
# hop would show "–" instead of a number
REQUIRED_METRICS = ("http_request_duration_seconds_bucket", "scoring_step_seconds_bucket",
                    "ingest_step_seconds_bucket", "kong_request_latency_ms_bucket",
                    "kong_kong_latency_ms_bucket", "kong_upstream_latency_ms_bucket",
                    "feature_job_delay_seconds_bucket", "feature_job_payments_total",
                    "scoring_decisions_total")


def prometheus_query(url, end=None):
    """A function that runs one PromQL query at `end` (default now) and returns its vector."""
    def query(expr):
        params = {"query": expr}
        if end:
            params["time"] = end
        with urllib.request.urlopen(f"{url}/api/v1/query?{urllib.parse.urlencode(params)}",
                                    timeout=30) as r:
            body = json.load(r)
        if body["status"] != "success":
            raise RuntimeError(f"Prometheus refused {expr}: {body}")
        return body["data"]["result"]
    return query


def by_label(vector, label):
    """{label value: number}; a single unlabelled result is keyed by ''."""
    return {s["metric"].get(label, ""): float(s["value"][1]) for s in vector}


def collect(query, seconds):
    """The numbers for the `seconds` before the query time (a whole number of seconds)."""
    w = f"{seconds}s"
    r = {name: query(expr.format(w=w)) for name, expr in QUERIES.items()}

    def one(name):
        return by_label(r[name], "").get("", math.nan)

    return {
        "seconds": seconds,
        "requests": by_label(r["requests"], "service"),
        "errors": by_label(r["errors"], "service"),
        "p50": by_label(r["p50"], "service"),
        "p95": by_label(r["p95"], "service"),
        "p99": by_label(r["p99"], "service"),
        "decisions": by_label(r["decisions"], "decision"),
        "degraded": one("degraded"),
        "breaker_open_seconds": one("breaker_samples") * 5,
        "calls_not_ok": {f"{s['metric']['op']} {s['metric']['result']}": float(s["value"][1])
                         for s in r["calls_not_ok"]},
        "skipped_writes": one("skipped_writes"),
        "wait_scoring_p95": one("wait_scoring_p95"),
        "score_route_p95": one("score_route_p95"),
        "steps_p95": by_label(r["steps_p95"], "step"),
        "cpu_node_peak": by_label(r["cpu_node_peak"], "node"),
        "cpu_pod_peak": by_label(r["cpu_pod_peak"], "pod"),
        "kong_p95": one("kong_p95") / 1000,   # ms -> s, like everything else here
        "kong_own_p95": one("kong_own_p95") / 1000,
        "kong_upstream_p95": one("kong_upstream_p95") / 1000,
        "ingest_steps_p95": by_label(r["ingest_steps_p95"], "step"),
        "freshness_p95": one("freshness_p95"),
        "feature_job_payments": one("feature_job_payments"),
    }


def to_json(numbers):
    """JSON with no data (NaN) as null, so other tools can read it."""
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        return None if isinstance(v, float) and math.isnan(v) else v
    return json.dumps(clean(numbers), indent=2)


def ms(seconds):
    return "–" if math.isnan(seconds) else f"{seconds * 1000:.0f} ms"


def count(n):
    return "–" if math.isnan(n) else f"{n:,.0f}"


def render(s):
    seconds = s["seconds"]
    out = [f"Window: the last {seconds} s ({seconds / 60:.1f} minutes).", "",
           "| Service | Requests | Per second | p50 | p95 | p99 | 5xx |", "|---|---|---|---|---|---|---|"]
    for svc in sorted(s["requests"]):
        n = s["requests"][svc]
        errors = s["errors"].get(svc, 0.0)
        share = f"{errors / n:.2%}" if n else "–"
        out.append(f"| {svc} | {count(n)} | {n / seconds:.1f} | {ms(s['p50'].get(svc, math.nan))} | "
                   f"{ms(s['p95'].get(svc, math.nan))} | {ms(s['p99'].get(svc, math.nan))} | {share} |")

    total = sum(s["decisions"].values())
    degraded = s["degraded"] if not math.isnan(s["degraded"]) else 0.0
    out += ["", "| Decisions | Count |", "|---|---|"]
    out += [f"| {d} | {count(n)} |" for d, n in sorted(s["decisions"].items())]
    out.append(f"| degraded (any decision) | {count(degraded)}"
               f"{f' ({degraded / total:.1%})' if total else ''} |")

    out += ["", "| Cassandra | Value |", "|---|---|",
            f"| breaker open | about {s['breaker_open_seconds']:.0f} s |"
            if not math.isnan(s["breaker_open_seconds"]) else "| breaker open | – |"]
    out += [f"| {k} | {count(n)} |" for k, n in sorted(s["calls_not_ok"].items())]
    out.append(f"| skipped writes | {count(s['skipped_writes'])} |")

    out += ["", "| Where ingest's wait goes (p95) | Time |", "|---|---|",
            f"| ingest waits for scoring | {ms(s['wait_scoring_p95'])} |",
            f"| scoring answers /score (queue + work) | {ms(s['score_route_p95'])} |"]
    out += [f"| scoring step: {k} | {ms(v)} |" for k, v in sorted(s["steps_p95"].items())]

    out += ["", "| Gateway and feature job | Value |", "|---|---|",
            f"| Kong, whole request (p95) | {ms(s['kong_p95'])} |",
            f"| Kong itself (p95) | {ms(s['kong_own_p95'])} |",
            f"| Kong waiting for ingest (p95) | {ms(s['kong_upstream_p95'])} |"]
    out += [f"| ingest step: {k} (p95) | {ms(v)} |" for k, v in sorted(s["ingest_steps_p95"].items())]
    fresh = s["freshness_p95"]
    fresh_text = "–" if math.isnan(fresh) else f"{fresh:.1f} s"
    out += [f"| feature job: txn.raw -> features written (p95) | {fresh_text} |",
            f"| feature job: payments handled | {count(s['feature_job_payments'])} |"]

    if s["cpu_node_peak"]:
        out += ["", "| Node | Peak CPU (cores, 1-minute average) |", "|---|---|"]
        out += [f"| {node} | {v:.2f} |" for node, v in sorted(s["cpu_node_peak"].items())]
    if s["cpu_pod_peak"]:
        out += ["", "| Busiest pods | Peak CPU (cores, 1-minute average) |", "|---|---|"]
        out += [f"| {pod} | {v:.2f} |"
                for pod, v in sorted(s["cpu_pod_peak"].items(), key=lambda kv: -kv[1])]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=PROMETHEUS)
    window = ap.add_mutually_exclusive_group()
    window.add_argument("--minutes", type=int, default=10)
    window.add_argument("--seconds", type=int, help="an exact window, e.g. a load run's length")
    ap.add_argument("--end", help="end of the window (RFC 3339 or unix seconds); default now")
    ap.add_argument("--json", help="also write the numbers here, for load/ramp.py")
    args = ap.parse_args()
    seconds = args.seconds or args.minutes * 60
    numbers = collect(prometheus_query(args.url, args.end), seconds)
    if args.json:
        with open(args.json, "w") as f:
            f.write(to_json(numbers))
    print(render(numbers))


if __name__ == "__main__":
    main()
