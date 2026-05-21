FROM postgres:17-trixie@sha256:0027bef26712baaee437a4ea48fdf3d2d2e2bc5f0d81615374408ca320f3c7e3

ARG PG_SEARCH_VERSION
ARG PG_SEARCH_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN test -n "$PG_SEARCH_VERSION" \
    && test -n "$PG_SEARCH_SHA256" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        curl=8.14.1-2+deb13u3 \
        postgresql-17-pgaudit=17.1-2.pgdg13+1 \
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

RUN mkdir -p /var/log/pgaudit \
    && chown postgres:postgres /var/log/pgaudit \
    && chmod 0750 /var/log/pgaudit

COPY initdb.d/ /docker-entrypoint-initdb.d/
COPY scripts/pg_audit_export.sh /usr/local/bin/pg_audit_export.sh
RUN chmod 0755 /usr/local/bin/pg_audit_export.sh
