.PHONY: dev up down logs check test lint fmt migrate seed clean-verify

dev:            ## run locally with reload on :8080
	uv run uvicorn podium.main:app --host 127.0.0.1 --port 8080 --reload

up:             ## build + start the portal (docker)
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f web

check:          ## run the organizer's acceptance checker against the running portal
	python3 tools/run.py .dogfood.toml --fixtures fixtures/fixtures.json | tee acceptance-report.txt

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

migrate:        ## apply migrations to the local database
	uv run alembic upgrade head

seed:           ## load fixtures + demo accounts into the local database
	uv run python -m podium.seed

clean-verify:   ## what a judge does: fresh build, boot, run the checker
	docker compose down -v --remove-orphans || true
	docker compose build --no-cache
	docker compose up -d
	@echo "waiting for /healthz"; for i in $$(seq 1 60); do curl -fs http://localhost:8080/healthz >/dev/null 2>&1 && break; sleep 1; done
	$(MAKE) check
