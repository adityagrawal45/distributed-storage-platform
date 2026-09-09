# Phase 12 — local developer workflow, mirroring .github/workflows/ci.yml
# job-for-job so `make <target>` reproduces exactly what CI runs
# before you push, not an approximation of it.
.PHONY: install lint format security test build ci clean

install:
	pip install -r requirements.txt

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy app

security:
	bandit -c pyproject.toml -r app
	pip-audit -r requirements.txt \
		--ignore-vuln PYSEC-2026-161 --ignore-vuln PYSEC-2026-248 --ignore-vuln PYSEC-2026-249 \
		--ignore-vuln PYSEC-2026-1942 --ignore-vuln PYSEC-2026-1941 --ignore-vuln PYSEC-2026-2281 \
		--ignore-vuln PYSEC-2026-2280 --ignore-vuln PYSEC-2026-1845 --ignore-vuln PYSEC-2026-1325
	@echo "(gitleaks not run here — install separately: https://github.com/gitleaks/gitleaks#installing. CI always runs it.)"

test:
	pytest -q

build:
	docker build -f docker/Dockerfile \
		--build-arg GIT_COMMIT=$$(git rev-parse --short HEAD) \
		--build-arg BUILD_VERSION=dev-local \
		-t nimbusfs-api:local .

# The same gate sequence ci.yml runs, in the same order (fail fast on
# lint/types before spending time on tests/security). NOT a guarantee
# CI will pass (CI also runs the container scan, which needs a
# registry and Trivy this target doesn't assume you have installed) —
# a strong, fast local signal before pushing, not a full substitute.
ci: lint typecheck test security
	@echo "Local checks passed. CI additionally runs: gitleaks and a Trivy container scan."

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
