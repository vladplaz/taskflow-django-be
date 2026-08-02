# TaskFlow backend

The API behind a Trello-style board app. People sign up, create a workspace,
invite each other, and fill boards with columns of cards. A card holds a
description, labels, a status, assignees, comments, files, and a history of
everything that happened to it. When two people have the same board open, each
sees the other's changes as they happen.

This is the server half. The React client lives in its own repository.

## Where it runs

Google Cloud Run, in `europe-west1`, as one container: `Dockerfile` builds a
single image and Daphne serves both the REST API and the WebSockets from it.
Postgres, Redis, and object storage are managed elsewhere — Prisma, Upstash,
and Cloudflare R2.

Pushing to `main` deploys it. `.github/workflows/ci.yml` runs the test suite,
then builds the image on the GitHub runner and pushes it to Artifact Registry,
runs migrations as a Cloud Run job, and starts the new revision with no traffic
behind a `candidate` tag. Traffic moves only once that revision answers
`/health`, so a container that cannot boot never reaches a user. GitHub
authenticates to Google by workload identity federation, so no key is stored in
the repository. Configuration lives on the service itself: the pipeline only
ever swaps the image.

## Stack

- **Django 6** and **Django REST Framework** — the API, session-cookie auth.
- **Channels** + **daphne** — WebSockets for the live board updates.
- **PostgreSQL** (psycopg 3) for data, **Redis** for the channel layer and the
  throttle cache.
- **drf-spectacular** — generates the OpenAPI schema the client is written against.
- **django-storages** + **Pillow** — attachments on S3 or local disk, with
  generated thumbnails.
- **ruff** for lint and formatting; Django's own test runner.

Postgres and Redis run in Docker (`compose.yaml`); Django runs on the host.

## Getting it running

```bash
cp .env.example .env
python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

API at `http://localhost:8000/api/v1/`, docs at `/api/v1/schema/swagger-ui/`.
Tests: `.venv/bin/python manage.py test`.

## Decisions worth knowing

**Authorization is queryset composition.** `apps/boards/access.py` exposes
`visible_boards_for`, `editable_boards_for`, `administered_boards_for`. REST
views, the WebSocket consumer, and profile lookups all filter through them, so a
rule change lands everywhere at once instead of in six places.

**Business rules live in `services.py`, not in serializers.** Keyword-only
functions inside `transaction.atomic()`, taking `select_for_update()` where two
requests could race. Anything that must not fire on a rollback goes through
`transaction.on_commit()`.

**Environments are separate settings modules, not `if DEBUG`.**
`taskflow/settings/` splits into base + dev/test/prod. Production refuses to boot
without real secrets, and `wsgi.py`/`asgi.py` default to `prod` so a forgotten
`DJANGO_SETTINGS_MODULE` fails loudly rather than serving with `DEBUG=True`.

**Nothing is really deleted.** Boards, lists, and tasks archive via
`is_archived`, and archiving cascades by filtering rather than by writing — so
archiving a column hides its cards without touching them. Everything is
restorable.

**Ordering uses sparse fractional positions.** Dropping a card between two others
writes the midpoint: one UPDATE, not a renumbering of the column.

**Attachments are uploaded before they are referenced.** A file is stored on its
own request; everything after that refers to it by id. That is why saving a
description on every keystroke pause cannot duplicate the images in it. Files are
private — no storage key ever reaches the client, and every read goes through the
same board access check as everything else.

**Storage backend is a settings change.** Name `AWS_STORAGE_BUCKET_NAME` and
uploads go to S3; leave it blank and they stay on disk. The serving endpoint asks
the backend for a signed URL and redirects if it gets one, streams the bytes if
it does not. Compose ships an S3 emulator under the `s3` profile so the bucket
path can be exercised locally.

**Three settings stop being optional behind a TLS-terminating proxy.**
`DJANGO_BEHIND_PROXY`, or `SECURE_SSL_REDIRECT` redirects to the URL it is
already on and the browser loops. `DJANGO_NUM_PROXIES`, or anonymous throttling
keys on a client-supplied `X-Forwarded-For` and is bypassable. And the SameSite
pair, when the client is served from another domain — `Lax` drops the session
cookie from cross-site requests, so login appears to work and every request
after it is a 403.

**Everything is paginated**, by default and without asking. Task activity is the
one endpoint using offset rather than page-number pagination, because a card
opens with five entries and loads ten at a time after that.
