"""The only network paths allowed inside the cluster (week 4, step 3; threat 4). Design:
docs/network.md

deploy/k8s/base/network.yaml implements this table; tests/test_k8s_manifests.py checks the two
agree, and ST4 (security/tests/st4_network.py) checks the running cluster against it.
"""

# (from, to, port, why). Workloads are named by their `app` label.
PATHS = [
    ("kong", "ingest-svc", 8000, "payments"),
    ("kong", "alert-svc", 8000, "analyst case routes"),
    ("kong", "model-admin-svc", 8000, "admin model routes"),
    ("kong", "token-issuer", 8000, "tokens"),
    ("ingest-svc", "scoring-svc", 8000, "a decision per payment"),
    ("ingest-svc", "redpanda", 29092, "txn.raw, txn.scored, dlq.txn"),
    ("scoring-svc", "cassandra", 9042, "user history"),
    ("scoring-svc", "redpanda", 29092, "model.events (promotions)"),
    ("scoring-svc", "mlflow", 5000, "download a promoted model"),
    ("alert-svc", "redpanda", 29092, "txn.scored"),
    ("alert-svc", "postgres", 5432, "cases, audit log"),
    ("alert-svc", "siem-mock", 8000, "audit rows to the SIEM"),
    ("feature-job", "redpanda", 29092, "txn.raw"),
    ("feature-job", "cassandra", 9042, "features_by_txn, user_features"),
    ("model-admin-svc", "redpanda", 29092, "model.events"),
    ("model-admin-svc", "postgres", 5432, "audit log"),
    ("mlflow", "postgres", 5432, "registry database"),
    ("mlflow", "objectstore", 9000, "model files"),
    ("grafana", "prometheus", 9090, "dashboard queries"),
    ("prometheus", "ingest-svc", 8000, "metrics"),
    ("prometheus", "scoring-svc", 8000, "metrics"),
    ("prometheus", "alert-svc", 8000, "metrics"),
    ("prometheus", "model-admin-svc", 8000, "metrics"),
    ("prometheus", "token-issuer", 8000, "metrics"),
    ("prometheus", "siem-mock", 8000, "metrics"),
    ("prometheus", "feature-job", 8000, "metrics"),
    ("prometheus", "kong", 8100, "metrics"),
    ("prometheus", "redpanda", 9644, "metrics"),
]

# Reached from outside the cluster (the laptop, through the NodePort): the public entrance. Pods
# inside may not open it: their own egress rules don't allow it (ST2 tests it from outside).
PUBLIC = {("kong", 8000)}

# Every workload and the ports it listens on
LISTENS = {
    "kong": [8000, 8100], "token-issuer": [8000], "ingest-svc": [8000], "scoring-svc": [8000],
    "alert-svc": [8000], "model-admin-svc": [8000], "siem-mock": [8000], "feature-job": [8000], "mlflow": [5000],
    "redpanda": [29092, 9644], "cassandra": [9042], "postgres": [5432], "objectstore": [9000],
    "prometheus": [9090], "grafana": [3000],
}


def allowed(src, dst, port):
    """May a pod of `src` open `dst`:`port`? (src None: a pod with no labels.)"""
    return any(p[:3] == (src, dst, port) for p in PATHS)

# Ports that speak HTTP. The rest (Cassandra, Redpanda, Postgres) are databases whose client
# speaks first. ST4 needs to know: behind a Linkerd proxy, "connected" isn't enough (docs/mtls.md).
HTTP_PORTS = {8000, 8100, 5000, 9644, 9000, 9090, 3000}

