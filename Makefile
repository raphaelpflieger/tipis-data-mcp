# Day-to-day commands (everything runs in Docker; see README).
.PHONY: lock test build up down logs check-drift

# Regenerate the hashed lockfiles after editing pyproject.toml (uv resolves, pip installs with --require-hashes)
lock:
	docker run --rm -v "$(PWD)":/w -w /w python:3.12-slim sh -c "pip install -q uv && uv pip compile pyproject.toml --generate-hashes -o requirements.txt && uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements-dev.txt"

test:
	@set -e; trap 'docker compose -f docker-compose.test.yml down -v' EXIT; docker compose -f docker-compose.test.yml build tests; docker compose -f docker-compose.test.yml run --rm tests

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

# compare the live analytics views with tipis-web's allowlist (needs .env + secrets, run on the box)
check-drift:
	docker compose run --rm -v "$(PWD)/analytics_allowlist.yml":/tmp/allowlist.yml:ro datamcp python -m datamcp.cli check-drift --allowlist /tmp/allowlist.yml
