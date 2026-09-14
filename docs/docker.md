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

The GPU image is amd64-only upstream. The CPU core image and the PGroonga image
are built/published for the host architectures supported by their upstream
images; verify architecture availability before deploying to ARM hardware.
