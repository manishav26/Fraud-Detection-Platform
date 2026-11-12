"""The health check (scripts/k8s_health.py) and fail-closed Linkerd injection
(scripts/k8s_linkerd.sh), from the VM restart of 12 Nov."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("k8s_health", ROOT / "scripts" / "k8s_health.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def pod(name, node="k3d-fraud-agent-0", phase="Running", ready="True", proxy=True, annotations=None):
    init = [{"name": "linkerd-init"}] + ([{"name": "linkerd-proxy"}] if proxy else [])
    return {"metadata": {"name": name, "annotations": annotations or {}},
            "spec": {"nodeName": node, "initContainers": init, "containers": [{"name": "app"}]},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": ready}]}}


def node(name, ready="True"):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


def test_pods_created_without_their_proxy_are_found():
    pods = {"items": [pod("kong-a", proxy=False), pod("ingest-b"), pod("feature-job-0", proxy=False)]}
    assert h.unmeshed(pods) == ["kong-a", "feature-job-0"]


def test_a_pod_that_opts_out_on_purpose_is_not_counted():
    probe = pod("st3-probe", proxy=False, annotations={"linkerd.io/inject": "disabled"})
    assert h.unmeshed({"items": [probe]}) == []


def test_a_proxy_in_either_container_list_counts():
    p = pod("x", proxy=False)
    p["spec"]["containers"].append({"name": "linkerd-proxy"})   # older Linkerd: a normal container
    assert h.has_proxy(p)


def test_not_ready_and_not_running_pods_are_listed_finished_ones_are_not():
    pods = {"items": [pod("ok"), pod("ingest-c", ready="False"), pod("old", phase="Unknown", ready="False"),
                      pod("job-done", phase="Succeeded", ready="False")]}
    assert h.not_ready(pods) == ["ingest-c (Running, ready=False)", "old (Unknown, ready=False)"]


def test_nodes_not_ready_are_reported():
    nodes = {"items": [node("k3d-fraud-agent-0"), node("k3d-fraud-agent-1", ready="Unknown")]}
    [(problem, fix)] = h.node_problems(nodes)
    assert "k3d-fraud-agent-1" in problem and "docker ps" in fix


def test_a_stale_kubelet_certificate_says_which_node_to_restart():
    error = ('Error from server: Get "https://172.18.0.2:10250/containerLogs/fraud/cassandra-2/x": '
             "tls: failed to verify certificate: x509: certificate is valid for 127.0.0.1, ::1, "
             "172.18.0.6, not 172.18.0.2")
    problem, fix = h.kubelet_problem("k3d-fraud-agent-0", error)
    assert "old address" in problem and fix.startswith("docker restart k3d-fraud-agent-0")


def test_each_node_is_reached_through_one_running_pod_on_it():
    pods = {"items": [pod("a", node="n1"), pod("b", node="n1"), pod("c", node="n2", phase="Pending")]}
    assert h.pod_per_node(pods) == {"n1": "a"}


def test_cassandra_counts_only_up_and_normal_nodes():
    status = "Datacenter: datacenter1\n--  Address\nUN  10.42.4.47 ...\nUN  10.42.1.51 ...\nDN  10.42.3.46 ...\n"
    assert h.cassandra_up(status) == 2


def test_linkerd_injection_fails_closed_with_kube_system_exempt():
    text = (ROOT / "scripts" / "k8s_linkerd.sh").read_text()
    assert "webhookFailurePolicy=Fail" in text
    assert 'linkerd install "${FAIL_CLOSED[@]}"' in text and 'linkerd upgrade "${FAIL_CLOSED[@]}"' in text
    assert "kubectl label namespace kube-system config.linkerd.io/admission-webhooks=disabled" in text
    # and it checks the live setting instead of assuming the install took
    assert "jsonpath='{.webhooks[0].failurePolicy}'" in text and '[ "$policy" = Fail ]' in text


def test_k8s_up_ends_with_the_health_check():
    up = (ROOT / "scripts" / "k8s_up.sh").read_text()
    assert "scripts/k8s_health.py" in up
    assert up.index("scripts/k8s_health.py") > up.index("wait_for mlflow")
