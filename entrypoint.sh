#!/bin/sh
# Boot sequence: migrate → seed (idempotent) → print test logins → serve.
set -e
alembic upgrade head
python -m podium.seed
exec uvicorn podium.main:app --host 0.0.0.0 --port 8080 --proxy-headers
