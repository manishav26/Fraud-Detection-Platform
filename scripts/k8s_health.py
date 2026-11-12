"""Is the cluster healthy, and if not, what exactly to do (docs/vm.md, "After a stop and start").

    make k8s-health        (also the last step of make k8s-up)

Read-only: it checks, and prints the command that fixes each problem, but changes nothing.
Exit code 1 if anything is wrong. The checks, in the order to fix them:
  1. every node Ready
  2. every node's kubelet reachable: after the VM restarts, Docker can give a k3d node a new
     address while its kubelet keeps a certificate for the old one (12 Nov), and then logs, exec
     and port-forward to its pods fail
  3. every pod in `fraud` has its Linkerd proxy: a pod created while the proxy injector wasn't
     answering ran without one and could reach nothing (12 Nov; the injector now fails closed,
     scripts/k8s_linkerd.sh, so this catches older pods)
  4. every pod ready
  5. Cassandra: as many nodes Up/Normal as the StatefulSet has
"""

import json
import subprocess
import sys

NS = "fraud"


def kubectl(*args):
    """(exit code, stdout + stderr)"""
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, check=False)
    return r.returncode, r.stdout + r.stderr


def node_problems(nodes):
    """[(problem, fix)] for nodes not Ready."""
    out = []
    for n in nodes["items"]:
        ready = next((c["status"] for c in n["status"].get("conditions", []) if c["type"] == "Ready"),
                     "Unknown")
        if ready != "True":
            name = n["metadata"]["name"]
            out.append((f"node {name} is not Ready ({ready})",
                        f"wait a minute; if it stays so: docker ps -a --filter name={name}"))
    return out


def kubelet_problem(node, error):
    """(problem, fix) when reading a pod's logs on `node` failed with `error`."""
    if "x509" in error or "certificate" in error:
        return ((f"{node}: its kubelet's certificate is for an old address (the node moved when "
                f"Docker restarted)"), f"docker restart {node}   (one node at a time)")
    return (f"{node}: kubelet not reachable: {error.strip().splitlines()[-1] if error.strip() else '?'}",
            f"docker restart {node}   (one node at a time), then check again")


def has_proxy(pod):
    spec = pod["spec"]
    return any(c["name"] == "linkerd-proxy" for c in spec.get("initContainers", []) + spec["containers"])


def unmeshed(pods):
    """Pods that should have a Linkerd proxy and don't. A pod that opts out on purpose (ST3's
    probe: linkerd.io/inject: disabled) is not counted."""
    return [p["metadata"]["name"] for p in pods["items"]
            if (p["metadata"].get("annotations") or {}).get("linkerd.io/inject") != "disabled"
            and p["status"].get("phase") in ("Running", "Pending") and not has_proxy(p)]


def not_ready(pods):
    """Pods that are running but not ready, or not running at all (finished ones aside)."""
    out = []
    for p in pods["items"]:
        phase = p["status"].get("phase")
        if phase == "Succeeded":
            continue
        ready = next((c["status"] for c in p["status"].get("conditions", []) if c["type"] == "Ready"),
                     "False")
        if phase != "Running" or ready != "True":
            out.append(f"{p['metadata']['name']} ({phase}, ready={ready})")
    return out


def cassandra_up(nodetool_status):
    return sum(1 for line in nodetool_status.splitlines() if line.startswith("UN"))


def pod_per_node(pods):
    """{node: a running pod on it}, to reach each node's kubelet through."""
    out = {}
    for p in pods["items"]:
        node = p["spec"].get("nodeName")
        if node and p["status"].get("phase") == "Running":
            out.setdefault(node, p["metadata"]["name"])
    return out


def main():
    problems = []
    code, text = kubectl("get", "nodes", "-o", "json")
    if code:
        sys.exit(f"can't reach the cluster: {text.strip()}")
    nodes = json.loads(text)
    problems += node_problems(nodes)

    code, text = kubectl("-n", NS, "get", "pods", "-o", "json")
    pods = json.loads(text)
    on = pod_per_node(pods)
    for node in sorted(n["metadata"]["name"] for n in nodes["items"]):
        if node not in on:
            continue   # nothing of ours runs there to test through
        code, text = kubectl("-n", NS, "logs", on[node], "--all-containers", "--tail=1")
        if code:
            problems.append(kubelet_problem(node, text))

    missing = unmeshed(pods)
    if missing:
        problems.append((f"{len(missing)} pod(s) without their Linkerd proxy: {', '.join(missing)}",
                         f"kubectl -n {NS} delete pod {' '.join(missing)}   (recreated with it)"))
    waiting = not_ready(pods)
    if waiting:
        problems.append((f"{len(waiting)} pod(s) not ready: {', '.join(waiting)}",
                         ("wait a few minutes (Cassandra's nodes join one at a time); if they stay so, "
                         "fix the problems above first")))

    code, text = kubectl("-n", NS, "get", "statefulset", "cassandra", "-o", "jsonpath={.spec.replicas}")
    expected = int(text) if code == 0 and text.strip().isdigit() else None
    if expected:
        code, status = kubectl("-n", NS, "exec", "cassandra-0", "-c", "cassandra", "--", "nodetool", "status")
        up = cassandra_up(status) if code == 0 else 0
        if up != expected:
            problems.append((f"Cassandra: {up} of {expected} nodes Up/Normal",
                             f"kubectl -n {NS} exec cassandra-0 -c cassandra -- nodetool status"))

    if not problems:
        print(f"healthy: {len(nodes['items'])} nodes, {len(pods['items'])} pods all meshed and ready"
              f"{f', Cassandra {expected} of {expected} up' if expected else ''}")
        return
    print("NOT HEALTHY. Fix in this order, then run make k8s-health again:")
    for i, (problem, fix) in enumerate(problems, 1):
        print(f"  {i}. {problem}\n     fix: {fix}")
    sys.exit(1)


if __name__ == "__main__":
    main()
