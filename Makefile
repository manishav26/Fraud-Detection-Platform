.PHONY: keys up down test lint logs
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
