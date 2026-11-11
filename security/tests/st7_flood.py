"""ST7: a client flooding at 200/s must not hurt others more than it could by staying within its
limit of 50/s.

    make st7                 (compose)        make st7 URL=http://localhost:8080   (cluster)

merchant-2 sends 10/s and its p95 is measured while merchant-1 sends, in a separate process (so
the sending work doesn't slow this measurement down):
  A. nothing                       the baseline
  B. 45/s, just under its limit    what others feel from traffic the limit allows (twice)
  C. 200/s, a flood                what others feel from the flood (twice)

Why B and not A as the comparison: the limit correctly lets 50/s through, and those are real
payments. On a laptop, 6 times the load slows everyone, limit or not (an A-vs-C run measured +729%,
docs/results/security.md). B vs C isolates what the limit is for: the excess.

Pass: C's average p95 within 10% of B's, merchant-1 held to about 50/s in C with the rest
getting 429, and every merchant-2 payment answered 200.
"""

import argparse
import ast
import asyncio
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np

from load import synthetic
from load.credentials import credentials
from load.sender import run

LIMIT = 50          # merchant-1's limit, per second (deploy/kong/kong.tpl.yml)
UNDER_LIMIT = 45    # just under: Kong counts per clock second, so exactly 50 could tip over
FLOOD = 200


def p95(stats):
    return float(np.percentile(stats.latency_ms, 95)) if stats.latency_ms else float("nan")


def measured_run(url, seconds, seed, merchant1_rps):
    """merchant-2 at 10/s, with merchant-1 sending at merchant1_rps in another process.
    Returns (merchant-2 stats, merchant-1 status codes, merchant-1 accepted per second)."""
    other = None
    if merchant1_rps:
        other = subprocess.Popen(
            [sys.executable, "-m", "load.synthetic", "--client", "merchant-1",
             "--rps", str(merchant1_rps), "--seconds", str(seconds), "--concurrency", "128",
             "--seed", str(seed + 1000), "--url", url], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    stats, _ = asyncio.run(run(synthetic.schedule(10, seconds, users=500, seed=seed), url,
                               creds=credentials("merchant-2")))
    if other is None:
        return stats, {}, 0.0
    out = other.communicate()[0]
    codes = ast.literal_eval(re.search(r"Status codes: (\{.*\})", out).group(1))
    wall = float(re.search(r"payments in ([\d.]+) s", out).group(1))
    return stats, codes, codes.get(200, 0) / wall


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--seconds", type=float, default=20)
    # On the laptop two identical B runs differed by up to 2.4 times (docs/results/security.md), so
    # one pair can't judge a 10% rule: the VM rerun uses longer runs and more pairs
    p.add_argument("--repeats", type=int, default=2, help="B and C pairs (alternating)")
    args = p.parse_args()

    # B and C alternate, so a slow patch hits both
    plan = [("A", 0, 71)]
    for i in range(args.repeats):
        plan += [("B", UNDER_LIMIT, 72 + 2 * i), ("C", FLOOD, 73 + 2 * i)]
    results = []
    print("| Run | merchant-1 sends | merchant-1 status codes | merchant-1 accepted/s |"
          " merchant-2 status codes | merchant-2 p95 ms |")
    print("|---|---|---|---|---|---|")
    for name, rps, seed in plan:
        stats, codes, accepted = measured_run(args.url, args.seconds, seed, rps)
        results.append((name, stats, codes, accepted))
        print(f"| {name} | {rps or 'nothing'}{'/s' if rps else ''} | {codes or '-'} |"
              f" {accepted:.0f} | {dict(stats.status)} | {p95(stats):.1f} |", flush=True)

    b = np.mean([p95(s) for n, s, _, _ in results if n == "B"])
    c = np.mean([p95(s) for n, s, _, _ in results if n == "C"])
    floods = [(codes, acc) for n, _, codes, acc in results if n == "C"]
    rise = (c - b) / b * 100
    bs = [p95(s) for n, s, _, _ in results if n == "B"]
    print(f"\nmerchant-2 p95: within limit (B) {b:.1f} ms, during flood (C) {c:.1f} ms:"
          f" {rise:+.1f}% (pass: under 10%)")
    # How much identical runs differ: a difference between B and C smaller than this is noise
    print(f"B runs ranged {min(bs):.1f}-{max(bs):.1f} ms ({(max(bs) - min(bs)) / b * 100:.0f}% of B's"
          f" mean) over {len(bs)} runs")
    for codes, acc in floods:
        print(f"flood: merchant-1 accepted {acc:.0f}/s against a limit of {LIMIT}/s,"
              f" {codes.get(429, 0)} got 429")

    ok_limit = all(codes.get(429, 0) > 0 and acc <= LIMIT * 1.2 for codes, acc in floods)
    ok_others = rise < 10 and all(set(s.status) == {200} for _, s, _, _ in results)
    if not (ok_limit and ok_others):
        sys.exit("ST7 FAILED" + ("" if ok_limit else ": limit not enforced")
                 + ("" if ok_others else ": the flood hurt the other client more than allowed"
                    " traffic does"))
    print("ST7 PASSED")


if __name__ == "__main__":
    main()
