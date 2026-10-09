# HTTP contract corpus

Recorded request/response cases for the HTTP surface that gdaemon's front door
proxies. pytest (`tests/contracts/test_http_corpus.py`) and the Rust harness both
replay them. The corpus is the parity gate for every Stage 2 native takeover: a
family flips from `proxy` to `native` only when gdaemon's handler replays its
cases equal.

## Case format

Each case is one JSON file at `schema_version` 2:

```json
{
  "schema_version": 2,
  "name": "health_ok",
  "family": "health",
  "backend": "up",
  "credential": "none",
  "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
  "response": {"status": 200, "headers": {"content-type": "application/json"}, "body": {"status": "@mask@"}},
  "mask": ["/response/body/status"]
}
```

- `name` matches the file stem.
- `backend` is the state of the Python backend behind the front door while the
  case runs: `up` or `down` (see [Backend state](#backend-state)).
- `request` is an executable recipe: the exact method, path, query, headers, and
  JSON body Python sends. It never holds an `Authorization` or runtime-grant
  header, and every value in it is a fixed non-secret constant.
- `response` is the normalized recording: the status, the allowlisted headers,
  and the body after secret redaction and masking.
- `mask` lists the RFC 6901 JSON pointers of volatile fields.

The loader rejects any case whose `schema_version` differs from the manifest's,
or whose `backend` is missing or neither `up` nor `down`.
A `schema_version` bump re-records every case.

## Manifest

`manifest.json` holds:

- `schema_version`, the corpus version;
- `cases`, the case file names in replay order;
- `families`, one entry per family: `{"parity": "proxy" | "native", "origin": "python" | "gdaemon"}`.
  - `parity` is the route backend the Rust harness replays the family under.
    `health` is `native`, because gdaemon implements it; every other family here
    is `proxy`.
  - `origin: python` families are recorded from the live e2e daemon. Their `up`
    cases are replayed by both harnesses.
  - `origin: gdaemon` families are authored from a gdaemon contract, such as the
    typed 503 the front door returns while the backend is down. Python neither
    records nor replays them.

## Backend state

Every case declares `backend`, and each harness replays a case only in that
state:

- Python replays and records only `up` cases of `origin: python` families,
  against the isolated front-door e2e daemon, whose backend is always up.
- Rust replays every case. An `up` case runs against a stub backend that serves
  the recorded response. A `down` case runs against a bound, non-listening
  loopback address that refuses connections.

Python cannot produce a `down` response, so `down` cases are authored by hand
from the gdaemon contract and never recorded. `front_door_backend_down` is the
typed 503 for a path the front door proxies. `health_backend_down` is the same 503 from
gdaemon's native `health` handler, which also sets `x-gobby-served-by: gdaemon`.

## Credential recipes

`credential` names the headers added immediately before Python sends a case.
They are materialized fresh on every run and never persisted:

- `none` adds nothing.
- `operator` adds the isolated daemon's operator bearer
  (`daemon_auth_headers(daemon_instance.gobby_home)`).
- `grant` adds `BoundaryHarness.grant_headers()`: the operator bearer, an
  encoded runtime grant, and the principal's identity headers.

The Rust harness replays against a stub backend and needs no credential.

## Response headers

Only these response headers are recorded and compared, on both sides:
`cache-control`, `content-type`, `retry-after`, `x-gobby-user-id`,
`x-gobby-machine-id`, and `x-gobby-key-id`.

## Normalization

Normalization applies to the copy the recorder writes and the copy replay
compares. The request that is sent is never normalized.

1. **Secret redaction** runs first, on every body, whatever `mask` says and
   whether or not a value varies. The value of any object key named `dsn`,
   `password`, `api_key`, `token`, `deployment_token`, `payload_checksum`,
   `signature`, or `proof`, at any depth, becomes `"@secret@"`.
2. **Masks** then replace each pointed-at value with `"@mask@"`, keeping the key.
   A pointer to an absent field leaves it absent. `~0` and `~1` unescape to `~`
   and `/`.

`mask_vector.json` is the shared vector (`{"input", "mask", "expected"}` per
entry) that both the Python and Rust helpers must reproduce, including a
redaction entry.

Masked fields in the first corpus:

- `health_ok`: `status` and `hook_runtime` depend on the host's services,
  `install_dir` on the checkout path, and `gterm_host` on the terminal host
  process.
- `config_values`: the per-fixture temporary `terminal_host/socket_dir`, under
  both `desired` and `active`.
- `runtime_handshake`: both `fencing_epoch` fields; the grant's `issued_at` and
  `expires_at`; and each capability's `valid_until` and `credential_generation`.
  It also masks `capabilities/postgres/role_name`, which the database derives
  from the deployment token, machine, project, and generation.

Every other grant field keeps its recorded value. `schema_identity` pins the
migration set, so a new migration re-records `runtime_handshake`.

## Re-recording

From the worktree root, against the isolated test hub:

```bash
GOBBY_RECORD_HTTP_CONTRACTS=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_http_corpus.py -k test_record_http_contracts
```

The recorder sends each committed case to a fresh front-door e2e daemon and
rewrites its file in place. The requests, credentials, and masks stay as
committed. Record twice and diff the results: any value that differs between
the two recordings needs a mask. Then run the module without the variable to
confirm that every case replays equal.
