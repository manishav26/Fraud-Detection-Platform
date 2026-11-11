import math

from observability import snapshot


def sample(value, **labels):
    return {"metric": labels, "value": [1730000000, str(value)]}


ANSWERS = {
    "requests": [sample(6000, service="ingest-svc"), sample(6000, service="scoring-svc")],
    "errors": [sample(60, service="ingest-svc")],
    "p50": [sample(0.02, service="ingest-svc"), sample(0.01, service="scoring-svc")],
    "p95": [sample(0.8, service="ingest-svc"), sample("NaN", service="scoring-svc")],
    "p99": [sample(1.5, service="ingest-svc")],
    "decisions": [sample(5800, decision="approve"), sample(200, decision="review")],
    "degraded": [sample(120)],
    "breaker_samples": [sample(4)],
    "calls_not_ok": [sample(30, op="read", result="failed")],
    "skipped_writes": [sample(10)],
    "wait_scoring_p95": [sample(0.75)],
    "score_route_p95": [sample(0.7)],
    "steps_p95": [sample(0.05, step="predict")],
    "cpu_node_peak": [sample(2.71, node="k3d-fraud-server-0")],
    "cpu_pod_peak": [sample(0.4, pod="ingest-svc-a"), sample(0.9, pod="cassandra-0")],
    "kong_p95": [sample(820)],            # Kong reports milliseconds
    "kong_own_p95": [sample(3)],
    "kong_upstream_p95": [sample(810)],
    "ingest_steps_p95": [sample(0.004, step="publish_raw"), sample(0.75, step="wait_scoring")],
    "freshness_p95": [sample(1.8)],
    "feature_job_payments": [],           # no data: shown as a gap, written as null
}


def fake_query(seen):
    by_expr = {}

    def query(expr):
        seen.append(expr)
        return by_expr[expr]

    for name, expr in snapshot.QUERIES.items():
        by_expr[expr.format(w="600s")] = ANSWERS[name]
    return query


def test_every_query_uses_the_window():
    seen = []
    snapshot.collect(fake_query(seen), 600)
    assert len(seen) == len(snapshot.QUERIES)
    assert all("600s" in e for e in seen)
    assert not any("{w}" in e or "{{" in e for e in seen)


def test_collect_reads_each_answer():
    s = snapshot.collect(fake_query([]), 600)
    assert s["requests"] == {"ingest-svc": 6000, "scoring-svc": 6000}
    assert s["breaker_open_seconds"] == 20   # 4 samples, one per 5 s scrape
    assert s["calls_not_ok"] == {"read failed": 30}
    assert math.isnan(s["p95"]["scoring-svc"])


def test_render_shows_rates_shares_and_gaps():
    text = snapshot.render(snapshot.collect(fake_query([]), 600))
    assert "Window: the last 600 s (10.0 minutes)." in text
    assert "| ingest-svc | 6,000 | 10.0 | 20 ms | 800 ms | 1500 ms | 1.00% |" in text
    assert "| scoring-svc | 6,000 | 10.0 | 10 ms | – | – | 0.00% |" in text
    assert "| degraded (any decision) | 120 (2.0%) |" in text
    assert "| breaker open | about 20 s |" in text
    assert "| ingest waits for scoring | 750 ms |" in text
    assert "| k3d-fraud-server-0 | 2.71 |" in text
    assert text.index("| cassandra-0 | 0.90 |") < text.index("| ingest-svc-a | 0.40 |")


def test_render_without_cpu_figures_leaves_the_table_out():
    answers = dict(ANSWERS, cpu_node_peak=[], cpu_pod_peak=[])
    s = snapshot.collect(lambda e: answers[next(n for n, q in snapshot.QUERIES.items()
                                                if q.format(w="300s") == e)], 300)
    text = snapshot.render(s)
    assert "Peak CPU" not in text and "Busiest pods" not in text


def test_kong_milliseconds_become_seconds_like_the_rest():
    s = snapshot.collect(fake_query([]), 600)
    assert (s["kong_p95"], s["kong_own_p95"], s["kong_upstream_p95"]) == (0.82, 0.003, 0.81)
    assert s["ingest_steps_p95"] == {"publish_raw": 0.004, "wait_scoring": 0.75}


def test_render_shows_the_gateway_and_the_feature_job():
    text = snapshot.render(snapshot.collect(fake_query([]), 600))
    assert "| Kong itself (p95) | 3 ms |" in text
    assert "| Kong waiting for ingest (p95) | 810 ms |" in text
    assert "| ingest step: publish_raw (p95) | 4 ms |" in text
    assert "| feature job: txn.raw -> features written (p95) | 1.8 s |" in text
    assert "| feature job: payments handled | – |" in text


def test_json_writes_no_data_as_null():
    import json
    numbers = json.loads(snapshot.to_json(snapshot.collect(fake_query([]), 600)))
    assert numbers["feature_job_payments"] is None
    assert numbers["p95"]["scoring-svc"] is None and numbers["kong_p95"] == 0.82


def test_every_metric_a_ramp_step_needs_is_queried():
    text = " ".join(snapshot.QUERIES.values())
    assert all(m.removesuffix("_bucket") in text for m in snapshot.REQUIRED_METRICS)
