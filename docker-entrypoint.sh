#!/bin/sh
set -e

case "$1" in
    serve)
        # Single instance only: concurrent starts would migrate at once.
        if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
            python manage.py migrate --noinput
        fi
        exec daphne -b 0.0.0.0 -p "${PORT:-8080}" taskflow.asgi:application
        ;;
    migrate)
        exec python manage.py migrate --noinput
        ;;
    *)
        exec "$@"
        ;;
esac
