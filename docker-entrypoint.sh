#!/bin/bash
# docker-entrypoint.sh
#
# Startup sequence:
#   migrate → seed_tools → collectstatic → daphne
#
# seed_tools replaces both register_tools and sync_tools_to_ui.
# It is idempotent — safe to run on every container start.
#
# The --export flag writes /app/tools_export.json so the frontend
# can load tool metadata at startup without an extra API call.
# That file is written to the app container's local filesystem;
# if the frontend needs it, mount a shared volume or fetch it via
# GET /api/tools/ instead.

set -e

echo "[entrypoint] Starting ai_invoicing..."

# ── One-shot startup tasks ────────────────────────────────────────────────────
# Run on the web container only, not on the Celery worker.
# The migrate service in docker-compose already runs migrations before
# this container starts, but we keep the guard here for local dev where
# docker-compose isn't used.

if [ "${RUN_STARTUP_TASKS}" = "true" ]; then
    echo "[entrypoint] Running migrations..."
    python manage.py migrate --noinput

    echo "[entrypoint] Seeding tools..."
    python manage.py seed_tools --export /app/tools_export.json

    echo "[entrypoint] Collecting static files..."
    python manage.py collectstatic --noinput
else
    echo "[entrypoint] Skipping startup tasks (RUN_STARTUP_TASKS != true)"
fi

# ── Command override ──────────────────────────────────────────────────────────
# If arguments are passed (e.g. by docker-compose `command:` or CI),
# run those instead of starting Daphne.
# Examples:
#   docker run ... python manage.py shell
#   docker run ... celery -A config worker ...

if [ "$#" -gt 0 ]; then
    echo "[entrypoint] Running command: $*"
    exec "$@"
fi

# ── Start Daphne ──────────────────────────────────────────────────────────────
echo "[entrypoint] Starting Daphne on 0.0.0.0:8000..."
exec daphne -b 0.0.0.0 -p 8000 config.asgi:application