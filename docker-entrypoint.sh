#!/bin/bash
set -e

echo "Starting Django application..."

# Run migrations
echo "Running migrations..."
#python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Register all tools in the database
echo "Registering tools..."
python manage.py register_tools 2>/dev/null || true

# Sync tools to UI (export to a file that can be shared)
echo "Syncing tools to UI..."
python manage.py sync_tools_to_ui --output /app/tools_export.json 2>/dev/null || true

# Start the application
echo "Starting Daphne server..."
daphne -b 0.0.0.0 -p 8000 config.asgi:application
