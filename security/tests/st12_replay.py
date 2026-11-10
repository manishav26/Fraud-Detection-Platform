"""ST12: replayed and old requests (threat 6). Design: docs/ingest.md, "Replays"

    make k8s-st12       (cluster; needs your age key and keys/private.pem; opens a Cassandra tunnel)

1. Same request twice: a fraud payment the model flags, sent twice with the same txn_id and the
   same token. Both answers must be equal, and there must be one history row in Cassandra, one
   case, one case_opened audit row; the feature job counts the second one as a duplicate.
2. A payment whose token was issued 10 minutes ago (made with the real key, as ST1 does, so Kong
   accepts it): refused with 401 by ingest, and txn.raw gets nothing.
3. The same with a token issued 1 minute ago: accepted.
Sends as load-test (role tester), so the answer includes the score. Prints a Markdown table.
"""

import argparse
import re
import sys
import time
from pathlib import Path

import httpx
import jwt
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "security" / "tests"))
from st1_tokens import claims, raw_count
from st11_personal_data import psql

from load.credentials import credentials
from load.replay import attr_columns, load_rows, to_request
from pseudonym import pseudonym
from store.history import HistoryStore, is_known, to_datetime

CLIENT, ROLE = "load-test", "tester"
SCORE = "/v1/transactions/score"
NS = "fraud"


def feature_job_counts():
    import subprocess
    code = ("import urllib.request; t = urllib.request.urlopen('http://localhost:8000/metrics',"
            " timeout=5).read().decode(); print('\\n'.join(l for l in t.splitlines()"
            " if l.startswith('feature_job_payments_total{')))")
    for _ in range(10):   # the pod may be busy or restarting: try again for a little while
        out = subprocess.run(["kubectl", "-n", NS, "exec", "feature-job-0", "-c", "feature-job",
                              "--", "python", "-c", code],
                             capture_output=True, text=True, check=False).stdout
        counts = {m[1]: float(m[2]) for m in
                  re.finditer(r'outcome="(\w+)"\}\s+([0-9.e+-]+)', out)}
        if counts:
            return counts
        time.sleep(2)
    sys.exit("could not read the feature job's metrics (is feature-job-0 running?)")


def wait_for(check, seconds=30):
    for _ in range(seconds):
        if check():
            return True
        time.sleep(1)
    return check()


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8080")
    args = p.parse_args()
    run = int(time.time())
    private = (ROOT / "keys" / "private.pem").read_text()
    df = load_rows(ROOT / "data/processed/stream.parquet")
    frauds = df[(df["isFraud"] == 1) & df["uid"].map(is_known)]
    attrs = attr_columns()
    rows, wrong = [], []

    def record(what, expected, got, ok):
        rows.append((what, expected, got, "pass" if ok else "FAIL"))
        if not ok:
            wrong.append(what)

    with httpx.Client(base_url=args.url, timeout=15) as client:
        r = client.post("/token", json=credentials(CLIENT))
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 1. A flagged payment, then exactly the same request again
        payment, first = None, None
        for n, i in enumerate(np.random.default_rng(run).permutation(len(frauds))[:60]):
            payment = {**to_request(frauds.iloc[i].to_dict(), attrs), "txn_id": f"st12-{run}-{n}"}
            first = client.post(SCORE, json=payment, headers=auth).json()
            if first["decision"] != "approve":
                break
        else:
            sys.exit("no flagged fraud payment in 60 tries")
        before_job = feature_job_counts()
        again = client.post(SCORE, json=payment, headers=auth).json()
        txn = payment["txn_id"]
        record("same request twice: answers", "equal", "equal" if again == first else
               f"{first['decision']} then {again['decision']}", again == first)

        store = HistoryStore(hosts=["localhost"])
        try:
            n_rows = store.session.execute(
                "SELECT count(*) AS n FROM txn_by_user WHERE user_id = %s AND ts = %s"
                " AND txn_id = %s",
                (pseudonym(payment["user_id"]), to_datetime(payment["ts"]), txn)).one()["n"]
        finally:
            store.close()
        record("same request twice: history rows", "1", str(n_rows), n_rows == 1)

        def cases():
            return int(psql(f"SELECT count(*) FROM cases WHERE txn_id = '{txn}';")[0])
        wait_for(lambda: cases() >= 1)
        n_cases = cases()
        n_audit = int(psql("SELECT count(*) FROM audit_log WHERE action = 'case_opened'"
                           f" AND details->>'txn_id' = '{txn}';")[0])
        record("same request twice: cases", "1", str(n_cases), n_cases == 1)
        record("same request twice: case_opened audit rows", "1", str(n_audit), n_audit == 1)

        def dup():
            return feature_job_counts().get("duplicate", 0) - before_job.get("duplicate", 0)
        wait_for(lambda: dup() >= 1)
        record("same request twice: feature job", "second counted as duplicate",
               f"{dup():.0f} duplicate", dup() == 1)

        # 2 and 3. Tokens made with the real key, issued 10 and 1 minutes ago
        for age, allowed in [(600, False), (60, True)]:
            token = jwt.encode(claims(client=CLIENT, role=ROLE, age=age), private,
                               algorithm="RS256")
            body = {**payment, "txn_id": f"st12-{run}-age{age}"}
            before = raw_count("./scripts/rpk.sh")
            r = client.post(SCORE, json=body, headers={"Authorization": f"Bearer {token}"})
            added = raw_count("./scripts/rpk.sh") - before
            if allowed:
                record(f"token issued {age // 60} min ago", "200", str(r.status_code),
                       r.status_code == 200)
            else:
                ok = r.status_code == 401 and "too old" in r.text and added == 0
                record(f"token issued {age // 60} min ago", "401, nothing stored",
                       f"{r.status_code}, {added} new in txn.raw", ok)

    print("| Check | Expected | Got | Result |")
    print("|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    if wrong:
        sys.exit("\nST12 FAILED: " + "; ".join(wrong))
    print(f"\nST12 PASSED (payment {txn}): a resend changes nothing, an old token is refused")


if __name__ == "__main__":
    main()
