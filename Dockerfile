# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt


FROM python:3.13-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=taskflow.settings.prod

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin taskflow
COPY --chown=taskflow:taskflow . .

# Placeholders for this layer only: collectstatic imports prod.py, which
# refuses to boot without secrets.
RUN DJANGO_SECRET_KEY=build-only-placeholder-never-used-to-sign-anything-0000 \
    DJANGO_ALLOWED_HOSTS=localhost \
    POSTGRES_PASSWORD=placeholder \
    REDIS_URL=redis://localhost:6379/0 \
    python manage.py collectstatic --noinput

# The runtime user cannot write .pyc; compiling now saves every cold start.
RUN python -m compileall -q /app /opt/venv

USER taskflow

# Overridden by $PORT on Cloud Run; this value only matters to `docker run`.
EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
