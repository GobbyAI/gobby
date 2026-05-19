FROM postgres:17-trixie

ARG PG_SEARCH_VERSION
ARG PG_SEARCH_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN test -n "$PG_SEARCH_VERSION" \
    && test -n "$PG_SEARCH_SHA256" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        postgresql-17-pgaudit \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL \
        "https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/postgresql-17-pg-search_${PG_SEARCH_VERSION}-1PARADEDB-trixie_${arch}.deb" \
        -o /tmp/pg_search.deb \
    && echo "${PG_SEARCH_SHA256}  /tmp/pg_search.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/pg_search.deb \
    && rm /tmp/pg_search.deb \
    && rm -rf /var/lib/apt/lists/*

RUN { \
        echo "shared_preload_libraries = 'pg_search,pgaudit'"; \
        echo "pgaudit.log = 'write'"; \
    } >> /usr/share/postgresql/postgresql.conf.sample

COPY initdb.d/ /docker-entrypoint-initdb.d/
