.PHONY: keys up down test lint logs topics schema mlflow-check register promote cassandra-speed replay load cluster cluster-down k8s-up k8s-down
keys:
	./scripts/gen_keys.sh

up: keys
	docker compose up -d --build

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

# 10,000 writes plus read timings against the local Cassandra (needs `make schema` first)
cassandra-speed:
	python -m store.speed_test

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

k8s-down:
	kubectl delete namespace fraud --ignore-not-found
