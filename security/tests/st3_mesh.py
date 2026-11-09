"""ST3: a pod outside the mesh calls scoring directly; it must be refused (threat 3, docs/mtls.md).

    make k8s-st3

The test pod is labelled as ingest (app=ingest-svc), so the NetworkPolicies let it reach scoring,
but it has no Linkerd proxy, so it has no mesh identity. It sends scoring the same call ingest
makes (POST /score). The same call from a real ingest pod, which is in the mesh, must work. That
separates the two layers: the network rules let the packet through; the mesh refuses the caller.

The test pod never takes real traffic: it is never ready (a readiness check that always fails), so
it is never in the ingest-pods Service that Kong sends payments to. Exit code 1 if anything differs.
"""

import json
import subprocess
import sys
import time

NS = "fraud"
POD = "st3-outside-mesh"
IMAGE = "fraud/ingest-svc:dev"
# An unknown user (NA in the id), so scoring doesn't save anything to Cassandra
PAYMENT = {"txn_id": "st3-probe", "user_id": "st3|NA|0", "amount": 10.0,
           "ts": "2026-11-08T10:00:00Z", "attrs": {}}
CALL = """
import json, sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], data=sys.argv[2].encode(),
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        print(r.status, r.read(80).decode(errors="replace"))
except urllib.error.HTTPError as e:
    print(e.code, e.read(120).decode(errors="replace").replace(chr(10), " "))
except Exception as e:
    print("error", type(e).__name__, str(e)[:100])
"""

OUTSIDE_POD = {
    "apiVersion": "v1", "kind": "Pod",
    "metadata": {"name": POD, "namespace": NS, "labels": {"app": "ingest-svc", "st3": "probe"},
                 "annotations": {"linkerd.io/inject": "disabled"}},   # no proxy, no identity
    "spec": {
        "serviceAccountName": "ingest-svc", "automountServiceAccountToken": False,
        "containers": [{
            "name": "probe", "image": IMAGE, "imagePullPolicy": "Never",
            "command": ["sleep", "600"],
            # never ready: never in the ingest-pods Service, so Kong never sends it payments
            "readinessProbe": {"exec": {"command": ["false"]}, "periodSeconds": 5},
        }],
    },
}


def kubectl(*args, stdin=None, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args], input=stdin, capture_output=True,
                          text=True, check=check).stdout


def ready_pod(app):
    pods = json.loads(kubectl("get", "pods", "-l", f"app={app}", "-o", "json"))["items"]
    for p in pods:
        ok = any(c["type"] == "Ready" and c["status"] == "True"
                 for c in p["status"].get("conditions", []))
        if ok and not p["metadata"].get("deletionTimestamp") and p["metadata"]["name"] != POD:
            return p["metadata"]["name"], p["status"]["podIP"]
    sys.exit(f"no ready {app} pod")


def call_from(pod, url):
    out = kubectl("exec", pod, "-c", pod_container(pod), "--", "python", "-c", CALL, url,
                  json.dumps(PAYMENT))
    return out.strip()


def pod_container(pod):
    return "probe" if pod == POD else "ingest-svc"


def main():
    _, scoring_ip = ready_pod("scoring-svc")
    ingest_pod, _ = ready_pod("ingest-svc")
    url = f"http://{scoring_ip}:8000/score"

    kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=true")
    kubectl("apply", "-f", "-", stdin=json.dumps(OUTSIDE_POD))
    for _ in range(60):
        if kubectl("get", "pod", POD, "-o", "jsonpath={.status.phase}", check=False) == "Running":
            break
        time.sleep(1)
    try:
        outside = call_from(POD, url)
        inside = call_from(ingest_pod, url)
    finally:
        kubectl("delete", "pod", POD, "--ignore-not-found", "--wait=false", check=False)

    print("| Caller | Network rules | Mesh identity | POST /score to a scoring pod |")
    print("|---|---|---|---|")
    print(f"| test pod labelled ingest, outside the mesh | allowed | none | {outside} |")
    print(f"| real ingest pod ({ingest_pod}) | allowed | ingest-svc | {inside[:60]} |")
    wrong = []
    if outside.startswith("200"):
        wrong.append("the pod outside the mesh was answered")
    if not inside.startswith("200"):
        wrong.append("the real ingest pod was refused")
    if wrong:
        sys.exit("\nST3 FAILED: " + "; ".join(wrong))
    print("\nST3 PASSED: without a mesh identity scoring refuses the call; with one it answers")


if __name__ == "__main__":
    main()
