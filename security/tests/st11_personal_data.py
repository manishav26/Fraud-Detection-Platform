"""ST11: no raw user id in any stored row or log (threat 8). Design: docs/privacy.md

    make k8s-st11        (cluster; after make k8s-reset-data; needs your age key; opens a
                          tunnel to Cassandra, logs in as fraud_admin)

1. Replays the first --limit stream payments through Kong, so every store has fresh rows.
2. Searches every place the platform keeps data for two things: those payments' raw user ids
   exactly, and anything shaped like a raw id (card1|addr1|day, e.g. 9500|204.0|98.0).
   - Cassandra: txn_by_user, features_by_txn, user_features (every stored user id)
   - Postgres: cases.user_id, and the audit log's details
   - Redpanda: every message in txn.raw, txn.scored, dlq.txn
   - the SIEM's events
   - the logs of every container in the namespace since the replay started
3. Every user id Cassandra and Postgres hold must also have the pseudonym's shape.
Pass: 0 hits everywhere. Prints a Markdown table for docs/results/security.md.

The payments' attributes (card1, addr1, ...) are model features and stay as sent: see
docs/privacy.md, "Limits". This test looks for the user id, as ST11 defines it.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load.replay import load_rows
from pseudonym import is_pseudonym

NS = "fraud"
# A raw proxy id: card1 | addr1 (or NA) | first-seen day (or NA)
RAW_SHAPE = re.compile(r"(?<![\w.])\d{1,6}\|(?:\d+\.\d+|NA)\|(?:-?\d+\.\d+|NA)(?![\w.])")


def sh(*args, stdin=None, timeout=600):
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout,
                       check=False)
    if r.returncode != 0:
        sys.exit(f"failed: {' '.join(args[:6])}...\n{r.stderr.strip()[-500:]}")
    return r.stdout


def cassandra_ids():
    """Every user id in the three tables, read in pages of 1,000 through the driver (the tunnel
    from make k8s-st11). One unpaged query over the whole history is refused by Cassandra: the
    backfill's missing e-mails and devices are stored as empty cells (tombstones), and a single
    read may pass at most 100,000 of them."""
    from cassandra.query import SimpleStatement

    from store.history import HistoryStore
    store = HistoryStore(hosts=["localhost"])
    try:
        out = {}
        for table, query in [("txn_by_user", "SELECT DISTINCT user_id FROM txn_by_user"),
                             ("features_by_txn", "SELECT user_id FROM features_by_txn"),
                             ("user_features", "SELECT user_id FROM user_features")]:
            rows = store.session.execute(SimpleStatement(query, fetch_size=1000), timeout=60)
            out[f"Cassandra {table}"] = [r["user_id"] for r in rows]
        return out
    finally:
        store.close()


def psql(query):
    return sh("kubectl", "-n", NS, "exec", "-i", "postgres-0", "-c", "postgres", "--",
              "psql", "-U", "fraud", "-d", "fraud", "-qtA", stdin=query).splitlines()


def topic(name):
    return sh("./scripts/rpk.sh", "topic", "consume", name, "-o", ":end", "-f", "%v\n",
              stdin="", timeout=900)


def siem_events():
    return sh("kubectl", "-n", NS, "exec", "deploy/siem-mock", "-c", "siem-mock", "--", "python",
              "-c", "import urllib.request; print(urllib.request.urlopen("
              "'http://localhost:8000/events?limit=1000', timeout=10).read().decode())")


def logs(since_s):
    pods = sh("kubectl", "-n", NS, "get", "pods", "-o", "name").split()
    return {p: subprocess.run(["kubectl", "-n", NS, "logs", p, "--all-containers",
                               f"--since={since_s}s"], capture_output=True, text=True,
                              check=False).stdout for p in pods}


def hits(text, raw):
    """(exact raw ids of this run found, anything raw-shaped found)"""
    exact = sum(1 for r in raw if r in text)
    return exact, len(RAW_SHAPE.findall(text))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args()

    raw = set(load_rows(ROOT / "data/processed/stream.parquet", args.limit)["uid"])
    started = time.time()
    print(f"1. replay the first {args.limit} stream payments ({len(raw)} user ids)", flush=True)
    sh(sys.executable, "-m", "load.replay", "--speed", "1000", "--limit", str(args.limit),
       "--url", args.url)
    time.sleep(10)   # cases open and the feature job writes within seconds

    print("2. search", flush=True)
    rows = []   # (place, items, exact hits, raw-shaped hits, ids not pseudonyms)
    for place, ids in cassandra_ids().items():
        text = "\n".join(ids)
        rows.append((place, len(ids), *hits(text, raw), sum(not is_pseudonym(i) for i in ids)))
    cases = psql("SELECT user_id FROM cases;")
    rows.append(("Postgres cases.user_id", len(cases), *hits("\n".join(cases), raw),
                 sum(not is_pseudonym(i) for i in cases)))
    audit = psql("SELECT details::text FROM audit_log;")
    rows.append(("Postgres audit_log details", len(audit), *hits("\n".join(audit), raw), "-"))
    for name in ("txn.raw", "txn.scored", "dlq.txn"):
        text = topic(name)
        rows.append((f"Redpanda {name}", len(text.splitlines()), *hits(text, raw), "-"))
    text = siem_events()
    rows.append(("SIEM events", text.count('"event"'), *hits(text, raw), "-"))
    for pod, text in logs(int(time.time() - started) + 60).items():
        e, s = hits(text, raw)
        rows.append((f"logs {pod.removeprefix('pod/')}", len(text.splitlines()), e, s, "-"))

    print("\n| Where | Rows, messages or lines | This run's raw ids | Raw-shaped ids | Ids not pseudonyms |")
    print("|---|---:|---:|---:|---:|")
    for place, n, exact, shaped, bad in rows:
        print(f"| {place} | {n:,} | {exact} | {shaped} | {bad} |")
    found = [r for r in rows if r[2] or r[3] or (isinstance(r[4], int) and r[4])]
    if found:
        sys.exit("\nST11 FAILED: raw user ids in " + ", ".join(r[0] for r in found))
    print("\nST11 PASSED: no raw user id in any stored row, message or log")


if __name__ == "__main__":
    main()
