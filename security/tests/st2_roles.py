"""ST2: every role calls every route; the results must match the roles table (docs/access.md).

    make st2                 (compose)        make st2 URL=http://localhost:8080   (cluster)

"Allowed" means the request got past Kong and the service's role checks: 200 for a payment or list,
404 for the case id below, which doesn't exist, and 400 for a promotion that isn't signed. That
proves access without resolving a real case or changing the model, so this can run as often as
needed. "Forbidden" means 403, from Kong.
Prints a Markdown table for docs/results/security.md; exit code 1 if anything differs.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load.credentials import credentials

NO_CASE = 2_000_000_000   # no case has this id
NO_TXN = "st2-no-such-txn"   # no case for this transaction

NOT_SIGNED = {"token": "not-a-signed-promotion"}   # model-admin-svc answers 400, changes nothing

# (name, method, path, body, roles allowed): the roles table
ROUTES = [
    ("Send a payment", "POST", "/v1/transactions/score", "payment", {"client", "tester"}),
    ("List cases", "GET", "/cases?limit=1", None, {"analyst", "admin"}),
    ("Read a case", "GET", f"/cases/{NO_CASE}", None, {"analyst", "admin"}),
    ("Resolve a case", "POST", f"/cases/{NO_CASE}/resolve", {"outcome": "fraud"}, {"analyst"}),
    ("Case status by payment", "GET", f"/v1/cases/txn/{NO_TXN}", None, {"case-system", "analyst"}),
    ("Write back a decision", "POST", f"/v1/cases/txn/{NO_TXN}/decision", {"outcome": "fraud"},
     {"case-system"}),
    ("See the live model", "GET", "/v1/model", None, {"admin"}),
    ("Model history", "GET", "/v1/model/history?limit=1", None, {"admin"}),
    ("Promote a model", "POST", "/v1/model/promote", NOT_SIGNED, {"admin"}),
    ("Roll back the model", "POST", "/v1/model/rollback", NOT_SIGNED, {"admin"}),
]
ALLOWED = {200, 400, 404}


def payment():
    return {"txn_id": f"st2-{uuid.uuid4()}", "user_id": "st2-user", "amount": 10.0,
            "ts": "2026-10-19T10:00:00Z"}


def call(client, method, path, body, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = payment() if body == "payment" else body
    return client.request(method, path, json=body, headers=headers)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    args = p.parse_args()
    roster = {k: v for k, v in json.loads((ROOT / "deploy/clients.json").read_text()).items()
              if not k.startswith("_")}
    # One client per role, plus no token at all
    callers = {"no token": (None, None)}
    for role in ("client", "tester", "analyst", "admin", "case-system"):
        cid = next(c for c, r in roster.items() if r == role)
        callers[role] = (cid, role)

    wrong = []
    with httpx.Client(base_url=args.url, timeout=15) as client:
        tokens = {}
        for name, (cid, _) in callers.items():
            if cid:
                r = client.post("/token", json=credentials(cid))
                r.raise_for_status()
                tokens[name] = r.json()["access_token"]

        print("| Route | " + " | ".join(callers) + " |")
        print("|---|" + "---|" * len(callers))
        for label, method, path, body, allowed in ROUTES:
            cells = []
            for name, (_, role) in callers.items():
                status = call(client, method, path, body, tokens.get(name)).status_code
                expect = "allowed" if role in allowed else ("401" if role is None else "403")
                ok = (status in ALLOWED) if expect == "allowed" else (status == int(expect))
                cells.append(f"{status}{'' if ok else ' WRONG'}")
                if not ok:
                    wrong.append(f"{label} as {name}: got {status}, expected {expect}")
            print(f"| {label} | " + " | ".join(cells) + " |")

    if wrong:
        sys.exit("ST2 FAILED:\n  " + "\n  ".join(wrong))
    print("\nST2 PASSED: every call matches the roles table")


if __name__ == "__main__":
    main()
