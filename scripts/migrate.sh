#!/usr/bin/env bash
# Convenience wrapper for running Alembic migrations against the
# configured database (reads DB config from .env via app.core.config).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Running Alembic migrations..."
alembic upgrade head
echo "Migrations complete."
