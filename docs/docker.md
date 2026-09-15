# Docker deployment

The deployment is split into three independently selectable services:

- `core` runs the Telegram indexer and its web server.
- `web` runs only the HTTP server against an existing database. It uses the
  same `luoxu:latest` image as `core`.
- `ocr` is a separate PaddleOCR service. It is optional and is not started by
  default.

## Configuration and persistent data

Create a local `config.toml` from `config.toml.example`. The application
always reads it from `/config/config.toml`; Compose mounts the local file
read-only. Do not put `config.toml` in the image or commit it.

The Telegram session is stored in the named `luoxu-session` volume mounted at
`/data`. Set `telegram.session_db` to `/data/luoxu` (or another path below
`/data`) so authentication survives container replacement. The cache is in
`luoxu-cache` and PostgreSQL data is in `luoxu-db`.

The default database is a local PGroonga-enabled PostgreSQL container using the
pinned `groonga/pgroonga:4.0.8-alpine-17-slim` image. Supply
`POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` through the environment;
`POSTGRES_PASSWORD` is required. The database has a `pg_isready` healthcheck,
and application services wait for it to pass.

For an external database, do not start the local `db` service. Put the external
PostgreSQL URL in the mounted `config.toml`, and run the relevant application
service with a Compose file that does not include the local-db dependency (or
maintain a small local override for that dependency). The local image is
PGroonga-enabled; an external PostgreSQL server must provide the extensions
required by `dbsetup.sql` itself.

## Database upgrades

Image replacement does **not** apply schema migrations. Existing database volumes
also do not rerun `dbsetup.sql`. Back up the database and stop all old indexers
and Python Web processes before upgrading. Apply only migrations not already
applied, in order: `001_access_control.sql`, `002_conversations.sql`,
`003_message_history.sql`, then `004_group_monitoring.sql`.

If `001`–`003` are already applied, the local Compose database can be upgraded
with (retain your existing Compose `-f` flags):

```sh
docker compose exec -T db sh -c \
  'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < migrations/004_group_monitoring.sql
```

Use your normal authenticated PostgreSQL connection instead for an external DB.
Fresh databases use only `dbsetup.sql`. Migration `004` adopts every existing
registered group once as a manual monitoring reference, without publishing it
or granting access. Old unwanted groups may resume; disable their manual
reference through the admin API. Remaining account/public references still keep
a group collecting. Repeating the migration does not resurrect disabled groups.
See [group access and monitoring](group-access.md) for the full lifecycle.

After migration, start the new image using the existing configuration and data.
`telegram.index_groups` is imported once; after that the database is authoritative.
A separate Web process can change stored references, but actual collection needs
a running upgraded indexer; its refresh loop observes changes about every 2 seconds.

## Published images: no local build needed

The main application is published as `ghcr.io/nimbly8836/luoxu:latest` and
`ghcr.io/nimbly8836/luoxu:sha-<full-commit>`. GHCR releases currently target
**linux/amd64**; use a local build on ARM hosts. `core` and the repository's
Python `web` service share this image. A separately maintained frontend image
is not included in it.

For an existing deployment (database and other dependencies already running),
keep your existing configuration, passwords, volumes, and Compose overrides:

```sh
# Optionally export LUOXU_IMAGE=ghcr.io/nimbly8836/luoxu:sha-<full-commit>
docker compose --profile core pull core
docker compose --profile core up -d --no-build --no-deps core
```

This updates only `core`, without rebuilding it or restarting the database,
OCR, or another frontend service. For a web-only deployment, replace the
profile and service name `core` with `web`. On a fresh installation, omit
`--no-deps` so Compose can start the configured database dependency. Do not
use `up --build` when you intend to use the published application image.

## Local builds and PyPI timeouts

The builder installs the build tools declared in `pyproject.toml` in a cached
layer before copying application source. Application packaging uses
`--no-build-isolation` and `PIP_NO_INDEX=1`, so a source-only change does not
create another isolated environment and download setuptools again. The pip
cache is enabled and mounted as a BuildKit cache. Runtime dependencies,
including transitive dependencies, are collected as wheels; installation in
the final image is explicitly offline (`--network=none`, `--no-index`).

The default PyPI socket timeout is 120 seconds, with 5 connection retries.
If the build host cannot reach PyPI reliably, choose an HTTPS mirror you trust:

```sh
LUOXU_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
LUOXU_PIP_TIMEOUT=120 LUOXU_PIP_RETRIES=5 \
docker compose --profile core build core

# Replace only the existing application container after the build succeeds.
docker compose --profile core up -d --no-build --no-deps core
```

These settings apply to local builds of both `core` and the repository's
Python `web` service, not to a separate frontend or OCR build. With plain
`docker build`, use `--build-arg PIP_INDEX_URL=...`,
`--build-arg PIP_DEFAULT_TIMEOUT=...`, and `--build-arg PIP_RETRIES=...`.
Do not disable TLS verification or put index credentials in build arguments.

The first build still needs network access for the base image, APT, Rust,
Cargo crates, and the dependency layer. This is **not** a fully offline build,
and increasing pip retries does not guarantee recovery from every mid-download
read timeout. Avoid `--no-cache` for normal updates; it forces dependency layers
to run again.

A build regression check after building the `builder` target is:

```sh
docker build --target builder -t luoxu:builder-check .
docker run --rm --network none -e PIP_NO_INDEX=1 luoxu:builder-check \
  python -m pip wheel --no-build-isolation --no-deps --wheel-dir /tmp/wheels .
```

This second command must package the application without downloading Python
build tools (the first build has already populated Cargo's dependency cache).

## Compose combinations

Compose profiles are explicit:

```sh
# Indexer + web + local database
POSTGRES_PASSWORD='change-me' docker compose --profile core up --build

# Web-only + local database
POSTGRES_PASSWORD='change-me' docker compose --profile web up --build

# Indexer and web together (they share the image and volumes)
POSTGRES_PASSWORD='change-me' docker compose --profile core --profile web up --build
```

Do not run `core` and `web` together with the same published port unless one
of them is given a different port or the web-only service is removed; both
publish port 9008 by default. The web-only mode is intended for a deployment
where the indexer is elsewhere, or for an override with a separate port.

## OCR

The OCR service is internal-only by default: it has no `ports` mapping and is
reachable only by other containers on the Compose network. Configure the core
application's database/OCR settings to use `http://ocr:12345/api` when OCR is
enabled. To expose it deliberately for local testing, add a local override
that publishes `12345`; do not expose it on an untrusted network without a
proxy and access controls.

`POST /api` requires a multipart field named `file` and returns the stable
shape:

```json
{"result": [{"text": "recognized text"}]}
```

`/health` returns `{"status":"ok"}`. Uploads are bounded to 10 MiB by
`OCR_MAX_UPLOAD_BYTES` (set a lower value if appropriate), and inference is
serialized to prevent concurrent Paddle model use. Temporary files are deleted
only after the worker has fully materialized Paddle's possibly-generator
result.

The default image is CPU-only and pins PaddlePaddle 3.2.2, PaddleOCR 3.2.0,
and aiohttp 3.12.15. The GPU override is a real alternate build: it uses
Paddle's pinned `paddlepaddle/paddle:3.2.2-gpu-cuda12.6-cudnn9.5` base image,
sets `PADDLEOCR_DEVICE=gpu:0`, and requests an NVIDIA GPU.

```sh
# CPU OCR (the core must be configured to call http://ocr:12345/api)
POSTGRES_PASSWORD='change-me' docker compose --profile core --profile ocr up --build

# GPU OCR; requires NVIDIA Container Toolkit and a compatible host
POSTGRES_PASSWORD='change-me' docker compose -f docker-compose.yml \
  -f docker-compose.gpu.yml --profile core --profile ocr up --build
```

The GPU image is amd64-only upstream. The main application can be built locally for the host architecture, but
its current GHCR publication workflow only publishes amd64. Verify availability
of the database/base images before deploying to ARM hardware.
