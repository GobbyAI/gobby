# Remote Docker Stack Live-Test Runbook

This runbook validates the M0 topology with PostgreSQL, Qdrant, and FalkorDB on
physical machine A and one active Gobby daemon on physical machine B. It uses a
separate acceptance home and separate remote Docker stack. The production local
volumes stay intact and return without a data migration.

Before starting, ensure machine A has Docker and SSH access, all three machines
are online in the same tailnet, and machine B has `jq`, `nc`, and a running local
embedding/generation provider. The examples use LM Studio with the recommended
Nomic embedding catalog entry; load both the embedding model and a text-generation
model before the daemon starts.

## Safety contract

- Use the same Gobby commit on both machines.
- Use dedicated acceptance homes. Never point the test at the production database.
- Never copy `machine_id`; each acceptance home must generate its own identity.
- Never run `docker compose down`, `docker rm`, `docker rm -f`, `docker volume rm`,
  or any Docker prune command during this procedure.
- Container authorization is discovered after creation. Every stop/start verifies
  the immutable container ID, exact Compose working-directory label, and service
  label before acting.
- Keep the local production stack stopped during the remote test. Closed local
  ports prove the daemon cannot silently fall back to local datastores.
- Keep the remote acceptance volumes after the test until evidence has been
  reviewed and backed up. Their later deletion is a separate operator action.

## Topology and variables

Machine A is the remote datastore host. Machine B is this workstation and runs
the active daemon. Machine C is a tailnet device that has no grant to the
datastore ports.

On machine B, from the Gobby repository:

```bash
set -euo pipefail
export GOBBY_REPO="$(pwd -P)"
export LOCAL_PROD_HOME="$HOME/.gobby"
export LOCAL_ACCEPT_HOME="$HOME/.gobby-m0-acceptance"
export REMOTE_SSH="user@remote-host"
export REMOTE_REPO="/absolute/path/to/gobby"
export REMOTE_ACCEPT_HOME="/absolute/home/path/.gobby-m0-acceptance"
export REMOTE_TS_DNS="remote-host.example-tailnet.ts.net"
export REMOTE_TS_IP="100.x.y.z"
export LOCAL_TS_IP="100.a.b.c"
export DENIED_TS_IP="100.d.e.f"
export ACCEPT_HTTP_PORT="61887"
export ACCEPT_WS_PORT="61888"
export ACCEPT_UI_PORT="61889"
export ACCEPT_EMBEDDING_PROVIDER="lmstudio"
export ACCEPT_EMBEDDING_CATALOG_KEY="nomic-v1.5-f16"
export ACCEPT_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
export ACCEPT_EVIDENCE="$GOBBY_REPO/.gobby/acceptance/$ACCEPT_RUN_ID"
mkdir -p "$ACCEPT_EVIDENCE"
```

Replace the example addresses and paths with values from `tailscale status` and
`tailscale ip -4`. Record the resolved values:

```bash
{
  printf 'run_id=%s\n' "$ACCEPT_RUN_ID"
  printf 'local_ts_ip=%s\n' "$LOCAL_TS_IP"
  printf 'remote_ts_ip=%s\n' "$REMOTE_TS_IP"
  printf 'remote_ts_dns=%s\n' "$REMOTE_TS_DNS"
  printf 'denied_ts_ip=%s\n' "$DENIED_TS_IP"
  printf 'local_commit=%s\n' "$(git rev-parse HEAD)"
  printf 'local_version=%s\n' "$(uv run gobby --version)"
} | tee "$ACCEPT_EVIDENCE/identity.txt"

ssh "$REMOTE_SSH" \
  "cd '$REMOTE_REPO' && git rev-parse HEAD && uv run gobby --version" \
  | tee "$ACCEPT_EVIDENCE/remote-version.txt"
diff -u \
  <(git rev-parse HEAD) \
  <(ssh "$REMOTE_SSH" "cd '$REMOTE_REPO' && git rev-parse HEAD")
```

The final `diff` must be empty.

## Safe container identity helpers

Define these functions in each shell that will stop or start a stack:

```bash
capture_stack_ids() {
  stack_home="$(cd "$1" && pwd -P)" || return 1
  evidence_file="$2"
  services_dir="$stack_home/services"
  test -f "$services_dir/docker-compose.yml" || {
    printf 'missing Compose file: %s\n' "$services_dir/docker-compose.yml" >&2
    return 1
  }
  (
    set -o pipefail
    docker ps -aq --no-trunc \
      --filter "label=com.docker.compose.project.working_dir=$services_dir" \
      | sort -u > "$evidence_file"
  ) || return 1
  validate_captured_stack "$stack_home" "$evidence_file"
}

validate_captured_container() {
  container_id="$1"
  services_dir="$2"
  test "${#container_id}" = "64" || {
    printf 'container ID is not full length: %s\n' "$container_id" >&2
    return 1
  }
  case "$container_id" in
    *[!0-9a-f]*) printf 'invalid immutable container ID: %s\n' "$container_id" >&2; return 1 ;;
  esac
  actual_id="$(docker inspect --format '{{.Id}}' "$container_id")" || return 1
  test "$actual_id" = "$container_id" || {
    printf 'container ID changed: %s != %s\n' "$actual_id" "$container_id" >&2
    return 1
  }
  working_dir="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container_id")" || return 1
  test "$working_dir" = "$services_dir" || {
    printf 'unexpected Compose working directory: %s\n' "$working_dir" >&2
    return 1
  }
  service="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id")" || return 1
  case "$service" in
    postgres|qdrant|falkordb) printf '%s\n' "$service" ;;
    *) printf 'unexpected Compose service: %s\n' "$service" >&2; return 1 ;;
  esac
}

validate_captured_stack() {
  stack_home="$(cd "$1" && pwd -P)" || return 1
  evidence_file="$2"
  services_dir="$stack_home/services"
  test -f "$services_dir/docker-compose.yml" || return 1
  test -f "$evidence_file" || return 1
  test "$(wc -l < "$evidence_file" | tr -d ' ')" = "3" || {
    printf 'captured stack does not contain exactly three containers\n' >&2
    return 1
  }
  captured_services=""
  while IFS= read -r container_id; do
    test -n "$container_id" || return 1
    service="$(validate_captured_container "$container_id" "$services_dir")" || return 1
    captured_services="${captured_services}${service}\n"
    docker inspect --format '{{.Id}} {{.Name}} {{.State.Status}}' "$container_id" || return 1
  done < "$evidence_file"
  test "$(printf '%b' "$captured_services" | sort -u)" = "$(printf 'falkordb\npostgres\nqdrant')" || {
    printf 'captured stack service set is incomplete or duplicated\n' >&2
    return 1
  }
}

stop_captured_stack() {
  stack_home="$(cd "$1" && pwd -P)" || return 1
  evidence_file="$2"
  services_dir="$stack_home/services"
  validate_captured_stack "$stack_home" "$evidence_file" || return 1
  while IFS= read -r container_id; do
    validate_captured_container "$container_id" "$services_dir" >/dev/null || return 1
    docker stop --time 30 "$container_id" || return 1
  done < "$evidence_file"
}

start_captured_stack() {
  stack_home="$(cd "$1" && pwd -P)" || return 1
  evidence_file="$2"
  services_dir="$stack_home/services"
  validate_captured_stack "$stack_home" "$evidence_file" || return 1
  while IFS= read -r container_id; do
    validate_captured_container "$container_id" "$services_dir" >/dev/null || return 1
    docker start "$container_id" || return 1
  done < "$evidence_file"
}
```

These helpers require full immutable IDs and refuse a stack unless it resolves to
exactly one each of `postgres`, `qdrant`, and `falkordb` in the expected Compose
working directory. Every stop or start revalidates the complete set, then revalidates
each container immediately before acting. They never remove a container or volume.

## Phase 1: protect and inventory the local installation

Create a current restore-verified hub backup while the production stack is
healthy:

```bash
uv run gobby health
uv run gobby status | tee "$ACCEPT_EVIDENCE/local-status-before.txt"
uv run gobby hub-backup \
  --output "$LOCAL_PROD_HOME/backups/hub/$ACCEPT_RUN_ID-pre-remote-test" \
  --json | tee "$ACCEPT_EVIDENCE/local-hub-backup.json"
test -f "$LOCAL_PROD_HOME/backups/hub/$ACCEPT_RUN_ID-pre-remote-test/manifest.json"
shasum -a 256 "$LOCAL_PROD_HOME/bootstrap.yaml" \
  | tee "$ACCEPT_EVIDENCE/local-bootstrap.sha256"
capture_stack_ids "$LOCAL_PROD_HOME" "$ACCEPT_EVIDENCE/local-stack.ids" \
  | tee "$ACCEPT_EVIDENCE/local-stack-before.txt"
```

Do not continue if the backup command, manifest check, or three-container
identity check fails.

## Phase 2: provision the isolated stack on machine A

On machine A:

```bash
set -euo pipefail
export REMOTE_REPO="/absolute/path/to/gobby"
export REMOTE_ACCEPT_HOME="/absolute/home/path/.gobby-m0-acceptance"
export REMOTE_TS_DNS="remote-host.example-tailnet.ts.net"
export REMOTE_TS_IP="100.x.y.z"
export ACCEPT_RUN_ID="the-run-id-created-on-machine-b"
cd "$REMOTE_REPO"
git fetch --all --prune
git switch 0.5.0
git pull --ff-only
uv sync
cargo build -p gobby-daemon
export GOBBY_HOME="$REMOTE_ACCEPT_HOME"
uv run gobby install
uv run gobby status
```

The remote home must be new or reserved for this acceptance run. The install
creates an isolated Compose project and persistent volumes under that home.

In the Tailscale policy editor, add one least-privilege grant with machine B as
the source, machine A as the destination, and only these permissions:

```json
{
  "src": ["100.a.b.c/32"],
  "dst": ["100.x.y.z"],
  "ip": ["tcp:60891", "tcp:6333", "tcp:16379"]
}
```

Use the actual `$LOCAL_TS_IP` and `$REMOTE_TS_IP`. Remove any broader grant that
would also authorize machine C. Tailscale's current grants syntax is documented
at <https://tailscale.com/docs/reference/syntax/grants>.

Still on machine A, expose only the Tailscale address and capture the generated
container identities:

```bash
export GOBBY_HOME="$REMOTE_ACCEPT_HOME"
uv run gobby datastores expose --bind "$REMOTE_TS_IP" --host "$REMOTE_TS_DNS"
uv run gobby status
mkdir -p "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID"
capture_stack_ids \
  "$REMOTE_ACCEPT_HOME" \
  "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID/remote-stack.ids" \
  | tee "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID/remote-stack-before.txt"

docker ps --format '{{.ID}} {{.Names}} {{.Ports}}' \
  | tee "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID/docker-ports.txt"
```

Every published datastore port must show `$REMOTE_TS_IP`, never `0.0.0.0`,
`::`, a public address, or the machine's LAN address.

## Phase 3: prepare the isolated daemon home on machine B

Copy only the remote acceptance bootstrap and shared authentication material.
Do not copy `machine_id`:

```bash
mkdir -p "$LOCAL_ACCEPT_HOME"
chmod 700 "$LOCAL_ACCEPT_HOME"
scp "$REMOTE_SSH:$REMOTE_ACCEPT_HOME/bootstrap.yaml" \
  "$LOCAL_ACCEPT_HOME/bootstrap.remote-source.yaml"
scp "$REMOTE_SSH:$REMOTE_ACCEPT_HOME/.secret_kek" \
  "$LOCAL_ACCEPT_HOME/.secret_kek"
scp "$REMOTE_SSH:$REMOTE_ACCEPT_HOME/local_cli_token" \
  "$LOCAL_ACCEPT_HOME/local_cli_token"
chmod 600 \
  "$LOCAL_ACCEPT_HOME/bootstrap.remote-source.yaml" \
  "$LOCAL_ACCEPT_HOME/.secret_kek" \
  "$LOCAL_ACCEPT_HOME/local_cli_token"
test ! -e "$LOCAL_ACCEPT_HOME/machine_id"
```

Generate the local remote-mode bootstrap without printing its credentialed DSN:

```bash
export LOCAL_ACCEPT_HOME REMOTE_TS_DNS ACCEPT_HTTP_PORT ACCEPT_WS_PORT ACCEPT_UI_PORT
uv run python - <<'PY'
import os
import re
from pathlib import Path

import yaml

home = Path(os.environ["LOCAL_ACCEPT_HOME"])
source = yaml.safe_load((home / "bootstrap.remote-source.yaml").read_text())
dsn = str(source["database_url"])
remote_host = os.environ["REMOTE_TS_DNS"]
remote_dsn, replacements = re.subn(
    r"@(localhost|127\.0\.0\.1):",
    f"@{remote_host}:",
    dsn,
    count=1,
)
if replacements != 1:
    raise SystemExit("refusing DSN whose loopback host cannot be replaced exactly once")
source.update(
    {
        "hub_backend": "postgres",
        "datastore_mode": "remote",
        "database_url": remote_dsn,
        "daemon_port": int(os.environ["ACCEPT_HTTP_PORT"]),
        "bind_host": "localhost",
        "websocket_port": int(os.environ["ACCEPT_WS_PORT"]),
        "ui_port": int(os.environ["ACCEPT_UI_PORT"]),
    }
)
target = home / "bootstrap.yaml"
target.write_text(yaml.safe_dump(source, sort_keys=False))
target.chmod(0o600)
PY
```

Run the remote-mode installer from machine B. It must report that Docker
provisioning is skipped and all three remote preflight checks pass:

```bash
export GOBBY_HOME="$LOCAL_ACCEPT_HOME"
uv run gobby install --no-interactive
test -s "$LOCAL_ACCEPT_HOME/machine_id"
```

Record both identities. Their values must differ:

```bash
cat "$LOCAL_ACCEPT_HOME/machine_id" \
  | tee "$ACCEPT_EVIDENCE/machine-b.id"
ssh "$REMOTE_SSH" \
  "cat '$REMOTE_ACCEPT_HOME/machine_id'" \
  | tee "$ACCEPT_EVIDENCE/machine-a.id"
test "$(cat "$ACCEPT_EVIDENCE/machine-a.id")" != \
  "$(cat "$ACCEPT_EVIDENCE/machine-b.id")"
```

## Phase 4: stop the local production runtime without removing it

Inventory every active local session, notify its operator that the daemon is
entering a maintenance window, and record the session refs that must be resumed.
Do not continue until every operator has acknowledged or paused their session:

```bash
unset GOBBY_HOME GOBBY_CONFIG
uv run gobby sessions list --status active --json \
  | tee "$ACCEPT_EVIDENCE/local-active-sessions-before-stop.json"
```

Then, on machine B:

```bash
unset GOBBY_HOME GOBBY_CONFIG
uv run gobby stop
stop_captured_stack "$LOCAL_PROD_HOME" "$ACCEPT_EVIDENCE/local-stack.ids" \
  | tee "$ACCEPT_EVIDENCE/local-stack-stop.txt"

for port in 60891 6333 16379; do
  if nc -z 127.0.0.1 "$port"; then
    printf 'local datastore port still open: %s\n' "$port" >&2
    exit 1
  fi
done
```

The saved local container IDs must remain inspectable with state `exited`.
From this point onward, Phase 9 is the mandatory recovery path after any test
failure. Restore the local containers and daemon before diagnosing an optional
acceptance check.

## Phase 5: start the active daemon on machine B

Run the acceptance daemon directly so the installed production OS service and
its home cannot be selected accidentally:

```bash
export GOBBY_HOME="$LOCAL_ACCEPT_HOME"
export GOBBY_CONFIG="$LOCAL_ACCEPT_HOME/config.yaml"
export PATH="$GOBBY_REPO/target/debug:$PATH"
mkdir -p "$LOCAL_ACCEPT_HOME/logs"
uv run python -m gobby.runner --config "$GOBBY_CONFIG" \
  >"$LOCAL_ACCEPT_HOME/logs/acceptance-daemon.log" 2>&1 &
export ACCEPT_DAEMON_PID="$!"
printf '%s\n' "$ACCEPT_DAEMON_PID" > "$ACCEPT_EVIDENCE/acceptance-daemon.pid"

for attempt in $(seq 1 60); do
  startup_done="$(
    curl --fail --silent \
      "http://127.0.0.1:$ACCEPT_HTTP_PORT/api/admin/startup-progress" \
      | jq -r '.done // false' 2>/dev/null || true
  )"
  test "$startup_done" = "true" && break
  sleep 1
done
test "$startup_done" = "true"
curl --fail --silent "http://127.0.0.1:$ACCEPT_HTTP_PORT/api/health" \
  | tee "$ACCEPT_EVIDENCE/daemon-health.json"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby status \
  | tee "$ACCEPT_EVIDENCE/remote-mode-status.txt"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby lease status \
  | tee "$ACCEPT_EVIDENCE/lease-status.txt"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" gdaemon schema verify
GOBBY_HOME="$LOCAL_ACCEPT_HOME" gdaemon schema version --json \
  | tee "$ACCEPT_EVIDENCE/schema-version.json"
```

The status must identify machine B as the active daemon, report remote
PostgreSQL/Qdrant/FalkorDB healthy, and show no local Compose lifecycle action.

Configure the isolated stack's embedding model through the running acceptance
daemon, then wait for the staged switch to finish. A final `not_found` status
means the completed run removed its journal as designed:

```bash
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby embeddings switch \
  "$ACCEPT_EMBEDDING_CATALOG_KEY" \
  --provider "$ACCEPT_EMBEDDING_PROVIDER" \
  | tee "$ACCEPT_EVIDENCE/embedding-switch-start.json"

for attempt in $(seq 1 120); do
  switch_status="$(
    GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby embeddings switch --status
  )"
  switch_state="$(printf '%s' "$switch_status" | jq -r '.status')"
  test "$switch_state" = "not_found" && break
  test "$switch_state" = "running" || {
    printf '%s\n' "$switch_status" >&2
    exit 1
  }
  sleep 5
done
test "$switch_state" = "not_found"
printf '%s\n' "$switch_status" \
  | tee "$ACCEPT_EVIDENCE/embedding-switch-final.json"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" "$HOME/.gobby/bin/gcode" embeddings doctor \
  | tee "$ACCEPT_EVIDENCE/embedding-doctor.json"
```

The doctor output must name the selected model, dimension, and local provider
endpoint. `gobby status` must also show a healthy text-generation model before
Phase 7; load or repair that model on machine B before continuing.

## Phase 6: network and ACL acceptance

From machine B:

```bash
nc -vz "$REMOTE_TS_DNS" 60891
nc -vz "$REMOTE_TS_DNS" 6333
nc -vz "$REMOTE_TS_DNS" 16379
curl --fail --silent "http://$REMOTE_TS_DNS:6333/healthz" \
  | tee "$ACCEPT_EVIDENCE/qdrant-health.txt"
tailscale ping "$REMOTE_TS_DNS" \
  | tee "$ACCEPT_EVIDENCE/tailscale-ping.txt"
```

From machine C, which must have no matching grant:

```bash
for port in 60891 6333 16379; do
  if nc -w 3 -z "$REMOTE_TS_DNS" "$port"; then
    printf 'ACL FAILURE: unauthorized port reachable: %s\n' "$port" >&2
    exit 1
  fi
done
```

Capture that output on machine C and copy it to
`$ACCEPT_EVIDENCE/non-acl-rejection.txt`. A timeout or policy denial passes;
successful connection to any port fails the acceptance.

## Phase 7: task, session, vector, and graph continuity

Use the daemon API so the active daemon performs the writes. `jq` is required
for the evidence extraction below:

```bash
export ACCEPT_TOKEN="$(cat "$LOCAL_ACCEPT_HOME/local_cli_token")"
export ACCEPT_API="http://127.0.0.1:$ACCEPT_HTTP_PORT/api"
export ACCEPT_PROJECT_ID="$(
  GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby projects list --json \
    | jq -r '.[] | select(.repo_path == env.GOBBY_REPO) | .id' \
    | head -n 1
)"
test -n "$ACCEPT_PROJECT_ID"

curl --fail --silent \
  -H "Authorization: Bearer $ACCEPT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg project "$ACCEPT_PROJECT_ID" \
    --arg run "$ACCEPT_RUN_ID" \
    '{title:("M0 remote acceptance " + $run), task_type:"simple_fix", category:"test", project_id:$project, validation_criteria:"Remote task survives daemon restart."}')" \
  "$ACCEPT_API/tasks" \
  | tee "$ACCEPT_EVIDENCE/task-create.json"

curl --fail --silent \
  -H "Authorization: Bearer $ACCEPT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg project "$ACCEPT_PROJECT_ID" \
    --arg run "$ACCEPT_RUN_ID" \
    --arg cwd "$GOBBY_REPO" \
    '{external_id:("m0-remote-" + $run), source:"Codex", project_id:$project, cwd:$cwd, title:"M0 remote acceptance"}')" \
  "$ACCEPT_API/sessions/register" \
  | tee "$ACCEPT_EVIDENCE/session-register.json"

curl --fail --silent \
  -H "Authorization: Bearer $ACCEPT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg project "$ACCEPT_PROJECT_ID" \
    --arg run "$ACCEPT_RUN_ID" \
    '{content:("M0 remote vector and graph roundtrip " + $run), memory_type:"fact", project_id:$project, source_type:"user", tags:["m0-remote-acceptance"]}')" \
  "$ACCEPT_API/memories" \
  | tee "$ACCEPT_EVIDENCE/memory-create.json"

GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby memory reindex-embeddings \
  | tee "$ACCEPT_EVIDENCE/vector-reindex.txt"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby memory rebuild-graph \
  --project "$ACCEPT_PROJECT_ID" --wait --timeout 600 \
  | tee "$ACCEPT_EVIDENCE/graph-rebuild.txt"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby memory recall \
  --project "$ACCEPT_PROJECT_ID" --limit 10 \
  "M0 remote vector and graph roundtrip $ACCEPT_RUN_ID" \
  | tee "$ACCEPT_EVIDENCE/memory-recall.txt"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby memory graph-counts \
  --project "$ACCEPT_PROJECT_ID" --json \
  | tee "$ACCEPT_EVIDENCE/graph-counts.json"
```

The reindex must report the created memory, recall must return its ID, and graph
counts must show the rebuilt memory graph. Any unavailable embedding or text
generation provider fails this phase; fix the isolated acceptance configuration
and repeat it.

Prove continuity across a controlled daemon handoff/restart:

```bash
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby lease handoff
wait "$ACCEPT_DAEMON_PID"

uv run python -m gobby.runner --config "$GOBBY_CONFIG" \
  >>"$LOCAL_ACCEPT_HOME/logs/acceptance-daemon.log" 2>&1 &
export ACCEPT_DAEMON_PID="$!"
for attempt in $(seq 1 60); do
  startup_done="$(
    curl --fail --silent \
      "http://127.0.0.1:$ACCEPT_HTTP_PORT/api/admin/startup-progress" \
      | jq -r '.done // false' 2>/dev/null || true
  )"
  test "$startup_done" = "true" && break
  sleep 1
done
test "$startup_done" = "true"

curl --fail --silent \
  -H "Authorization: Bearer $ACCEPT_TOKEN" \
  "$ACCEPT_API/tasks/$(jq -r '.id' "$ACCEPT_EVIDENCE/task-create.json")" \
  | tee "$ACCEPT_EVIDENCE/task-after-restart.json"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby sessions list \
  --project "$ACCEPT_PROJECT_ID" --json \
  | tee "$ACCEPT_EVIDENCE/sessions-after-restart.json"
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby memory recall \
  --project "$ACCEPT_PROJECT_ID" --limit 10 \
  "M0 remote vector and graph roundtrip $ACCEPT_RUN_ID" \
  | tee "$ACCEPT_EVIDENCE/memory-after-restart.txt"
```

## Phase 8: PostgreSQL connection capacity

Use the credentialed DSN from the acceptance bootstrap without printing it:

```bash
export LOCAL_ACCEPT_HOME
uv run python - <<'PY' | tee "$ACCEPT_EVIDENCE/postgres-capacity.txt"
import os
from pathlib import Path

import psycopg
import yaml

bootstrap = yaml.safe_load((Path(os.environ["LOCAL_ACCEPT_HOME"]) / "bootstrap.yaml").read_text())
with psycopg.connect(bootstrap["database_url"]) as connection:
    with connection.cursor() as cursor:
        cursor.execute("SHOW max_connections")
        maximum = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT application_name, count(*) "
            "FROM pg_stat_activity WHERE datname = current_database() "
            "GROUP BY application_name ORDER BY application_name"
        )
        rows = cursor.fetchall()
        used = sum(int(row[1]) for row in rows)
print(f"max_connections={maximum}")
print(f"observed_connections={used}")
for application_name, count in rows:
    print(f"{application_name or '<unset>'}={count}")
if maximum - used < 20:
    raise SystemExit("fewer than 20 PostgreSQL connections remain as headroom")
PY
```

The acceptance requires at least 20 unused connections after normal CLI churn.

## Phase 9: return the Docker workload to machine B

First stop the acceptance daemon cleanly on machine B:

```bash
GOBBY_HOME="$LOCAL_ACCEPT_HOME" uv run gobby lease handoff
wait "$ACCEPT_DAEMON_PID"
if kill -0 "$ACCEPT_DAEMON_PID" 2>/dev/null; then
  printf 'acceptance daemon still running: %s\n' "$ACCEPT_DAEMON_PID" >&2
  exit 1
fi
```

Remove the datastore grant from the Tailscale policy. On machine B, verify all
three connections are denied before continuing:

```bash
for port in 60891 6333 16379; do
  if nc -w 3 -z "$REMOTE_TS_DNS" "$port"; then
    printf 'grant removal failed; remote port still reachable: %s\n' "$port" >&2
    exit 1
  fi
done
```

Then, on machine A, stop the exact captured remote containers:

```bash
export GOBBY_HOME="$REMOTE_ACCEPT_HOME"
stop_captured_stack \
  "$REMOTE_ACCEPT_HOME" \
  "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID/remote-stack.ids" \
  | tee "$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID/remote-stack-stop.txt"
```

Copy machine A's evidence directory to machine B:

```bash
scp -r \
  "$REMOTE_SSH:$REMOTE_ACCEPT_HOME/acceptance/$ACCEPT_RUN_ID" \
  "$ACCEPT_EVIDENCE/machine-a"
```

Restore the same local production containers by immutable ID:

```bash
unset GOBBY_HOME GOBBY_CONFIG
start_captured_stack "$LOCAL_PROD_HOME" "$ACCEPT_EVIDENCE/local-stack.ids" \
  | tee "$ACCEPT_EVIDENCE/local-stack-start.txt"

for attempt in $(seq 1 60); do
  nc -z 127.0.0.1 60891 && nc -z 127.0.0.1 6333 && nc -z 127.0.0.1 16379 && break
  sleep 1
done
shasum -a 256 -c "$ACCEPT_EVIDENCE/local-bootstrap.sha256"
uv run gobby start
uv run gobby health
uv run gobby status | tee "$ACCEPT_EVIDENCE/local-status-restored.txt"
uv run gobby lease status | tee "$ACCEPT_EVIDENCE/local-lease-restored.txt"
gdaemon schema verify
gdaemon schema version --json \
  | tee "$ACCEPT_EVIDENCE/local-schema-restored.json"
capture_stack_ids "$LOCAL_PROD_HOME" \
  "$ACCEPT_EVIDENCE/local-stack-restored.ids" \
  | tee "$ACCEPT_EVIDENCE/local-stack-restored.txt"
cmp "$ACCEPT_EVIDENCE/local-stack.ids" \
  "$ACCEPT_EVIDENCE/local-stack-restored.ids"
```

Watch `~/.gobby/logs/` for five continuous minutes. Any new warning or error
owned by this test or the restored runtime resets the five-minute window and
must be fixed before acceptance closes.

Resume every session recorded before the maintenance window and send each one a
continue message after the daemon is healthy.

## Completion record

Copy these results into the `M0 acceptance checklist` in
`docs/guides/shared-stack.md`:

- UTC run ID and exact commit/version on both machines.
- Machine A and machine B IDs, roles, and Tailscale addresses.
- PostgreSQL, Qdrant, and FalkorDB endpoints and schema identity.
- Machine B readiness output and machine C rejection output.
- Active lease owner before and after restart.
- Task, session, memory, vector, and graph evidence filenames.
- PostgreSQL connection count, maximum, and remaining headroom.
- Exact local and remote immutable container IDs.
- Successful restoration of the original local bootstrap hash and containers.
- Five-minute clean local-daemon observation window.

The remote acceptance stack remains stopped and recoverable. Keep it until the
evidence review is complete.
