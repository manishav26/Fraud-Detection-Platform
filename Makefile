.PHONY: keys token up down test lint logs topics schema db-schema st1 k8s-st1 k8s-st2 st2 st7 verify-audit k8s-verify-audit k8s-st8-tamper k8s-st8-restore k8s-alert-stats k8s-cases st8-tamper st8-restore alert-stats mlflow-check register promote st9-tamper cassandra-speed backfill read-timing replay load stall-probe cluster cluster-down k8s-up k8s-down k8s-mlflow-check k8s-register k8s-promote k8s-backfill k8s-read-timing k8s-st9-tamper k8s-mlflow
keys:
	./scripts/gen_keys.sh

# Print a token for one client:  TOKEN=$$(make -s token CLIENT=ana)   (docs/access.md)
CLIENT ?= merchant-1
token:
	@python -m load.credentials $(CLIENT) --url $(URL)

# Also creates topics and tables: compose's Cassandra keeps nothing across `make down`, and a
# missing keyspace stops scoring from starting. All three steps are safe to repeat.
up: keys
	@# Compose and the k3d cluster together run this laptop out of memory and stall Docker
	@if k3d cluster list fraud 2>/dev/null | grep -q " 1/1 "; then \
		echo "The k3d cluster is running. Stop it first: k3d cluster stop fraud"; exit 1; fi
	docker compose up -d --build
	@# Kong reads kong.yml only when it starts, and compose doesn't restart it when the file changes
	docker compose restart kong
	./scripts/create_topics.sh
	./scripts/create_schema.sh
	./scripts/create_db_schema.sh

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

# Redpanda topics (needs `make up` first). List and reasons: docs/topics.md
topics:
	./scripts/create_topics.sh

# Cassandra keyspace and table (needs `make up` first). Design: docs/cassandra.md
schema:
	./scripts/create_schema.sh

# Cases and audit-log tables, and alert-svc's login (needs `make up`). Design: docs/alerts.md
db-schema:
	./scripts/create_db_schema.sh

# Recompute the audit log's hash chain; names the first broken row (ST8)
verify-audit:
	python -m audit.verify

# ST1: 1,000 payments with bad tokens; all refused, none reach ingest (run with no other traffic)
st1:
	python security/tests/st1_tokens.py --url $(URL)

k8s-st1:
	python security/tests/st1_tokens.py --url http://localhost:8080 --rpk "kubectl -n fraud exec -i redpanda-0 -- rpk"

k8s-st2:
	python security/tests/st2_roles.py --url http://localhost:8080

# ST2: every role calls every route and is checked against the roles table (docs/access.md)
st2:
	python security/tests/st2_roles.py --url $(URL)

# ST7: one client floods at 200/s against a 50/s limit; another must barely notice
st7:
	python security/tests/st7_flood.py --url $(URL)

# ST8: edit the audit log's ROW-th row by hand as the database owner, then put it back
st8-tamper:
	docker compose exec -T postgres psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=attacker < security/tests/st8_tamper_audit.sql

st8-restore:
	docker compose exec -T postgres psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=alert-svc < security/tests/st8_tamper_audit.sql

# Cases opened since alert-svc started (until the metrics in step 7)
alert-stats:
	docker compose exec -T alert-svc python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/stats').read().decode())"

# End-to-end check of the model registry (needs `make up`). Design: docs/mlflow.md
mlflow-check:
	docker compose exec -T mlflow python /srv/smoke_test.py

# Add a trained model to the registry. This does not make it live. Design: docs/mlflow.md
MODEL_FOLDER ?= models/fraud-lgbm-v1
register:
	python -m registry.register --folder $(MODEL_FOLDER)

# Make a registered version live: make promote VERSION=1
promote:
	python -m registry.promote --version $(VERSION)

# ST9 attack: change a registered model's file behind MLflow's back (docs/results/promotion.md)
st9-tamper:
	docker compose exec -T mlflow python - $(VERSION) < security/tests/st9_tamper_artifact.py

# 10,000 writes plus read timings against the local Cassandra (needs `make schema` first)
cassandra-speed:
	python -m store.speed_test

# Load days 1-109 into Cassandra, so users have their real history (needs `make schema`)
backfill:
	python -m store.backfill

# How long history reads take, including for the largest users (needs `make backfill`)
read-timing:
	python -m store.read_timing

# Replay real payments through the gateway (needs data/processed/stream.parquet). Docs: docs/load.md
# URL=http://localhost:8080 sends to the k3d cluster instead of compose.
URL ?= http://localhost:8000
SPEED ?= 1000
LIMIT ?= 5000
replay:
	python -m load.replay --speed $(SPEED) --limit $(LIMIT) --url $(URL)

# Fake payments at a fixed rate, for load tests (no data needed)
RPS ?= 50
SECONDS ?= 30
load:
	python -m load.synthetic --rps $(RPS) --seconds $(SECONDS) --url $(URL)

# Run beside a load test, to see where a slow period comes from
stall-probe:
	python -m load.stall_probe --url $(URL)

lint:
	ruff check .

test:
	python -m pytest -q

# Local Kubernetes: 1 server + AGENTS worker nodes. Gateway NodePort 30080 -> localhost:8080;
# your models/ folder is mounted into every node at /models. Docs: docs/k8s.md
AGENTS ?= 2
cluster:
	@k3d cluster list fraud >/dev/null 2>&1 || k3d cluster create fraud --agents $(AGENTS) \
		-p "8080:30080@server:0" --volume "$(CURDIR)/models:/models@all"
	kubectl get nodes

cluster-down:
	k3d cluster delete fraud

# Build, import and deploy the scoring path to k3d (needs `make cluster`)
k8s-up:
	./scripts/k8s_up.sh

# The cluster's registry, through tunnels (needs `make k8s-up`, and compose stopped). docs/k8s.md
k8s-mlflow-check:
	kubectl -n fraud exec -i deploy/mlflow -- python /srv/smoke_test.py

REGISTRY_TUNNELS := service/mlflow=5050:5000 pod/redpanda-0=9092:9092

k8s-register:
	./scripts/k8s_tunnel.sh $(REGISTRY_TUNNELS) -- python -m registry.register --folder $(MODEL_FOLDER)

k8s-promote:
	./scripts/k8s_tunnel.sh $(REGISTRY_TUNNELS) -- python -m registry.promote --version $(VERSION)

# Load days 1-109 into the cluster's Cassandra, through a tunnel
k8s-backfill:
	./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- python -m store.backfill

k8s-verify-audit:
	./scripts/k8s_tunnel.sh service/postgres=5432:5432 -- python -m audit.verify

k8s-st8-tamper:
	kubectl -n fraud exec -i postgres-0 -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=attacker < security/tests/st8_tamper_audit.sql

k8s-st8-restore:
	kubectl -n fraud exec -i postgres-0 -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=alert-svc < security/tests/st8_tamper_audit.sql

# Counts from each alert-svc pod: each handles its own share of txn.scored's partitions
k8s-alert-stats:
	@for p in $$(kubectl -n fraud get pods -l app=alert-svc -o name); do \
		echo "$$p $$(kubectl -n fraud exec $$p -- python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/stats').read().decode())")"; \
	done

# Cases in the cluster's Postgres, by status and decision
k8s-cases:
	kubectl -n fraud exec postgres-0 -- psql -U fraud -d fraud -c "SELECT status, decision, count(*) FROM cases GROUP BY 1, 2 ORDER BY 1, 2"

k8s-read-timing:
	./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- python -m store.read_timing

# ST9 attack on the cluster: change a registered model's file behind MLflow's back
k8s-st9-tamper:
	kubectl -n fraud exec -i deploy/mlflow -- python - $(VERSION) < security/tests/st9_tamper_artifact.py

# Keeps a tunnel open for the web page, http://localhost:5050, until you press Ctrl+C
k8s-mlflow:
	kubectl -n fraud port-forward service/mlflow 5050:5000

# Deletes everything in the namespace, INCLUDING its disks: topics, history and the model registry.
# To pause and keep the data, use `k3d cluster stop fraud`.
k8s-down:
	kubectl delete namespace fraud --ignore-not-found
