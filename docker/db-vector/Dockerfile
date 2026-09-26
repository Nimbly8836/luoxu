FROM postgres:17-alpine AS vector-builder
ARG PGVECTOR_VERSION=v0.8.1
RUN apk add --no-cache build-base git \
 && git clone --branch "$PGVECTOR_VERSION" --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no \
 && make -C /tmp/pgvector OPTFLAGS="" with_llvm=no install

# Keep the existing PostgreSQL major version and PGroonga extension/data layout.
FROM groonga/pgroonga:4.0.8-alpine-17-slim
COPY --from=vector-builder /usr/local/lib/postgresql/vector.so /usr/local/lib/postgresql/
COPY --from=vector-builder /usr/local/share/postgresql/extension/vector* /usr/local/share/postgresql/extension/
