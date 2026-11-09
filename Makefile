.PHONY: keys token up down test lint scan logs topics schema db-schema st1 k8s-st1 k8s-st2 k8s-st3 k8s-st4 k8s-st4-db k8s-secrets-check k8s-linkerd st4-db st2 st7 verify-audit k8s-verify-audit k8s-st8-tamper k8s-st8-restore k8s-alert-stats k8s-cases st8-tamper st8-restore alert-stats mlflow-check register promote rollback model-history st9-tamper cassandra-speed backfill read-timing replay load stall-probe cluster cluster-down k8s-up k8s-secrets k8s-password k8s-apply k8s-update k8s-resilience k8s-down k8s-mlflow-check k8s-register k8s-promote k8s-rollback k8s-model-history k8s-backfill k8s-read-timing k8s-st9-tamper k8s-mlflow k8s-grafana k8s-prometheus snapshot k8s-snapshot k8s-gc-pauses baseline
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

# ST3: a pod outside the mesh, allowed by the network rules, calls scoring: refused (docs/mtls.md)
k8s-st3:
	python security/tests/st3_mesh.py

# Install Linkerd if missing, then `linkerd check` (docs/mtls.md)
k8s-linkerd:
	./scripts/k8s_linkerd.sh

# ST4: from inside the cluster, only the allowed network paths connect (docs/network.md)
k8s-st4:
	python security/tests/st4_network.py

# ST4, database logins: Cassandra's scoring login may only read and write its one table
k8s-st4-db:
	./scripts/k8s_secrets.sh run env CQLSH="kubectl -n fraud exec -i cassandra-0 -- cqlsh" ./security/tests/st4_cassandra_logins.sh

st4-db:
	./security/tests/st4_cassandra_logins.sh

# After make k8s-up: no dev password left in the cluster's Secrets; each Postgres login takes
# only its encrypted password (docs/secrets.md)
k8s-secrets-check:
	./security/tests/secrets_check.sh

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

# What alert-svc did with each txn.scored message since it started, from its /metrics page
ALERT_COUNTS = import urllib.request as u; print({l.split('\"')[1]: int(float(l.split()[-1])) for l in u.urlopen('http://localhost:8000/metrics').read().decode().splitlines() if l.startswith('alerts_messages_total{')})
alert-stats:
	@docker compose exec -T alert-svc python -c "$(ALERT_COUNTS)"

# End-to-end check of the model registry (needs `make up`). Design: docs/mlflow.md
mlflow-check:
	docker compose exec -T mlflow python /srv/smoke_test.py

# Add a trained model to the registry. This does not make it live. Design: docs/mlflow.md
MODEL_FOLDER ?= models/fraud-lgbm-v1
register:
	python -m registry.register --folder $(MODEL_FOLDER)

# Make a registered version live, through model-admin-svc, with an audit row. Runs Manisha's
# promotion gate first and stops if it says keep, unless OVERRIDE="why" (recorded). docs/mlflow.md
#   make promote VERSION=4 [REASON="..."] [OVERRIDE="..."]      make rollback REASON="..."
promote:
	python -m registry.promote --version $(VERSION) --reason "$(REASON)" --override "$(OVERRIDE)" --url $(URL)

# Back to the version live before the current one (no gate: it is the way back)
rollback:
	python -m registry.promote --rollback --reason "$(REASON)" --url $(URL)

# What is live and every model change, from model-admin-svc (admin token)
MODEL_HISTORY = TOKEN=$$(python -m load.credentials admin --url $(1)) && \
	curl -s -H "Authorization: Bearer $$TOKEN" $(1)/v1/model && echo && \
	curl -s -H "Authorization: Bearer $$TOKEN" "$(1)/v1/model/history?limit=10" && echo
model-history:
	@$(call MODEL_HISTORY,$(URL))

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

# Baseline (week 3, step 8): 1 minute warm-up, then 10 minutes of real payments at 30/s, then
# that window's numbers. make baseline ON=k8s RUN=1 (1, 2, 3 or fresh); ON=compose RUN=1 (1-3).
# Results: docs/results/baseline.md
baseline:
	./scripts/baseline.sh $(ON) $(RUN)

lint:
	ruff check .

# Supply-chain scans, the same as CI (ST10; docs/supply-chain.md): secrets in git history,
# vulnerable pinned packages, risky Dockerfile and Kubernetes settings, vulnerable images.
# Pinned Docker images, nothing to install. One at a time: make scan WHAT=images
WHAT ?= all
scan:
	./scripts/scan.sh $(WHAT)

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

# Everything: build, copy into k3d, config, YAML, tables, restart all (needs `make cluster`).
# Slow; needed the first time and when a service is added. Docs: docs/k8s.md
k8s-up:
	./scripts/k8s_up.sh

# The cluster's passwords, encrypted in git (docs/secrets.md). Adds missing ones; ROTATE=1 makes
# all new. Then `make k8s-up` changes the logins. One password: make k8s-password NAME=...
ROTATE ?=
k8s-secrets:
	ROTATE=$(ROTATE) ./scripts/gen_secrets.sh

NAME ?= GRAFANA_ADMIN_PASSWORD
k8s-password:
	@./scripts/k8s_secrets.sh show $(NAME)

# Config maps, secrets and YAML only, no images (steps like HPA or NetworkPolicies). Pods whose
# YAML changed restart by themselves; RESTART lists ones that must re-read changed config:
#   make k8s-apply                    make k8s-apply RESTART="kong scoring-svc"
RESTART ?=
k8s-apply:
	./scripts/k8s_up.sh apply $(RESTART)

# Scale and resilience tests under real load (docs/results/scaling.md), one at a time:
#   make k8s-resilience TEST=scale     (also kill, rollout, drain; RERUN=1 for kill, drain again)
TEST ?= scale
k8s-resilience:
	PYTHON=python RERUN=$(RERUN) ./scripts/resilience_test.sh $(TEST)

# One of our services after a code change: rebuild, copy in, restart it (about a minute)
#   make k8s-update SVC=model-admin-svc
k8s-update:
	./scripts/k8s_up.sh update $(SVC)

# The cluster's registry, through tunnels (needs `make k8s-up`, and compose stopped). docs/k8s.md
k8s-mlflow-check:
	kubectl -n fraud exec -i deploy/mlflow -- python /srv/smoke_test.py

REGISTRY_TUNNELS := service/mlflow=5050:5000 pod/redpanda-0=9092:9092

k8s-register:
	./scripts/k8s_tunnel.sh $(REGISTRY_TUNNELS) -- python -m registry.register --folder $(MODEL_FOLDER)

# Promotions now go through Kong (port 8080) to model-admin-svc: only MLflow needs a tunnel
k8s-promote:
	./scripts/k8s_tunnel.sh service/mlflow=5050:5000 -- python -m registry.promote --version $(VERSION) \
		--reason "$(REASON)" --override "$(OVERRIDE)" --url http://localhost:8080

k8s-rollback:
	./scripts/k8s_tunnel.sh service/mlflow=5050:5000 -- python -m registry.promote --rollback \
		--reason "$(REASON)" --url http://localhost:8080

k8s-model-history:
	@$(call MODEL_HISTORY,http://localhost:8080)

# Load days 1-109 into the cluster's Cassandra, through a tunnel
k8s-backfill:
	./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- python -m store.backfill

k8s-verify-audit:
	./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh service/postgres=5432:5432 -- python -m audit.verify

k8s-st8-tamper:
	kubectl -n fraud exec -i postgres-0 -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=attacker < security/tests/st8_tamper_audit.sql

k8s-st8-restore:
	kubectl -n fraud exec -i postgres-0 -- psql -U fraud -d fraud -v ON_ERROR_STOP=1 -v row=$(ROW) -v actor=alert-svc < security/tests/st8_tamper_audit.sql

# Counts from each alert-svc pod: each handles its own share of txn.scored's partitions
k8s-alert-stats:
	@for p in $$(kubectl -n fraud get pods -l app=alert-svc -o name); do \
		echo "$$p $$(kubectl -n fraud exec $$p -- python -c "$(ALERT_COUNTS)")"; \
	done

# Cases in the cluster's Postgres, by status and decision
k8s-cases:
	kubectl -n fraud exec postgres-0 -- psql -U fraud -d fraud -c "SELECT status, decision, count(*) FROM cases GROUP BY 1, 2 ORDER BY 1, 2"

k8s-read-timing:
	./scripts/k8s_secrets.sh run ./scripts/k8s_tunnel.sh service/cassandra=9042:9042 -- python -m store.read_timing

# ST9 attack on the cluster: change a registered model's file behind MLflow's back
k8s-st9-tamper:
	kubectl -n fraud exec -i deploy/mlflow -- python - $(VERSION) < security/tests/st9_tamper_artifact.py

# The dashboard: http://localhost:3000 until Ctrl+C. docs/metrics.md
# Log in as admin; password: make k8s-password (compose: grafana_dev_only)
k8s-grafana:
	kubectl -n fraud port-forward service/grafana 3000:3000

# One run's numbers as markdown tables (docs/results/metrics.md): make k8s-snapshot MINUTES=10
MINUTES ?= 10
snapshot:
	python -m observability.snapshot --minutes $(MINUTES)

k8s-snapshot:
	./scripts/k8s_tunnel.sh service/prometheus=9090:9090 -- python -m observability.snapshot --minutes $(MINUTES)

# Cassandra's memory-cleanup pauses in the last MINUTES (they open the breaker; docs/metrics.md)
k8s-gc-pauses:
	@kubectl -n fraud logs cassandra-0 --since=$(MINUTES)m | grep GCInspector || echo "no pauses logged"

# Prometheus's own page, to check what it reads: http://localhost:9090/targets
k8s-prometheus:
	kubectl -n fraud port-forward service/prometheus 9090:9090

# Keeps a tunnel open for the web page, http://localhost:5050, until you press Ctrl+C
k8s-mlflow:
	kubectl -n fraud port-forward service/mlflow 5050:5000

# Deletes everything in the namespace, INCLUDING its disks: topics, history and the model registry.
# To pause and keep the data, use `k3d cluster stop fraud`.
k8s-down:
	kubectl delete namespace fraud --ignore-not-found
