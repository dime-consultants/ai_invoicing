#!/bin/bash
set -e

echo "Starting Django application..."

if [ "${RUN_STARTUP_TASKS}" = "true" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput

    echo "Seeding tools..."
    python manage.py seed_tools 2>/dev/null || true

    echo "Syncing tools to UI..."
    python manage.py sync_tools_to_ui --output /app/tools_export.json 2>/dev/null || true
else
    echo "Skipping startup tasks (set RUN_STARTUP_TASKS=true to enable)"
fi

# If arguments are passed, run them instead of Daphne
if [ "$#" -gt 0 ]; then
    echo "Running command: $*"
    exec "$@"
else
    echo "Starting Daphne server..."
    exec daphne -b 0.0.0.0 -p 8000 config.asgi:application
fi