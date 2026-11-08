"""The Kubernetes manifests keep their resilience settings (week 4, step 2; docs/k8s.md).

Read straight from deploy/k8s/base, so a later edit can't quietly drop a probe, a limit or the
rollout rule.
"""

from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parents[1] / "deploy/k8s/base"
DOCS = [d for f in sorted(BASE.glob("*.yaml")) if f.name != "kustomization.yaml"
        for d in yaml.safe_load_all(f.read_text()) if d]
DEPLOYMENTS = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "Deployment"}
# Our FastAPI services, which have /healthz and /readyz (observability/http.py). mlflow is built
# here too, but it is the MLflow server, with its own /health route.
OURS = {name: d for name, d in DEPLOYMENTS.items()
        if d["spec"]["template"]["spec"]["containers"][0]["image"].startswith("fraud/")
        and name != "mlflow"}


def container(deployment):
    return deployment["spec"]["template"]["spec"]["containers"][0]


def test_every_manifest_file_is_in_the_kustomization():
    listed = set(yaml.safe_load((BASE / "kustomization.yaml").read_text())["resources"])
    files = {f.name for f in BASE.glob("*.yaml")} - {"kustomization.yaml"}
    assert files <= listed, f"not deployed by kustomize: {files - listed}"


@pytest.mark.parametrize("name", sorted(DEPLOYMENTS))
def test_rollouts_never_lower_capacity(name):
    strategy = DEPLOYMENTS[name]["spec"]["strategy"]
    assert strategy["type"] == "RollingUpdate"
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


@pytest.mark.parametrize("name", sorted(DEPLOYMENTS))
def test_every_container_has_a_cpu_request_and_a_memory_limit(name):
    resources = container(DEPLOYMENTS[name])["resources"]
    assert resources["requests"]["cpu"] and resources["requests"]["memory"]
    assert resources["limits"]["memory"]


@pytest.mark.parametrize("name", sorted(OURS))
def test_our_services_have_all_three_probes_and_a_pause_before_stopping(name):
    c = container(OURS[name])
    assert c["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    live = c["livenessProbe"]
    assert live["httpGet"]["path"] == "/healthz"
    # lenient: one slow burst (1-2 s, docs/results/baseline.md) must never restart a pod
    assert live["periodSeconds"] * live["failureThreshold"] >= 60
    assert c["lifecycle"]["preStop"]["sleep"]["seconds"] >= 5


def test_mlflow_has_lenient_probes_on_its_own_health_route():
    c = container(DEPLOYMENTS["mlflow"])
    assert c["readinessProbe"]["httpGet"]["path"] == "/health"
    live = c["livenessProbe"]
    assert live["httpGet"]["path"] == "/health"
    assert live["periodSeconds"] * live["failureThreshold"] >= 60


def test_scoring_copies_are_set_by_its_hpa_only():
    assert "replicas" not in DEPLOYMENTS["scoring-svc"]["spec"]
    hpa = next(d for d in DOCS if d["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["scaleTargetRef"]["name"] == "scoring-svc"
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (2, 8)
    target = hpa["spec"]["metrics"][0]["resource"]
    assert target["name"] == "cpu" and target["target"]["averageUtilization"] == 60


def test_scoring_has_no_cpu_limit():
    # A CPU limit throttles the process when reached, which shows up as latency spikes
    assert "cpu" not in container(DEPLOYMENTS["scoring-svc"])["resources"]["limits"]


@pytest.mark.parametrize("name", ["scoring-svc", "ingest-svc", "alert-svc"])
def test_disruption_budget_keeps_one_copy_and_matches_the_pods(name):
    pdb = next(d for d in DOCS if d["kind"] == "PodDisruptionBudget"
               and d["metadata"]["name"] == name)
    assert pdb["spec"]["minAvailable"] == 1
    labels = DEPLOYMENTS[name]["spec"]["template"]["metadata"]["labels"]
    assert pdb["spec"]["selector"]["matchLabels"].items() <= labels.items()


def test_services_keep_connections_longer_than_kong():
    # Otherwise Kong can reuse a connection a service is closing at that moment: a 502
    # (docs/results/scaling.md). Kong's value is set explicitly, not left to its default.
    kong = container(DEPLOYMENTS["kong"])
    kong_s = int(next(e["value"] for e in kong["env"]
                      if e["name"] == "KONG_UPSTREAM_KEEPALIVE_IDLE_TIMEOUT"))
    for name in OURS:
        dockerfile = (BASE.parents[2] / "services" / name / "Dockerfile").read_text()
        cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
        args = cmd.split('"')[1::2]
        assert "--timeout-keep-alive" in args, name
        assert int(args[args.index("--timeout-keep-alive") + 1]) > kong_s, name


def test_kong_balances_across_ingest_pods_directly():
    # Through a normal Service address Kong's reused connections stayed tied to a leaving pod
    # (docs/results/scaling.md). Headless: the name returns each ready pod's address.
    service = next(d for d in DOCS if d["kind"] == "Service"
                   and d["metadata"]["name"] == "ingest-pods")
    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["selector"] == {"app": "ingest-svc"}
    kong = (BASE.parents[1] / "kong" / "kong.tpl.yml").read_text()
    assert "url: http://ingest-pods:8000" in kong
    # Kong refreshes the name every 5 s: the pod must keep answering longer than that
    pause = container(DEPLOYMENTS["ingest-svc"])["lifecycle"]["preStop"]["sleep"]["seconds"]
    assert pause >= 10
