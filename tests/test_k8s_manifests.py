"""The Kubernetes manifests keep their resilience settings (week 4, step 2; docs/k8s.md).

Read straight from deploy/k8s/base, so a later edit can't quietly drop a probe, a limit or the
rollout rule.
"""

import shutil
import subprocess
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
    # The scoring container's own CPU, not the pod's: the pod's sum includes the Linkerd proxy,
    # which once had no request and left the HPA unable to compute anything (fixed 10 Nov)
    [metric] = hpa["spec"]["metrics"]
    assert metric["type"] == "ContainerResource"
    target = metric["containerResource"]
    assert target["container"] == container(DEPLOYMENTS["scoring-svc"])["name"] == "scoring-svc"
    assert target["name"] == "cpu" and target["target"]["averageUtilization"] == 60


def test_every_injected_proxy_gets_resource_requests():
    """Linkerd injects a proxy into every pod; the namespace gives each one requests (so the
    scheduler counts it) and a memory limit, but no CPU limit."""
    ns = next(d for d in DOCS if d["kind"] == "Namespace")["metadata"]["annotations"]
    assert ns["linkerd.io/inject"] == "enabled"
    assert ns["config.linkerd.io/proxy-cpu-request"] and ns["config.linkerd.io/proxy-memory-request"]
    assert ns["config.linkerd.io/proxy-memory-limit"]
    assert "config.linkerd.io/proxy-cpu-limit" not in ns


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


def test_kong_sends_to_ingest_through_the_mesh_balancer():
    """A normal Service: Kong's Linkerd proxy balances each request across the ready ingest pods.
    Headless (until 11 Nov), Kong resolved the pods itself and served expired DNS answers: after
    both pods were replaced, its first payments went to gone addresses (502s)."""
    service = next(d for d in DOCS if d["kind"] == "Service"
                   and d["metadata"]["name"] == "ingest-svc")
    assert service["spec"].get("clusterIP") != "None"
    assert service["spec"]["selector"] == {"app": "ingest-svc"}
    kong = (BASE.parents[1] / "kong" / "kong.tpl.yml").read_text()
    assert "url: http://ingest-svc:8000" in kong
    # a leaving pod keeps answering while Kong's proxy stops sending to it
    pause = container(DEPLOYMENTS["ingest-svc"])["lifecycle"]["preStop"]["sleep"]["seconds"]
    assert pause >= 10
    # ingest's port is HTTP to the mesh (not opaque), so it is balanced per request
    ns = next(d for d in DOCS if d["kind"] == "Namespace")["metadata"]["annotations"]
    assert "8000" not in ns["config.linkerd.io/opaque-ports"].split(",")


# ---------- network isolation (week 4, step 3; docs/network.md) ----------

from security.tests.network_paths import LISTENS, PATHS

POLICIES = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "NetworkPolicy"}


def test_everything_is_denied_unless_allowed():
    deny = POLICIES["default-deny"]["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in deny and "egress" not in deny


def rules_of(policy, direction):
    """{(peer app, port)} a policy allows in one direction."""
    key, peer_key = ("ingress", "from") if direction == "in" else ("egress", "to")
    found = set()
    for rule in policy["spec"].get(key) or []:
        for peer in rule.get(peer_key, []):
            app = peer.get("podSelector", {}).get("matchLabels", {}).get("app")
            for port in rule.get("ports", []):
                if app:
                    found.add((app, port["port"]))
    return found


@pytest.mark.parametrize("app", sorted(LISTENS))
def test_each_policy_allows_exactly_the_table(app):
    # Each path on its app port and on Linkerd's proxy port (4143, docs/mtls.md); Prometheus
    # also reads every proxy's metrics (4191)
    policy = POLICIES[app]
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": app}}
    expect_in = {(s, port) for s, d, p, _ in PATHS if d == app for port in (p, 4143)}
    if app != "prometheus":
        expect_in.add(("prometheus", 4191))
    assert rules_of(policy, "in") == expect_in
    assert rules_of(policy, "out") == {(d, port) for s, d, p, _ in PATHS if s == app
                                       for port in (p, 4143)}


def test_network_yaml_is_generated_from_the_table():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_network_policies", BASE.parents[2] / "scripts" / "gen_network_policies.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert (BASE / "network.yaml").read_text() == gen.render(), \
        "run: python scripts/gen_network_policies.py"
    assert (BASE / "mesh-ports.yaml").read_text() == gen.mesh_ports(), \
        "run: python scripts/gen_network_policies.py"


def test_only_kong_takes_traffic_from_anywhere():
    for name, policy in POLICIES.items():
        for rule in policy["spec"].get("ingress") or []:
            if "from" not in rule:
                assert name == "kong" and rule["ports"] == [{"protocol": "TCP", "port": 8000}]


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_every_pod_has_its_own_account_and_no_api_token(kind):
    for d in DOCS:
        if d["kind"] != kind:
            continue
        spec = d["spec"]["template"]["spec"]
        name = d["spec"]["template"]["metadata"]["labels"]["app"]
        assert spec.get("serviceAccountName") == name, name
        if name != "prometheus":   # it reads the Kubernetes API
            assert spec.get("automountServiceAccountToken") is False, name


def test_cassandra_checks_logins_and_scoring_has_its_own():
    cassandra = next(d for d in DOCS if d["kind"] == "StatefulSet"
                     and d["metadata"]["name"] == "cassandra")
    start = " ".join(container(cassandra)["args"])
    assert "authenticator: PasswordAuthenticator" in start
    assert "authorizer: CassandraAuthorizer" in start
    refs = [e.get("secretRef", {}).get("name") for e in container(DEPLOYMENTS["scoring-svc"])["envFrom"]]
    assert "cassandra-credentials" in refs
    schema = (BASE.parents[2] / "scripts" / "create_schema.sh").read_text()
    assert "GRANT SELECT ON fraud.txn_by_user TO scoring" in schema
    assert "GRANT MODIFY ON fraud.txn_by_user TO scoring" in schema
    assert "GRANT ALL" not in schema and "SUPERUSER = true" in schema   # only fraud_admin


# ---------- mTLS with Linkerd (week 4, step 4; docs/mtls.md) ----------

def test_namespace_is_meshed_and_accepts_only_meshed_callers():
    ns = next(d for d in DOCS if d["kind"] == "Namespace")["metadata"]["annotations"]
    assert ns["linkerd.io/inject"] == "enabled"
    assert ns["config.linkerd.io/default-inbound-policy"] == "all-authenticated"
    # Cassandra's clients and node-to-node port (7000, the VM's 3 nodes), Redpanda, Postgres
    assert set(ns["config.linkerd.io/opaque-ports"].split(",")) == {"9042", "7000", "29092", "5432"}


def test_only_kongs_public_port_accepts_callers_outside_the_mesh():
    servers = [d for d in DOCS if d["kind"] == "Server"]
    open_to_all = [s for s in servers if s["spec"].get("accessPolicy") == "all-unauthenticated"]
    assert [(s["spec"]["podSelector"], s["spec"]["port"]) for s in open_to_all] == \
        [({"matchLabels": {"app": "kong"}}, 8000)]


def test_no_pod_opts_out_of_the_mesh():
    for d in DOCS:
        if d["kind"] in ("Deployment", "StatefulSet"):
            notes = d["spec"]["template"]["metadata"].get("annotations", {})
            assert notes.get("linkerd.io/inject") != "disabled", d["metadata"]["name"]


def test_each_port_of_a_shared_pod_names_exactly_its_callers():
    # Through the mesh every connection arrives on 4143, so for a pod whose ports have different
    # callers (Redpanda, Kong) the mesh checks the caller's identity per port
    from security.tests.network_paths import PUBLIC

    servers = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "Server"}
    authn = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "MeshTLSAuthentication"}
    for app, ports in LISTENS.items():
        if len(ports) < 2:
            continue
        for port in ports:
            if (app, port) in PUBLIC:
                continue
            server = servers[f"{app}-{port}"]["spec"]
            assert server["port"] == port and server["accessPolicy"] == "deny"
            names = {r["name"] for r in authn[f"{app}-{port}-callers"]["spec"]["identityRefs"]}
            assert names == {s for s, d, p, _ in PATHS if (d, p) == (app, port)}


def test_database_services_are_marked_opaque():
    for name, port in (("cassandra", "9042"), ("redpanda", "29092"), ("postgres", "5432")):
        svc = next(d for d in DOCS if d["kind"] == "Service" and d["metadata"]["name"] == name)
        assert svc["metadata"]["annotations"]["config.linkerd.io/opaque-ports"] == port


# ---------- supply chain and pod hardening (week 4, step 5; docs/supply-chain.md) ----------

@pytest.mark.parametrize("name", sorted(OURS))
def test_our_services_run_as_non_root_on_a_read_only_filesystem(name):
    pod = OURS[name]["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 10001
    c = container(OURS[name])
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert {"name": "tmp", "mountPath": "/tmp"}.items() <= next(
        m for m in c["volumeMounts"] if m["name"] == "tmp").items()
    dockerfile = (BASE.parents[2] / "services" / name / "Dockerfile").read_text()
    assert "USER 10001" in dockerfile


@pytest.mark.parametrize("kind", ["Deployment", "StatefulSet"])
def test_every_pod_uses_the_default_seccomp_filter(kind):
    for d in DOCS:
        if d["kind"] == kind:
            ctx = d["spec"]["template"]["spec"]["securityContext"]
            assert ctx["seccompProfile"] == {"type": "RuntimeDefault"}, d["metadata"]["name"]


def test_every_image_gets_security_updates():
    for dockerfile in (BASE.parents[2] / "services").glob("*/Dockerfile"):
        text = dockerfile.read_text()
        assert "apt-get upgrade" in text and "setuptools==" in text, dockerfile


# ---------- Redpanda logins and permissions (week 5, step 1; docs/kafka.md) ----------

KAFKA_USERS = {"ingest-svc": "kafka-ingest", "scoring-svc": "kafka-scoring",
               "alert-svc": "kafka-alert-svc", "model-admin-svc": "kafka-model-admin"}


@pytest.mark.parametrize("name", sorted(KAFKA_USERS))
def test_each_kafka_client_has_its_own_redpanda_user(name):
    refs = [e.get("secretRef", {}).get("name") for e in container(DEPLOYMENTS[name]).get("envFrom", [])]
    assert KAFKA_USERS[name] in refs


def test_the_feature_job_has_its_own_redpanda_user():
    job = next(d for d in DOCS if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "feature-job")
    refs = [e.get("secretRef", {}).get("name") for e in container(job)["envFrom"]]
    assert "kafka-feature-job" in refs


def test_each_redpanda_user_gets_only_its_topics():
    script = (BASE.parents[2] / "scripts" / "kafka_security.sh").read_text()
    grants = {}
    for line in script.splitlines():
        if line.startswith("acl --allow-principal") and "--topic" in line:
            words = line.split()
            user, ops = words[2], words[words.index("--operation") + 1]
            for topic in words[words.index("--topic") + 1].split(","):
                grants.setdefault(user, set()).add((topic, ops))
    assert grants == {
        "ingest": {("txn.raw", "write,describe"), ("txn.scored", "write,describe"),
                   ("dlq.txn", "write,describe")},
        "scoring": {("model.events", "read,describe")},
        "alert-svc": {("txn.scored", "read,describe")},
        "feature-job": {("txn.raw", "read,describe"), ("txn.parity", "read,describe"),
                        ("txn.bench", "read,describe")},
        "model-admin": {("model.events", "write,describe")},   # the only writer of promotions
        "tools": {("txn.parity", "write,describe"), ("txn.bench", "write,describe")},
        "label-sim": {("labels", "write,describe")},   # the only writer of labels (ST13)
    }
    # users and permissions before logins go on, or the services would be locked out
    assert script.index("security user") < script.index("acl --allow") < script.index("enable_sasl true")


def test_no_image_floats_on_latest():
    images = [c["image"] for d in DOCS if d["kind"] in ("Deployment", "StatefulSet")
              for c in d["spec"]["template"]["spec"]["containers"]]
    compose = (BASE.parents[2] / "compose.yaml").read_text()
    assert not [i for i in images if i.endswith(":latest") or ":" not in i]
    assert ":latest" not in compose


def test_the_headless_ingest_service_is_gone_for_good():
    services = {d["metadata"]["name"] for d in DOCS if d["kind"] == "Service"}
    assert "ingest-pods" not in services and "ingest-svc" in services
    up = (BASE.parents[2] / "scripts" / "k8s_up.sh").read_text()
    assert "delete service ingest-pods" in up and "delete service ingest-svc" not in up
    assert "ingest-pods" not in (BASE.parents[2] / "compose.yaml").read_text()


# ---------- the VM's 3-node Cassandra (week 5; deploy/k8s/vm, docs/cassandra.md) ----------

VM = BASE.parent / "vm"
VM_DOCS = {f.name: [d for d in yaml.safe_load_all(f.read_text()) if d] for f in VM.glob("*.yaml")}


def test_the_laptop_keeps_one_cassandra_node():
    cassandra = next(d for d in DOCS if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "cassandra")
    assert cassandra["spec"]["replicas"] == 1


def test_the_vm_overlay_is_the_base_plus_three_cassandra_nodes():
    kustomization = VM_DOCS["kustomization.yaml"][0]
    assert kustomization["resources"] == ["../base", "cassandra-peers.yaml"]
    assert [p["path"] for p in kustomization["patches"]] == ["cassandra-3-nodes.yaml"]


def test_every_overlay_puts_its_own_resources_in_the_fraud_namespace():
    """A base's `namespace:` doesn't reach the resources an overlay adds: each overlay sets its own
    (on the VM, cassandra-peers landed in `default` and the second node never found the first)."""
    for kustomization in BASE.parent.glob("*/kustomization.yaml"):
        assert yaml.safe_load(kustomization.read_text()).get("namespace") == "fraud", kustomization


CLUSTER_WIDE = {"Namespace", "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition"}


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl to build the overlays")
@pytest.mark.parametrize("overlay", ["base", "vm"])
def test_everything_kustomize_builds_lands_in_fraud(overlay):
    built = subprocess.run(["kubectl", "kustomize", str(BASE.parent / overlay)],
                           capture_output=True, text=True, check=True).stdout
    misplaced = [f"{d['kind']}/{d['metadata']['name']}" for d in yaml.safe_load_all(built)
                 if d and d["kind"] not in CLUSTER_WIDE and d["metadata"].get("namespace") != "fraud"]
    assert misplaced == []


def test_three_nodes_one_per_machine_started_one_at_a_time():
    [sts] = VM_DOCS["cassandra-3-nodes.yaml"]
    spec = sts["spec"]
    assert (spec["replicas"], spec["serviceName"], spec["podManagementPolicy"]) == \
        (3, "cassandra-peers", "OrderedReady")
    [rule] = spec["template"]["spec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]
    assert rule["topologyKey"] == "kubernetes.io/hostname"
    assert rule["labelSelector"] == {"matchLabels": {"app": "cassandra"}}
    [c] = spec["template"]["spec"]["containers"]
    seeds = {e["name"]: e["value"] for e in c["env"]}["CASSANDRA_SEEDS"]
    assert seeds == "cassandra-0.cassandra-peers.fraud.svc.cluster.local"
    assert {"name": "intra-node", "containerPort": 7000} in c["ports"]


def test_a_node_is_ready_only_when_cassandra_serves_clients():
    """Not the base's TCP check, which the Linkerd proxy answers before Cassandra is up: the
    next node must not start bootstrapping while this one still is."""
    [sts] = VM_DOCS["cassandra-3-nodes.yaml"]
    probe = sts["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert probe["$patch"] == "replace" and "tcpSocket" not in probe
    assert "nodetool statusbinary" in " ".join(probe["exec"]["command"])


def test_peers_find_each_other_and_a_drain_takes_one_node_at_most():
    service, budget = VM_DOCS["cassandra-peers.yaml"]
    assert service["spec"]["clusterIP"] == "None" and service["spec"]["publishNotReadyAddresses"]
    assert service["spec"]["ports"] == [{"name": "intra-node", "port": 7000}]
    assert service["metadata"]["annotations"]["config.linkerd.io/opaque-ports"] == "7000"
    assert budget["kind"] == "PodDisruptionBudget" and budget["spec"]["maxUnavailable"] == 1


def test_node_to_node_traffic_is_allowed_only_between_cassandra_nodes_and_meshed():
    from security.tests.network_paths import LISTENS, PATHS
    assert [p[:3] for p in PATHS if p[2] == 7000] == [("cassandra", "cassandra", 7000)]
    assert 7000 in LISTENS["cassandra"]
    ns = next(d for d in DOCS if d["kind"] == "Namespace")["metadata"]["annotations"]
    assert "7000" in ns["config.linkerd.io/opaque-ports"].split(",")


def test_copies_follow_the_overlay_and_cover_the_logins():
    up = (BASE.parents[2] / "scripts" / "k8s_up.sh").read_text()
    assert "base) CASSANDRA_RF=1" in up and "vm) CASSANDRA_RF=3" in up
    assert 'kubectl apply -k "deploy/k8s/$OVERLAY"' in up
    schema = (BASE.parents[2] / "scripts" / "create_schema.sh").read_text()
    assert "ALTER KEYSPACE fraud WITH replication" in schema
    assert "ALTER KEYSPACE system_auth WITH replication" in schema
    assert "repair -full system_auth" in schema


def pick_overlay(asked, running):
    import re
    import subprocess
    up = (BASE.parents[2] / "scripts" / "k8s_up.sh").read_text()
    func = re.search(r"^pick_overlay\(\) \{.*?^\}", up, re.DOTALL | re.MULTILINE).group(0)
    r = subprocess.run(["bash", "-c", f'{func}\npick_overlay "$1" "$2"', "_", asked, running],
                       capture_output=True, text=True, check=False)
    return r.returncode, r.stdout.strip()


def test_the_setup_comes_from_the_running_cluster_not_from_memory():
    """A plain `make k8s-apply` on the VM once tried to turn its 3-node Cassandra into 1."""
    assert pick_overlay("", "cassandra-peers") == (0, "vm")      # the VM, OVERLAY forgotten
    assert pick_overlay("vm", "cassandra-peers") == (0, "vm")
    assert pick_overlay("base", "cassandra-peers")[0] != 0       # would shrink it: refused
    assert pick_overlay("vm", "cassandra")[0] != 0               # would grow it in place: refused
    assert pick_overlay("", "cassandra") == (0, "base")          # the laptop
    assert pick_overlay("", "") == (0, "base")                   # no cluster yet
    assert pick_overlay("vm", "") == (0, "vm")                   # a new VM cluster
    assert pick_overlay("", "something-else")[0] != 0
