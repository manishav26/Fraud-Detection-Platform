.PHONY: keys up down test lint logs topics schema cassandra-speed replay load cluster cluster-down
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

# 10,000 writes plus read timings against the local Cassandra (needs `make schema` first)
cassandra-speed:
	python -m store.speed_test

# Replay real payments through the gateway (needs data/processed/stream.parquet). Docs: docs/load.md
SPEED ?= 1000
LIMIT ?= 5000
replay:
	python -m load.replay --speed $(SPEED) --limit $(LIMIT)

# Fake payments at a fixed rate, for load tests (no data needed)
RPS ?= 50
SECONDS ?= 30
load:
	python -m load.synthetic --rps $(RPS) --seconds $(SECONDS)

lint:
	ruff check .

test:
	python -m pytest -q

# Local Kubernetes (used from week 4). Install once: brew install k3d
cluster:
	@k3d cluster list fraud >/dev/null 2>&1 || k3d cluster create fraud
	kubectl get nodes

cluster-down:
	k3d cluster delete fraud
