#!/usr/bin/env bash
# Manual EC2 deployment (Phase 1 — no CI/CD pipeline required).
# Run ON the EC2 instance from the repo checkout.
set -euo pipefail

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building image (shared by api/worker/beat)"
docker compose -f docker-compose.prod.yml build

echo "==> Running database migrations"
docker compose -f docker-compose.prod.yml run --rm api alembic upgrade head

echo "==> Restarting containers"
docker compose -f docker-compose.prod.yml up -d

echo "==> Done. Health check:"
curl -fsS http://localhost:8000/healthz && echo
