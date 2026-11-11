"""Scaling test: throughput with 1, 2 and 4 scoring copies, and the scaling efficiency
E_N = T_N / (N x T_1) (week 5; docs/results/scaling.md, "Week 5: 1, 2 and 4 copies").

Run by scripts/scaling_test.sh, which saves in data/scaling/<copies>/: run.csv (the replay, sent
as fast as its in-flight limit allows) and prometheus.json (that run's window).

    python -m load.scaling report data/scaling
"""

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

from load.ramp import sender_parts

STEADY = (0.1, 0.9)   # throughput between the 10th and 90th percentile of answer times: no warm-up, no drain


def throughput(path):
    """Payments answered per second in the steady middle of a run, their latency, and errors."""
    return senders_throughput([(path, 0.0)])


def senders_throughput(parts):
    """throughput() over several senders at once: parts = [(run CSV, that sender's unix start)];
    each answer is placed on one clock (start + sent_s + latency)."""
    rows = []
    for path, started in parts:
        with open(path, newline="") as f:
            rows += [(started, r) for r in csv.DictReader(f)]
    ok = [(started + float(r["sent_s"]) + float(r["latency_ms"]) / 1000, float(r["latency_ms"]))
          for started, r in rows if r["status"] == "200"]
    rows = [r for _, r in rows]
    errors = sum(1 for r in rows if r["status"] == "error" or r["status"].startswith("5"))
    if len(ok) < 10:
        return {"rate": math.nan, "p50_ms": math.nan, "p95_ms": math.nan, "answered": len(ok),
                "errors": errors, "sent": len(rows)}
    done = np.array([t for t, _ in ok])
    lo, hi = np.quantile(done, STEADY)
    steady = [ms for t, ms in ok if lo <= t <= hi]
    return {"rate": len(steady) / (hi - lo), "p50_ms": float(np.percentile(steady, 50)),
            "p95_ms": float(np.percentile(steady, 95)), "answered": len(ok), "errors": errors,
            "sent": len(rows)}


def efficiency(rates):
    """{copies: T_N} -> {copies: T_N / (N x T_1)}; needs the 1-copy run."""
    t1 = rates.get(1)
    return {n: (t / (n * t1) if t1 else math.nan) for n, t in rates.items()}


def report(root):
    root = Path(root)
    runs = {}
    for folder in root.iterdir():
        if folder.name.isdigit() and any(folder.glob("run*.csv")):
            prom_file = folder / "prometheus.json"
            prom = json.loads(prom_file.read_text()) if prom_file.exists() else {}
            runs[int(folder.name)] = (senders_throughput(sender_parts(folder)), prom)
    eff = efficiency({n: r["rate"] for n, (r, _) in runs.items()})
    out = [("| Scoring copies | Throughput T_N (/s) | E_N = T_N / (N x T_1) | p50 ms | p95 ms |"
           " 5xx | scoring /score p95 ms | Cassandra read p95 ms | busiest pods (cores) |"),
           "|" + "---|" * 9]
    for n in sorted(runs):
        r, prom = runs[n]
        steps = prom.get("steps_p95") or {}
        busy = ", ".join(f"{pod} {v:.2f}" for pod, v in
                         sorted((prom.get("cpu_pod_peak") or {}).items(), key=lambda kv: -kv[1])[:4])
        out.append(f"| {n} | {r['rate']:.1f} | {eff[n]:.2f} | {r['p50_ms']:.0f} | {r['p95_ms']:.0f} | "
                   f"{r['errors']} of {r['sent']:,} | {ms(prom.get('score_route_p95'))} | "
                   f"{ms(steps.get('history_read'))} | {busy or '–'} |")
    return "\n".join(out)


def ms(v):
    return "–" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v * 1000:.0f}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report").add_argument("root")
    args = p.parse_args()
    print(report(args.root))


if __name__ == "__main__":
    main()
