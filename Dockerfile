FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.cargo/bin:${PATH}"
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl libopencc-dev pkg-config \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain nightly \
 && rm -rf /var/lib/apt/lists/*

# Use HTTPS indexes only; do not put credentials in build arguments.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=5

# Install PEP 517 tools before copying source. Read the project's declared
# requirements rather than maintaining a second list of build dependencies.
# Collect transitive runtime dependencies too, so runtime installation is offline.
COPY pyproject.toml requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"] + ["wheel"]))' > /tmp/build-requirements.txt \
 && pip install -r /tmp/build-requirements.txt \
 && pip wheel --wheel-dir /wheels -r requirements.txt

COPY MANIFEST.in README.md ./
COPY querytrans ./querytrans
COPY luoxu-cutwords ./luoxu-cutwords
COPY luoxu ./luoxu
COPY luoxu_plugins ./luoxu_plugins
COPY openapi.yaml ghost.jpg nobody.jpg ./
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/build/target \
    PIP_NO_INDEX=1 pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopencc1.1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN --network=none pip install --no-cache-dir --no-index --find-links=/wheels /wheels/*.whl \
 && rm -rf /wheels
COPY --from=builder /build/luoxu ./luoxu
COPY --from=builder /build/luoxu_plugins ./luoxu_plugins
COPY --from=builder /build/openapi.yaml ./openapi.yaml
COPY --from=builder /build/ghost.jpg /build/nobody.jpg ./
VOLUME ["/app/cache", "/data"]
EXPOSE 9008
CMD ["python", "-m", "luoxu", "--config", "/config/config.toml"]
