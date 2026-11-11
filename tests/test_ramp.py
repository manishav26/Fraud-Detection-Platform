"""The ramp test (load/ramp.py, scripts/ramp_test.sh; docs/results/ramp.md)."""
import csv
import json
import math
import pathlib
import re
import subprocess
from itertools import pairwise

from load import ramp

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ramp_test.sh"


def write_run(path, rows):
    """rows: (sent_s, status, latency_ms)"""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "user_id", "status", "decision", "score", "latency_ms", "extra", "sent_s"])
        for i, (sent, status, ms) in enumerate(rows):
            w.writerow([f"t{i}", "u", status, "approve", 0.1, ms, "", sent])


PROM_OK = {"p95": {"ingest-svc": 0.05}, "decisions": {"approve": 990, "review": 10}, "degraded": 2}


def test_only_the_measured_window_counts(tmp_path):
    # 60 s of slow warm-up, then 120 s at 10/s and 20 ms
    rows = [(s / 10, "200", 900.0) for s in range(600)] + \
           [(60 + s / 10, "200", 20.0) for s in range(1200)]
    write_run(tmp_path / "run.csv", rows)
    c = ramp.client_window(tmp_path / "run.csv", 60, 180)
    assert c["sent"] == 1200 and c["rate"] == 10 and c["p95_ms"] == 20


def test_errors_are_5xx_and_no_answer_not_4xx(tmp_path):
    write_run(tmp_path / "run.csv", [(1, "200", 10), (2, "503", 10), (3, "error", 10), (4, "429", 10)])
    c = ramp.client_window(tmp_path / "run.csv", 0, 10)
    assert c["errors"] == 2 and c["answered"] == 1


def client(rate=100.0, p95=100.0, p99=300.0, errors=0.0):
    return {"rate": rate, "p95_ms": p95, "p99_ms": p99, "errors_share": errors, "sent": 12000}


def test_a_step_within_every_target_passes():
    assert ramp.judge(client(), PROM_OK, 100) == (ramp.PASS, [])


def test_each_target_can_stop_the_ramp():
    assert ramp.judge(client(p95=251), PROM_OK, 100)[0] == ramp.MISSED
    assert ramp.judge(client(p99=1501), PROM_OK, 100)[0] == ramp.MISSED
    assert ramp.judge(client(errors=0.001), PROM_OK, 100)[0] == ramp.MISSED
    assert ramp.judge(client(), {**PROM_OK, "p95": {"ingest-svc": 0.101}}, 100)[0] == ramp.MISSED
    verdict, missed = ramp.judge(client(), {**PROM_OK, "degraded": 11}, 100)
    assert verdict == ramp.MISSED and missed[0].startswith("degraded_share")


def test_missing_data_is_a_miss_not_a_pass():
    verdict, missed = ramp.judge(client(), {**PROM_OK, "p95": {}}, 100)
    assert verdict == ramp.MISSED and "ingest_p95_ms no data" in missed[0]


def test_a_rate_the_sender_did_not_reach_is_not_a_platform_result():
    verdict, missed = ramp.judge(client(rate=94.9, p95=9999), PROM_OK, 100)
    assert verdict == ramp.SENDER_LIMITED and "94.9/s of 100/s" in missed[0]


def test_copies_are_the_most_ready_in_the_window(tmp_path):
    (tmp_path / "copies.csv").write_text("time,desired,current,ready\n100,2,2,2\n130,3,3,3\n200,4,4,4\n")
    assert ramp.copies(tmp_path / "copies.csv", 100, 150) == 3
    assert ramp.copies(tmp_path / "missing.csv", 0, 1) is None


def test_the_report_has_a_row_per_step_in_rate_order(tmp_path):
    for rate, verdict in ((50, ramp.PASS), (25, ramp.PASS), (75, ramp.MISSED)):
        d = tmp_path / str(rate)
        d.mkdir()
        (d / "step.json").write_text(json.dumps({
            "rps": rate, "verdict": verdict, "copies": 2,
            "missed": ["p95_ms 300 (target 250)"] if verdict else [],
            "client": {**client(rate=rate, p95=300 if verdict else 90), "p50_ms": 20}}))
        (d / "prometheus.json").write_text(json.dumps({
            **PROM_OK, "kong_p95": 0.09, "kong_own_p95": 0.002, "kong_upstream_p95": 0.088,
            "ingest_steps_p95": {"publish_raw": 0.004}, "steps_p95": {"history_read": 0.01},
            "score_route_p95": 0.03, "freshness_p95": 1.5, "feature_job_payments": 12000}))
    text = ramp.report(tmp_path)
    rows = [line for line in text.splitlines() if re.match(r"\| \d+ \| \d", line)]
    assert [r.split("|")[1].strip() for r in rows[:3]] == ["25", "50", "75"]
    assert "missed: p95_ms 300 (target 250)" in rows[2]
    assert "| 25 | 90 | 90 | 2 | 88 | 50 | 4 |" in text   # the hop table
    assert "| 1.5 | 1.00 |" in text                       # fresh, and the job kept up


def test_preflight_names_what_prometheus_lacks():
    have = {"count(scoring_step_seconds_bucket)"}
    missing = ramp.preflight(lambda q: [1] if q in have else [])
    assert "scoring_step_seconds_bucket" not in missing and "kong_kong_latency_ms_bucket" in missing


def plan(**env):
    return subprocess.run(["bash", str(SCRIPT), "plan"], capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", **env})


def test_the_steps_use_their_own_rows():
    out = plan()
    assert out.returncode == 0, out.stderr
    steps = [(int(a), int(b), int(c)) for a, b, c in re.findall(r"(\d+)/s: rows (\d+) \+(\d+)", out.stdout)]
    assert [s[0] for s in steps] == [25, 50, 75, 100, 150, 200]
    assert steps[0][1] == 2000 and steps[-1][1] + steps[-1][2] <= 110_000
    assert all(a[1] + a[2] == b[1] for a, b in pairwise(steps))


def test_steps_past_the_rows_are_refused_before_anything_is_sent():
    out = plan(RATES="25 50 75 100 150 200 250")
    assert out.returncode != 0 and "this test has 2000-110000" in out.stderr
    assert plan(MEASURE="180").returncode != 0   # the measured part must leave a warm-up


def test_nan_numbers_render_as_gaps():
    assert ramp.ms(None) == "–" and ramp.ms(math.nan) == "–" and ramp.ms(0.0123) == "12"


def test_zero_degraded_passes_and_a_gap_renders_as_a_gap():
    assert ramp.judge(client(), {**PROM_OK, "degraded": 0}, 100) == (ramp.PASS, [])
    assert ramp.share(math.nan) == "–" and ramp.share(None) == "–" and ramp.share(0.0) == "0.00%"
