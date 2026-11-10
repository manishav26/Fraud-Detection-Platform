"""SIEM hook, end to end on the cluster (week 4, step 7; docs/siem.md).   make k8s-siem-check

1. As the case system (`case-mgmt`, through Kong): read an open case's status by transaction
   id, then write back a decision. That writes an audit row; the check times how long until the
   SIEM shows it.
2. With --outage: the SIEM is stopped, a second case is written back, the SIEM is started again,
   and the event must still arrive: the forwarder kept the row until the SIEM answered.

Resolves real (open, dev) cases, outcome not_fraud, note "siem check". Reads the SIEM inside its
pod (kubectl exec), so no tunnel breaks while it restarts. The SIEM is always switched back on at
the end, even if the check fails or is stopped (an earlier version could leave it off). Delays are
the SIEM's receive time minus the audit row's time, both on the cluster's clock. Exit code 1 on a
failure.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from load.credentials import credentials

NS = "fraud"
READ_EVENTS = ("import json, urllib.request; print(urllib.request.urlopen("
               "'http://localhost:8000/events?limit=1000', timeout=5).read().decode())")


def kubectl(*args, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                          check=check).stdout


def siem_events():
    out = kubectl("exec", "deploy/siem-mock", "-c", "siem-mock", "--", "python", "-c", READ_EVENTS,
                  check=False)
    try:
        return json.loads(out)
    except ValueError:
        return None   # restarting


def token(client, cid):
    r = client.post("/token", json=credentials(cid))
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_for_event(case_id, timeout):
    """Seconds from the audit row being written to the SIEM storing it, or None if it didn't
    arrive within `timeout` seconds of waiting."""
    target, start = f"case:{case_id}", time.time()
    while time.time() - start < timeout:
        for item in siem_events() or []:
            e = item["event"]
            if e["action"] == "case_resolved" and e["target"] == target:
                return item["received_at"] - item["time"]
        time.sleep(0.5)
    return None


def siem_on():
    kubectl("scale", "deploy/siem-mock", "--replicas=1")
    kubectl("rollout", "status", "deploy/siem-mock", "--timeout=120s")
    for _ in range(60):   # ready to the Service, and answering inside the pod
        if siem_events() is not None:
            return
        time.sleep(1)
    sys.exit("FAILED: the SIEM did not come up")


def write_back(client, analyst, system, ref):
    """Pick an open case, check its status as the case system, write back. Returns case id."""
    open_cases = client.get("/cases?limit=1", headers=analyst).json()
    if not open_cases:
        sys.exit("No open case to use: run a replay first (make replay URL=http://localhost:8080)")
    txn = open_cases[0]["txn_id"]
    status = client.get(f"/v1/cases/txn/{txn}", headers=system)
    print(f"GET  /v1/cases/txn/{txn} as case-mgmt: {status.status_code} {status.json()['status']}")
    r = client.post(f"/v1/cases/txn/{txn}/decision", headers=system,
                    json={"outcome": "not_fraud", "note": "siem check", "external_ref": ref})
    print(f"POST /v1/cases/txn/{txn}/decision as case-mgmt: {r.status_code} {r.json().get('status')}")
    r.raise_for_status()
    again = client.post(f"/v1/cases/txn/{txn}/decision", headers=system, json={"outcome": "fraud"})
    print(f"POST again (a decision is never overwritten): {again.status_code}")
    if again.status_code != 409:
        sys.exit("FAILED: a second write-back was not refused")
    return r.json()["case_id"]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--outage", type=int, default=0, metavar="SECONDS",
                   help="also stop the SIEM this long and check nothing is lost")
    args = p.parse_args()
    failed = False
    siem_on()   # in case an earlier run was stopped mid-outage
    try:
        failed = run(args)
    finally:
        siem_on()
    if failed:
        sys.exit("\nSIEM check FAILED")
    print("\nSIEM check PASSED")


def run(args):
    failed = False
    with httpx.Client(base_url=args.url, timeout=15) as client:
        analyst, system = token(client, "ana"), token(client, "case-mgmt")

        print("\n1. Normal: write back, then wait for the SIEM")
        case_id = write_back(client, analyst, system, "SIEM-CHECK-1")
        delay = wait_for_event(case_id, timeout=30)
        print(f"SIEM shows case:{case_id} resolved after "
              + (f"{delay:.1f} s" if delay is not None else "NOTHING in 30 s"))
        failed |= delay is None

        if args.outage:
            print(f"\n2. Outage: SIEM stopped for {args.outage} s while a case is written back")
            kubectl("scale", "deploy/siem-mock", "--replicas=0")
            kubectl("wait", "--for=delete", "pod", "-l", "app=siem-mock", "--timeout=60s", check=False)
            case_id = write_back(client, analyst, system, "SIEM-CHECK-2")
            time.sleep(args.outage)
            siem_on()
            delay = wait_for_event(case_id, timeout=90)
            print(f"SIEM shows case:{case_id} resolved after "
                  + (f"{delay:.1f} s (including the {args.outage} s outage)" if delay is not None
                     else "NOTHING: the event was lost"))
            failed |= delay is None
    return failed


if __name__ == "__main__":
    main()
