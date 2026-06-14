#!/bin/bash
set -e

echo "Starting Django application..."

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Registering tools..."
python manage.py register_tools 2>/dev/null || true

echo "Syncing tools to UI..."
python manage.py sync_tools_to_ui --output /app/tools_export.json 2>/dev/null || true

echo "Starting Daphne server..."
daphne -b 0.0.0.0 -p 8000 config.asgi:application