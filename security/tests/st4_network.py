"""ST4: from inside the cluster, try every pod and port; only the allowed paths may connect.

    make k8s-st4

Sources: one pod of each workload that has Python (our services and MLflow), plus an "intruder":
a pod with no labels, standing in for a compromised pod. Each tries a TCP connection to one pod of
every workload on every port it listens on (pod addresses: policies act on pods, and the metrics
ports aren't on Services). The result must match security/tests/network_paths.py exactly: allowed
paths connect, every other one is blocked. Exit code 1 if anything differs.

"Blocked" is a timeout or a refusal: k3s's policy enforcer rejects a blocked connection at once
rather than dropping it, and every port tried here is listening, so a refusal is the policy. A pod
is never tested against its own address (that traffic never leaves the pod, so no policy sees it):
for its own workload it tries another copy, or skips it if there is only one.
Prints a Markdown table for docs/results/security.md.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from security.tests.network_paths import LISTENS, allowed

NS = "fraud"
SOURCES = ["ingest-svc", "scoring-svc", "alert-svc", "model-admin-svc", "token-issuer", "mlflow"]
INTRUDER = "st4-intruder"
IMAGE = "fraud/token-issuer:dev"   # any of our images: it has Python, and k3d already has it

# Runs inside each source pod: try every target at once, 2 s each
PROBE = """
import json, socket, sys
from concurrent.futures import ThreadPoolExecutor
def try_one(t):
    ip, port = t
    try:
        socket.create_connection((ip, port), timeout=2).close()
        return "open"
    except socket.timeout:
        return "blocked"
    except OSError as e:
        return "blocked"   # refused (the policy's reject) or unreachable
targets = json.loads(sys.argv[1])
with ThreadPoolExecutor(32) as ex:
    print(json.dumps(list(ex.map(try_one, targets))))
"""


def kubectl(*args, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                          check=check).stdout


def pods_of(app):
    """Ready pods of `app` that are not being removed, as [(name, ip)]. During a rollout old and
    new run side by side, and the old ones may already be shutting down."""
    pods = json.loads(kubectl("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]
    ready = [(p["metadata"]["name"], p["status"]["podIP"]) for p in pods
             if not p["metadata"].get("deletionTimestamp") and p["status"].get("podIP")
             and any(c["type"] == "Ready" and c["status"] == "True"
                     for c in p["status"].get("conditions", []))]
    if not ready:
        sys.exit(f"no ready pod for {app} yet: wait until `kubectl -n fraud get pods` shows "
                 "every pod 1/1 Running, then run again")
    return ready


def start_intruder():
    kubectl("delete", "pod", INTRUDER, "--ignore-not-found", "--wait=true")
    kubectl("run", INTRUDER, f"--image={IMAGE}", "--image-pull-policy=Never", "--restart=Never",
            "--labels=st4=intruder", "--command", "--", "sleep", "900")
    kubectl("wait", "--for=condition=Ready", f"pod/{INTRUDER}", "--timeout=90s")


def main():
    pods = {app: pods_of(app) for app in LISTENS}
    start_intruder()
    sources = {app: pods[app][0][0] for app in SOURCES} | {"intruder (no labels)": INTRUDER}
    wrong, rows = [], []
    try:
        for name, pod in sources.items():
            src = None if pod == INTRUDER else name
            targets = []
            for app, ports in LISTENS.items():
                others = [ip for p, ip in pods[app] if p != pod]   # never the sender itself
                if others:
                    targets += [(app, port, others[0]) for port in ports]
            out = kubectl("exec", pod, "--", "python", "-c", PROBE,
                          json.dumps([[ip, port] for _, port, ip in targets]))
            results = json.loads(out.strip().splitlines()[-1])
            opened = []
            for (dst, port, _), result in zip(targets, results):
                expect = "open" if allowed(src, dst, port) else "blocked"
                if result == "open":
                    opened.append(f"{dst}:{port}")
                if result != expect:
                    wrong.append(f"{name} -> {dst}:{port}: {result}, expected {expect}")
            tried = len(targets)
            rows.append(f"| {name} | {', '.join(opened) or 'nothing'} | {tried - len(opened)} of {tried} |")
    finally:
        kubectl("delete", "pod", INTRUDER, "--ignore-not-found", "--wait=false", check=False)

    print(f"Run {time.strftime('%Y-%m-%d %H:%M')}: each source tried every other pod's ports "
          "(its own workload: another copy, if there is one).\n")
    print("| From | Could open | Blocked |")
    print("|---|---|---|")
    print("\n".join(rows))
    if wrong:
        sys.exit("\nST4 FAILED:\n  " + "\n  ".join(wrong))
    print("\nST4 PASSED: only the allowed paths connect (security/tests/network_paths.py)")


if __name__ == "__main__":
    main()
