FROM postgres:18-trixie@sha256:41da01536bc3ae26308cefb0c57235e7488001360bdb15191eb0b7955b570299

ARG PG_SEARCH_VERSION
ARG PG_SEARCH_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN test -n "$PG_SEARCH_VERSION" \
    && test -n "$PG_SEARCH_SHA256" \
    && apt-get update \
    # Pin upstream versions; glob the Debian/PGDG revision because point releases
    # retire superseded revisions from the archive and break exact pins.
    && apt-get install -y --no-install-recommends \
        'ca-certificates=20250419*' \
        'curl=8.14.1-*' \
        'postgresql-18-pgaudit=18.*' \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL \
        "https://github.com/paradedb/paradedb/releases/download/v${PG_SEARCH_VERSION}/postgresql-18-pg-search_${PG_SEARCH_VERSION}-1PARADEDB-trixie_${arch}.deb" \
        -o /tmp/pg_search.deb \
    && echo "${PG_SEARCH_SHA256}  /tmp/pg_search.deb" | sha256sum -c - \
    && apt-get install -y --no-install-recommends /tmp/pg_search.deb \
    && rm /tmp/pg_search.deb \
    && rm -rf /var/lib/apt/lists/*

RUN echo "shared_preload_libraries = 'pg_search,pgaudit'" >> /usr/share/postgresql/postgresql.conf.sample

COPY initdb.d/ /docker-entrypoint-initdb.d/
