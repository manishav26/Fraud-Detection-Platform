"""Ramp test: judge each step against the targets, then the tables (week 5; docs/results/ramp.md).

Run by scripts/ramp_test.sh, which sends each step and saves, in data/ramp/<rate>/:
run.csv (the replay's payments, with when each was sent), prometheus.json (observability.snapshot
for the step's measured window) and step.json (start and end, in unix seconds).

    python -m load.ramp preflight                      every metric a step needs is in Prometheus
    python -m load.ramp judge data/ramp/100 --rps 100 --seconds 180 --measure 120
                                                       exit 0 pass, 1 a target missed, 2 the
                                                       sender didn't reach the rate
    python -m load.ramp report data/ramp               the rate table and the hop table (Markdown)
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

from observability.snapshot import PROMETHEUS, REQUIRED_METRICS, prometheus_query

# The Gate 2 targets (docs/results/baseline.md), for the cluster through Kong after warm-up
TARGETS = {
    "p95_ms": 250,          # end to end
    "p99_ms": 1500,
    "ingest_p95_ms": 100,   # ingest's own server time
    "errors_share": 0.0005,
    "degraded_share": 0.01,
}
REACHED = 0.95   # a step that sent less than 95% of the asked rate measured the sender, not us
PASS, MISSED, SENDER_LIMITED = 0, 1, 2


def client_window(path, start_s, end_s):
    """The replay's own view of the payments sent between start_s and end_s (seconds from the
    replay's start): how many, the rate reached, latency of the answered ones, and errors."""
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if start_s <= float(r["sent_s"]) < end_s]
    ok = [float(r["latency_ms"]) for r in rows if r["status"] == "200"]
    errors = sum(1 for r in rows if r["status"] == "error" or r["status"].startswith("5"))
    p50, p95, p99 = np.percentile(ok, [50, 95, 99]) if ok else (math.nan,) * 3
    return {"sent": len(rows), "rate": len(rows) / (end_s - start_s), "answered": len(ok),
            "errors": errors, "errors_share": errors / len(rows) if rows else math.nan,
            "p50_ms": float(p50), "p95_ms": float(p95), "p99_ms": float(p99)}


def nan(v):
    return math.nan if v is None else v


def judge(client, prom, asked):
    """(PASS, MISSED or SENDER_LIMITED, the reasons). A missing number is a miss, not a pass."""
    if client["rate"] < REACHED * asked:
        return SENDER_LIMITED, [f"sender reached {client['rate']:.1f}/s of {asked}/s asked"]
    decisions = sum((prom.get("decisions") or {}).values())
    measured = {
        "p95_ms": client["p95_ms"],
        "p99_ms": client["p99_ms"],
        "ingest_p95_ms": nan((prom.get("p95") or {}).get("ingest-svc")) * 1000,
        "errors_share": client["errors_share"],
        "degraded_share": nan(prom.get("degraded")) / decisions if decisions else math.nan,
    }
    missed = [f"{name} {'no data' if math.isnan(v) else f'{v:.4g}'} (target {TARGETS[name]})"
              for name, v in measured.items() if math.isnan(v) or v > TARGETS[name]]
    return (MISSED if missed else PASS), missed


def copies(path, start, end):
    """Most scoring copies ready during [start, end) (unix seconds), from the 5 s watcher."""
    try:
        with open(path, newline="") as f:
            ready = [int(r["ready"]) for r in csv.DictReader(f)
                     if r["ready"] and start <= float(r["time"]) < end]
    except FileNotFoundError:
        return None
    return max(ready) if ready else None


def load_step(folder):
    folder = Path(folder)
    step = json.loads((folder / "step.json").read_text())
    prom = json.loads((folder / "prometheus.json").read_text())
    return step, prom


def ms(v):
    return "–" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 1000:.0f}"


def report(root):
    root = Path(root)
    steps = []
    for folder in root.iterdir():
        if (folder / "step.json").exists() and (folder / "prometheus.json").exists():
            steps.append(load_step(folder))
    steps.sort(key=lambda s: s[0]["rps"])
    verdicts = {PASS: "pass", MISSED: "missed", SENDER_LIMITED: "sender-limited"}
    out = [("| Asked /s | Reached /s | p50 ms | p95 ms | p99 ms | ingest p95 ms | 5xx | degraded |"
            " scoring copies | Result |"), "|" + "---|" * 10]
    for step, prom in steps:
        c = step["client"]
        decisions = sum((prom.get("decisions") or {}).values())
        degraded = nan(prom.get("degraded")) / decisions if decisions else math.nan
        out.append(f"| {step['rps']} | {c['rate']:.1f} | {c['p50_ms']:.0f} | {c['p95_ms']:.0f} | "
                   f"{c['p99_ms']:.0f} | {ms((prom.get('p95') or {}).get('ingest-svc'))} | "
                   f"{c['errors_share']:.3%} | {degraded:.2%} | {step.get('copies') or '–'} | "
                   f"{verdicts[step['verdict']]}{': ' + '; '.join(step['missed']) if step['missed'] else ''} |")
    out += ["", "Where the time goes (p95 per hop, ms; p95s don't add up: each is its own slowest 5%)",
            "", ("| Asked /s | End to end | Kong total | Kong itself | Kong -> ingest | ingest server |"
                 " publish txn.raw | wait for scoring | scoring /score | Cassandra read | model |"
                 " reasons | Cassandra write | features fresh (p95 s) | feature job / sent |"),
            "|" + "---|" * 15]
    for step, prom in steps:
        c, ing, sc = step["client"], prom.get("ingest_steps_p95") or {}, prom.get("steps_p95") or {}
        fresh, handled = prom.get("freshness_p95"), prom.get("feature_job_payments")
        keeps_up = f"{handled / c['sent']:.2f}" if handled is not None and c["sent"] else "–"
        out.append(f"| {step['rps']} | {c['p95_ms']:.0f} | {ms(prom.get('kong_p95'))} | "
                   f"{ms(prom.get('kong_own_p95'))} | {ms(prom.get('kong_upstream_p95'))} | "
                   f"{ms((prom.get('p95') or {}).get('ingest-svc'))} | {ms(ing.get('publish_raw'))} | "
                   f"{ms(ing.get('wait_scoring'))} | {ms(prom.get('score_route_p95'))} | "
                   f"{ms(sc.get('history_read'))} | {ms(sc.get('predict'))} | {ms(sc.get('reasons'))} | "
                   f"{ms(sc.get('history_write'))} | {'–' if fresh is None else f'{fresh:.1f}'} | {keeps_up} |")
    return "\n".join(out)


def preflight(query):
    """The metrics a step needs that Prometheus has no series for."""
    return [m for m in REQUIRED_METRICS if not query(f"count({m})")]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--url", default=PROMETHEUS)
    j = sub.add_parser("judge")
    j.add_argument("folder")
    j.add_argument("--rps", type=int, required=True)
    j.add_argument("--seconds", type=int, required=True, help="the step's length")
    j.add_argument("--measure", type=int, required=True, help="its last N seconds are measured")
    j.add_argument("--copies", help="the scoring copies watcher's CSV")
    r = sub.add_parser("report")
    r.add_argument("root")
    args = p.parse_args()

    if args.cmd == "preflight":
        missing = preflight(prometheus_query(args.url))
        if missing:
            sys.exit("Prometheus has no data for: " + ", ".join(missing))
        print(f"Prometheus has all {len(REQUIRED_METRICS)} metrics a step needs")
    elif args.cmd == "judge":
        folder = Path(args.folder)
        step = json.loads((folder / "step.json").read_text())
        prom = json.loads((folder / "prometheus.json").read_text())
        client = client_window(folder / "run.csv", args.seconds - args.measure, args.seconds)
        verdict, missed = judge(client, prom, args.rps)
        step.update(rps=args.rps, client=client, verdict=verdict, missed=missed,
                    copies=copies(args.copies, step["measure_start"], step["end"]) if args.copies else None)
        (folder / "step.json").write_text(json.dumps(step, indent=2))
        print(f"{args.rps}/s: reached {client['rate']:.1f}/s, p95 {client['p95_ms']:.0f} ms, "
              f"p99 {client['p99_ms']:.0f} ms -> "
              f"{['pass', 'missed', 'sender-limited'][verdict]}{': ' + '; '.join(missed) if missed else ''}")
        sys.exit(verdict)
    else:
        print(report(args.root))


if __name__ == "__main__":
    main()
