FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.cargo/bin:${PATH}"
WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl libopencc-dev pkg-config \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain nightly \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml MANIFEST.in README.md ./
COPY querytrans ./querytrans
COPY luoxu-cutwords ./luoxu-cutwords
COPY luoxu ./luoxu
COPY luoxu_plugins ./luoxu_plugins
COPY openapi.yaml ghost.jpg nobody.jpg ./
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels . \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopencc1.1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels
COPY --from=builder /build/luoxu ./luoxu
COPY --from=builder /build/luoxu_plugins ./luoxu_plugins
COPY --from=builder /build/openapi.yaml ./openapi.yaml
COPY --from=builder /build/ghost.jpg /build/nobody.jpg ./
VOLUME ["/app/cache", "/data"]
EXPOSE 9008
CMD ["python", "-m", "luoxu", "--config", "/config/config.toml"]
