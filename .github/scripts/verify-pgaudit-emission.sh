#!/usr/bin/env bash
set -euo pipefail

: "${GOBBY_POSTGRES_TEST_CONTAINER:?GOBBY_POSTGRES_TEST_CONTAINER must be set}"
: "${GOBBY_POSTGRES_TEST_DB:?GOBBY_POSTGRES_TEST_DB must be set}"
: "${GOBBY_POSTGRES_TEST_USER:?GOBBY_POSTGRES_TEST_USER must be set}"

docker exec -i "${GOBBY_POSTGRES_TEST_CONTAINER}" \
  psql -X --set ON_ERROR_STOP=1 \
  --username "${GOBBY_POSTGRES_TEST_USER}" \
  --dbname "${GOBBY_POSTGRES_TEST_DB}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgaudit;
CREATE TEMP TABLE gobby_pgaudit_ci_probe (
  id integer PRIMARY KEY,
  value integer NOT NULL
);
INSERT INTO gobby_pgaudit_ci_probe VALUES (1, 0);
UPDATE gobby_pgaudit_ci_probe SET value = value + 1 WHERE id = 1;
SQL

for _ in $(seq 1 10); do
  if docker exec "${GOBBY_POSTGRES_TEST_CONTAINER}" sh -c \
    "grep -Eq 'LOG:  AUDIT: SESSION,.*UPDATE' /var/log/pgaudit/pgaudit-*.log"
  then
    printf '%s\n' "pgAudit emitted an UPDATE audit record"
    exit 0
  fi
  sleep 1
done

printf '%s\n' \
  "::error::pgAudit emitted no AUDIT: SESSION record for the UPDATE probe" >&2
docker exec "${GOBBY_POSTGRES_TEST_CONTAINER}" sh -c \
  'tail -n 100 /var/log/pgaudit/pgaudit-*.log' >&2 || true
exit 1
