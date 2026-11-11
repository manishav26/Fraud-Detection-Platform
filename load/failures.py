"""Failure tests: what clients felt when a scoring pod, a Cassandra node or Redpanda was killed,
how long until it was back, and availability = MTBF / (MTBF + MTTR) with a stated, assumed MTBF
(week 5; docs/results/failures.md).

Run by scripts/failure_test.sh, which saves in data/failures/<kind>-<round>/: run.csv (steady
payments, with when each was sent and whether it was degraded), client.txt (the sender's summary,
with the unix time it started) and events.json (unix times: killed, back).

    python -m load.failures report data/failures       per run, then per kind with availability
"""

import argparse
import csv
import json
import re
from pathlib import Path

# Assumed: each of these fails about once a month (a pod crash, a node lost, a broker restart).
# Six weeks can't measure it; the report states it and shows what changes if it is a week.
MTBF_HOURS = 30 * 24
KINDS = ("scoring", "cassandra", "redpanda")


def affected(row):
    """A payment a client would notice: no answer, an error, or scored without history."""
    return row["status"] != "200" or row.get("degraded") == "True"


def analyse(folder):
    folder = Path(folder)
    events = json.loads((folder / "events.json").read_text())
    started = float(re.search(r"Started sending at unix ([\d.]+)",
                              (folder / "client.txt").read_text()).group(1))
    killed, back = events["killed"] - started, events["back"] - started   # seconds into the run
    with open(folder / "run.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    before = [r for r in rows if float(r["sent_s"]) < killed - 1]
    after = [r for r in rows if float(r["sent_s"]) >= killed - 1]   # 1 s: the kill isn't instant
    hit = [r for r in after if affected(r)]
    # Client-visible repair time: from the kill to the answer of the last payment that noticed it
    last = max((float(r["sent_s"]) + float(r["latency_ms"]) / 1000 for r in hit), default=killed)
    return {
        "kind": events["kind"], "run": folder.name, "target": events.get("target", ""),
        "payments": len(rows), "affected_before": sum(map(affected, before)),
        "affected": len(hit), "errors": sum(r["status"] != "200" for r in hit),
        "degraded": sum(r["status"] == "200" and r.get("degraded") == "True" for r in hit),
        "back_s": back - killed,                       # Kubernetes: the pod (or node) ready again
        "client_mttr_s": max(0.0, last - killed),      # what clients felt
    }


def availability(mttr_s, mtbf_hours=MTBF_HOURS):
    mtbf_s = mtbf_hours * 3600
    return mtbf_s / (mtbf_s + mttr_s)


def report(root, mtbf_hours=MTBF_HOURS):
    runs = [analyse(d) for d in sorted(Path(root).iterdir()) if (d / "events.json").exists()]
    out = [("| Run | Killed | Payments | Noticed (errors + degraded) | Before the kill | Back in (s) |"
           " Clients felt it for (s) |"), "|" + "---|" * 7]
    for r in runs:
        out.append(f"| {r['run']} | {r['target']} | {r['payments']:,} | {r['affected']} "
                   f"({r['errors']} + {r['degraded']}) | {r['affected_before']} | {r['back_s']:.0f} | "
                   f"{r['client_mttr_s']:.1f} |")
    out += ["", (f"Availability = MTBF / (MTBF + MTTR), MTTR = what clients felt (mean over runs), "
            f"MTBF assumed {mtbf_hours / 24:.0f} days per kind (and, to show the sensitivity, 7 days):"),
            "", "| Failure | Runs | MTTR (s) | Availability | If MTBF were 7 days |", "|---|---|---|---|---|"]
    combined, combined_week, kinds = 1.0, 1.0, 0
    for kind in KINDS:
        mine = [r["client_mttr_s"] for r in runs if r["kind"] == kind]
        if not mine:
            continue
        mttr = sum(mine) / len(mine)
        a, a_week = availability(mttr, mtbf_hours), availability(mttr, 7 * 24)
        combined, combined_week, kinds = combined * a, combined_week * a_week, kinds + 1
        out.append(f"| {kind} | {len(mine)} | {mttr:.1f} | {a:.5%} | {a_week:.5%} |")
    if kinds > 1:
        out.append(f"| **all of them** (each can stop a payment: multiplied) | | | **{combined:.5%}** |"
                   f" {combined_week:.5%} |")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("root")
    r.add_argument("--mtbf-hours", type=float, default=MTBF_HOURS)
    args = p.parse_args()
    print(report(args.root, args.mtbf_hours))


if __name__ == "__main__":
    main()
