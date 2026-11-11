"""The scaling and failure tests (load/scaling.py, load/failures.py and their scripts)."""
import csv
import json
import math
import pathlib
import re
import subprocess

import pytest

from load import failures, scaling

ROOT = pathlib.Path(__file__).resolve().parents[1]


def write_run(path, rows):
    """rows: (sent_s, status, latency_ms, degraded)"""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["txn_id", "user_id", "status", "decision", "score", "latency_ms", "extra",
                    "sent_s", "degraded"])
        for i, (sent, status, ms, degraded) in enumerate(rows):
            w.writerow([f"t{i}", "u", status, "approve", 0.1, ms, "", sent, degraded])


# Scaling

def test_throughput_is_measured_in_the_steady_middle(tmp_path):
    # a slow start (10/s for 10 s), then 100/s for 80 s
    rows = [(i / 10, "200", 10, False) for i in range(100)] + \
           [(10 + i / 100, "200", 10, False) for i in range(8000)]
    write_run(tmp_path / "run.csv", rows)
    t = scaling.throughput(tmp_path / "run.csv")
    assert t["rate"] == pytest.approx(100, rel=0.02) and t["p95_ms"] == 10


def test_senders_add_up_on_one_clock(tmp_path):
    # two senders, each answering 50/s for 100 s; the second started 0.5 s later
    parts = []
    for i, started in ((0, 1000.0), (1, 1000.5)):
        write_run(tmp_path / f"run-{i}.csv", [(k / 50, "200", 10, False) for k in range(5000)])
        parts.append((tmp_path / f"run-{i}.csv", started))
    assert scaling.senders_throughput(parts)["rate"] == pytest.approx(100, rel=0.02)


def test_efficiency_is_t_n_over_n_times_t_1():
    assert scaling.efficiency({1: 100, 2: 180, 4: 300}) == {1: 1.0, 2: 0.9, 4: 0.75}
    assert math.isnan(scaling.efficiency({2: 180})[2])   # no 1-copy run, no efficiency


def test_scaling_report_lists_copies_in_order(tmp_path):
    for n, rate in ((4, 300), (1, 100), (2, 180)):
        d = tmp_path / str(n)
        d.mkdir()
        write_run(d / "run-0.csv", [(i / rate, "200", 20, False) for i in range(rate * 60)])
        (d / "client-0.txt").write_text("Sent ...\nStarted sending at unix 1000.0\n")
        (d / "prometheus.json").write_text(json.dumps(
            {"score_route_p95": 0.015, "steps_p95": {"history_read": 0.006},
             "cpu_pod_peak": {"cassandra-0": 0.9, f"scoring-{n}": 1.0}}))
    text = scaling.report(tmp_path)
    rows = [line for line in text.splitlines() if re.match(r"\| \d \|", line)]
    assert [r.split("|")[1].strip() for r in rows] == ["1", "2", "4"]
    assert "| 0.90 |" in rows[1] and "| 0.75 |" in rows[2]
    assert "scoring-4 1.00, cassandra-0 0.90" in rows[2]


# Failures

def failure_run(tmp_path, name, kind, rows, killed_at=60.0, back_after=20.0, started=1000.0):
    d = tmp_path / name
    d.mkdir()
    write_run(d / "run.csv", rows)
    (d / "client.txt").write_text(f"Sent ...\nStarted sending at unix {started}\n")
    (d / "events.json").write_text(json.dumps({
        "kind": kind, "target": f"{kind}-pod", "killed": started + killed_at,
        "back": started + killed_at + back_after}))
    return d


def steady(seconds=240, rate=10):
    return [(i / rate, "200", 20, False) for i in range(seconds * rate)]


def test_a_kill_nobody_notices_costs_no_client_time(tmp_path):
    r = failures.analyse(failure_run(tmp_path, "scoring-1", "scoring", steady()))
    assert (r["affected"], r["client_mttr_s"], r["back_s"]) == (0, 0.0, 20.0)


def test_clients_feel_it_until_the_last_noticed_answer(tmp_path):
    rows = steady()
    # an outage from 60 s: errors until 75 s, then degraded answers until 80 s (answered 0.5 s later)
    rows = [(s, "503", 500, False) if 60 <= s < 75 else
            (s, "200", 500, True) if 75 <= s < 80 else (s, st, ms, d) for s, st, ms, d in rows]
    r = failures.analyse(failure_run(tmp_path, "redpanda-1", "redpanda", rows))
    assert r["errors"] == 150 and r["degraded"] == 50
    assert r["client_mttr_s"] == pytest.approx(80 - 0.1 + 0.5 - 60, abs=0.01)


def test_problems_before_the_kill_are_kept_apart(tmp_path):
    rows = [(s, "200", 20, s < 5) for s, _, _, _ in steady()]   # degraded in the first 5 s only
    r = failures.analyse(failure_run(tmp_path, "cassandra-1", "cassandra", rows))
    assert r["affected_before"] == 50 and r["affected"] == 0


def test_availability_is_mtbf_over_mtbf_plus_mttr():
    assert failures.availability(0) == 1.0
    assert failures.availability(36, mtbf_hours=1) == pytest.approx(3600 / 3636)


def test_the_report_multiplies_the_kinds_for_the_whole_platform(tmp_path):
    rows = steady()
    failure_run(tmp_path, "scoring-1", "scoring", steady())
    failure_run(tmp_path, "redpanda-1", "redpanda",
                [(s, "503", 20, False) if 60 <= s < 96 else (s, st, ms, d) for s, st, ms, d in rows])
    text = failures.report(tmp_path, mtbf_hours=1)
    assert "| scoring | 1 | 0.0 | 100.00000% |" in text
    redpanda = next(line for line in text.splitlines() if line.startswith("| redpanda |"))
    assert "| redpanda | 1 | 35.9 |" in redpanda   # last failure sent at 95.9 s, answered 20 ms later
    assert "**all of them**" in text


# The scripts

def run(*args, **env):
    return subprocess.run(["bash", *args], capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", **env}, cwd=ROOT)


def test_scaling_runs_use_their_own_rows():
    out = run("scripts/scaling_test.sh", "plan")
    assert out.returncode == 0, out.stderr
    runs = [(int(n), int(a), int(b)) for n, a, b in re.findall(r"(\d) copies: rows (\d+) \+(\d+)", out.stdout)]
    assert [r[0] for r in runs] == [1, 2, 4] and runs[0][1] == 110_000
    assert runs[-1][1] + runs[-1][2] <= 167_240   # the consistency test's rows start there
    assert run("scripts/scaling_test.sh", "plan", COUNT="20000").returncode != 0


@pytest.mark.parametrize("args, env", [(("bogus", "1"), {}), (("scoring", "x"), {}),
                                       (("scoring", "1"), {"KILL_AT": "200"})])
def test_failure_runs_refuse_bad_arguments_before_touching_the_cluster(args, env):
    out = run("scripts/failure_test.sh", *args, **env)
    assert out.returncode != 0 and "kubectl" not in out.stderr


def test_the_scaling_test_always_puts_the_hpa_back():
    text = (ROOT / "scripts/scaling_test.sh").read_text()
    assert "trap restore EXIT" in text and "minReplicas\\\":${min:-1}" in text


def test_st7_alternates_b_and_c_for_every_repeat():
    text = (ROOT / "security/tests/st7_flood.py").read_text()
    assert '"--repeats"' in text and 'plan += [("B", UNDER_LIMIT' in text
    assert "B runs ranged" in text   # identical runs' spread, to tell a real difference from noise


# The VM's own CPU (the cluster and the senders together)

def test_cpu_times_count_idle_and_iowait_as_not_busy():
    # user nice system idle iowait irq softirq steal
    assert scaling.cpu_times("cpu  100 0 50 800 50 0 0 0\ncpu0 ...\n") == (150, 1000)


def test_cpu_log_writes_busy_cores(tmp_path, monkeypatch):
    stat = tmp_path / "stat"
    readings = iter(["cpu 0 0 0 100 0", "cpu 50 0 0 150 0", "cpu 150 0 0 150 0"])
    real = scaling.Path.read_text
    monkeypatch.setattr(scaling.Path, "read_text",
                        lambda self, *a, **k: next(readings) if self == stat else real(self, *a, **k))
    monkeypatch.setattr(scaling.os, "cpu_count", lambda: 8)
    scaling.cpu_log(tmp_path / "cpu.csv", every=0, stat=stat, rounds=2)
    rows = list(csv.DictReader((tmp_path / "cpu.csv").read_text().splitlines()))
    assert [float(r["busy_cores"]) for r in rows] == [4.0, 8.0]   # half busy, then all busy
    assert rows[0]["cores"] == "8"


def test_vm_cpu_is_the_mean_and_peak_in_the_window(tmp_path):
    (tmp_path / "cpu.csv").write_text("time,busy_cores,cores\n90,7.9,8\n100,4.0,8\n102,6.0,8\n200,8.0,8\n")
    assert scaling.vm_cpu(tmp_path / "cpu.csv", 100, 150) == (5.0, 6.0, 8)
    assert scaling.vm_cpu(tmp_path / "none.csv", 0, 1) is None


def test_the_report_shows_how_full_the_vm_was(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    write_run(d / "run-0.csv", [(i / 100, "200", 20, False) for i in range(6000)])
    (d / "client-0.txt").write_text("Sent ...\nStarted sending at unix 1000.0\n")
    (d / "window.json").write_text('{"start": 1000, "end": 1060}')
    (tmp_path / "vm_cpu.csv").write_text("time,busy_cores,cores\n1010,7.0,16\n1020,9.0,16\n")
    assert "| 8.0 / 9.0 of 16 cores |" in scaling.report(tmp_path)


def test_scaling_holds_ingest_and_puts_it_back():
    text = (ROOT / "scripts/scaling_test.sh").read_text()
    assert 'scale deploy/ingest-svc --replicas="$INGEST"' in text
    restore = text[text.index("restore() {"):text.index("trap restore EXIT")]
    assert 'scale deploy/ingest-svc --replicas="$ORIG_INGEST"' in restore and 'kill "$CPU_LOG"' in restore
