.PHONY: keys up down test lint logs cluster cluster-down
keys:
	./scripts/gen_keys.sh

up: keys
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

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
