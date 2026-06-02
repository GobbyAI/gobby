# gwiki Daemon And Web Contracts

This guide defines the daemon/web contract for consuming `gwiki --format json`.
The Rust CLI owns wiki behavior, vault layout, source lifecycle, indexing
internals, and command-specific JSON. Gobby daemon, MCP, cron, and web code
consume those commands only through `GwikiGateway`.

## Boundary

Daemon and web code must not call `gwiki` with direct subprocess calls. All
routes, MCP tools, watcher jobs, and scheduled jobs go through `GwikiGateway`,
which owns binary resolution, scope flags, timeout enforcement, JSON parsing,
stderr capture, and daemon HTTP/MCP envelope normalization.

The gateway may normalize transport metadata around the CLI result, but it must
preserve command-specific payloads. Upstream CLI JSON must include `scope`,
`command`, and the fields listed below for each command. When a command can
degrade, the payload must carry structured degradation data instead of forcing
daemon-side file inspection or schema inference.

## Request Context

Every gateway call carries explicit scope:

| Context | Requirement |
| :--- | :--- |
| Project wiki | Pass project scope resolved from Gobby project context. |
| Topic wiki | Pass topic scope when the caller targets a global topic. |
| Caller identity | Preserve session/user metadata in daemon envelopes; do not pass it as wiki schema. |
| Timeout | Use per-command gateway timeouts. Never let route, MCP, watcher, or cron code call subprocess APIs directly. |
| Output format | Always pass `--format json`; human text output is not a daemon contract. |

## Operation Classes

| Command | Required arguments | Operation class | Notes |
| :--- | :--- | :--- | :--- |
| `gwiki status --format json` | Scope flags | Read-only | Reports scope/vault/index readiness. |
| `gwiki index --format json` | Scope flags | Scheduled write | Rebuilds derived index state. May also be explicit maintenance. |
| `gwiki search --format json` | Query and scope flags | Read-only | Returns scoped search hits and degradation metadata. |
| `gwiki read --format json` | Exactly one of `--path` or `--title` | Read-only | `not_found`, `invalid_request`, and `ambiguous` are JSON payload statuses. |
| `gwiki backlinks --format json` | Target path/title and scope flags | Read-only | Returns same-scope backlinks. |
| `gwiki ingest-file --format json` | File path and scope flags | Explicit write | Daemon attach maps to this command after upload/staging. |
| `gwiki ingest-url --format json URL...` | One or more URLs and scope flags | Explicit write | CLI owns fetching, persistence, failure classification, and batch indexing. |
| `gwiki collect --format json` | Scope flags | Explicit write | Processes inbox items into raw sources. |
| `gwiki research --format json` | Research prompt/options and scope flags | Explicit or scheduled write | Scheduled research may run from cron; accepted output remains CLI-owned. |
| `gwiki compile --format json` | Compile target/options and scope flags | Explicit write | Writes compiled wiki material only under CLI rules. |
| `gwiki audit --format json` | Scope flags and audit options | Read-only | Scheduled audits are allowed, but audit remains read-only unless a future fix command says otherwise. |
| `gwiki health --format json` | Scope flags | Read-only | Scheduled health checks are read-only status/report jobs. |
| `gwiki sources --format json` | Scope flags | Read-only | CLI owns source record schema and missing-raw degradations. |
| `gwiki remove-source --id <SOURCE_ID> --format json [--dry-run\|--yes] [--keep-asset]` | `--id`; one of preview or confirmation intent | Explicit write | `--dry-run` previews only; `--yes` confirms mutation; `--keep-asset` preserves raw asset files. |
| `gwiki refresh --format json [--scope project\|topic] [--id <SOURCE_ID>...] [--dry-run]` | Optional scope override, repeated source IDs, optional dry-run | Scheduled write | May be explicit when user-triggered; CLI owns re-fetch, hashing, writes, failures, and changed-source batch indexing. |

## Common JSON Rules

Successful command JSON must include:

| Field | Requirement |
| :--- | :--- |
| `command` | CLI command name, such as `"search"` or `"refresh"`. |
| `scope` | Scope identity, including enough project/topic data for daemon/web callers to avoid cross-scope leakage. |
| Command payload | Command-specific fields listed in the sections below. |
| `degradations` | Present when work completed with missing optional services, missing raw files, stale indexes, setup gaps, or other actionable degradation. |

Nonzero exits are gateway errors, but `GwikiGateway` must preserve `stderr` and
parse structured JSON from stdout or stderr when available. Commands with
all-failed batch behavior still return useful structured stdout; the gateway
must include it in the typed failure.

Timeouts are gateway-owned. A timeout result must identify the command, scope,
timeout value, elapsed time when known, stderr/stdout captured before
termination, and structured degradation or setup guidance when available. Route,
MCP, watcher, and cron layers must not retry writes by reimplementing CLI logic.

## Command Contracts

### `status`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"status"` |
| `scope` | Project/topic identity. |
| `vault` or equivalent status payload | Vault root/readiness, setup guidance, and file/index availability. |
| `degradations` | Missing setup, missing optional stores, stale index, or unavailable services. |

### `index`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"index"` |
| `scope` | Project/topic identity. |
| `indexed` | Counts for `documents`, `chunks`, `links`, `sources`, and `ingestions` when available. |
| `changed_paths` or equivalent | Paths the CLI considered changed when available. |
| `degradations` | Missing stores, partial indexing, or setup guidance. |

`index` writes derived index state only. Filesystem wiki markdown remains the
source of truth.

### `search`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"search"` |
| `scope` | Project/topic identity. |
| `query` | Query string or structured query metadata. |
| `results` | Scoped hits with path, title/snippet, score/rank, and provenance when available. |
| `degradations` | BM25-only fallback, missing embeddings, stale index, or setup guidance. |

### `read`

Required request fields:

| Selector | Requirement |
| :--- | :--- |
| `--path <PATH>` | Resolve by wiki path. Mutually exclusive with `--title`. |
| `--title <TITLE>` | Exact first-heading lookup. Mutually exclusive with `--path`. |

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"read"` |
| `scope` | Project/topic identity. |
| `requested` | Requested path/title identity. |
| `resolved_path` | Resolved wiki path when found. |
| `content` | Markdown content returned by the CLI. |
| `status` | Success or payload statuses such as `not_found`, `invalid_request`, or `ambiguous`. |
| `degradations` | Structured guidance for missing, invalid, ambiguous, or unavailable data. |

`not_found`, `invalid_request`, and `ambiguous` are successful subprocess JSON
payloads. The gateway must pass them through as command results.

### `backlinks`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"backlinks"` |
| `scope` | Project/topic identity. |
| `target` | Requested/resolved target path or title. |
| `backlinks` | Same-scope referring documents with path/title and link context when available. |
| `degradations` | Missing graph/index setup, stale graph, or unresolved target guidance. |

### `ingest-file`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"ingest-file"` |
| `scope` | Project/topic identity. |
| `source` | Source record with ID, kind, content hash, location/citation, and raw path. |
| `raw_path` | Written immutable raw source path. |
| `source_asset` | Stored asset metadata when the input has an asset. |
| `indexed` or `index_status` | CLI-owned index result or follow-up indexing signal. |
| `degradations` | Partial extraction, unsupported media, missing optional services, or setup guidance. |

Daemon upload/attach features stage files and then call
`GwikiGateway.ingest_file`. There is no upstream `gwiki attach` command.

### `ingest-url`

Required request fields:

| Field | Requirement |
| :--- | :--- |
| `URL...` | One or more URLs. The daemon passes the array unchanged to the gateway. |

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"ingest-url"` |
| `scope` | Project/topic identity. |
| `status` | `"ingested"`, `"partial"`, or `"failed"`. |
| `accepted` | Entries with `requested_url`, `final_url`, `raw_path`, and `source { id, kind, content_hash, location }`. |
| `failed` | Entries with `url`, `code`, and `message`. |
| `indexed` | Once-per-batch CLI indexing counts for accepted sources: `documents`, `chunks`, `links`, `sources`, `ingestions`. |
| `degradations` | Fetch, parse, extraction, setup, or indexing degradation when available. |

Partial success is a successful subprocess result. All-failed batches return
nonzero while preserving the same structured result on stdout when available.
The daemon must not fetch URLs, write URL raw sources, classify URL failures,
retry failed URLs as its own operation, or schedule a second index pass for the
accepted batch already indexed by the CLI.

### `collect`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"collect"` |
| `scope` | Project/topic identity. |
| `accepted` or equivalent | Inbox items accepted into raw sources. |
| `skipped` or equivalent | Items left in inbox or ignored with reasons. |
| `failed` | Items that failed with code/message. |
| `indexed` or `index_status` | CLI-owned index result or follow-up indexing signal. |
| `degradations` | Ambiguous items, unsupported formats, missing optional services, or setup guidance. |

### `research`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"research"` |
| `scope` | Project/topic identity. |
| `session` or equivalent | Durable research session/checkpoint identity. |
| `accepted` | Accepted raw research notes or source records when produced. |
| `failed` | Failed worker/source records with code/message. |
| `degradations` | Missing daemon capabilities, partial worker failure, unavailable model services, or setup guidance. |
| `indexed` or `index_status` | CLI-owned index result or follow-up indexing signal for accepted output. |

Scheduled research uses the same gateway contract as explicit research.

### `compile`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"compile"` |
| `scope` | Project/topic identity. |
| `written_paths` or equivalent | Wiki pages, source pages, concept pages, topic pages, index files, or output files written. |
| `changed_paths` | Changed wiki paths when available. |
| `provenance` or equivalent | Source-to-section provenance/citation metadata when available. |
| `indexed` or `index_status` | CLI-owned index result or follow-up indexing signal. |
| `degradations` | Missing completions, conflicts, unsupported overwrite, incomplete evidence, or setup guidance. |

### `audit`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"audit"` |
| `scope` | Project/topic identity. |
| `findings` | Unsupported claims, stale citations, broken links, duplicate concepts, or related findings. |
| `paths` or equivalent | Actionable wiki/source paths for findings. |
| `degradations` | Missing optional services, stale index, missing source data, or setup guidance. |

Audit is read-only for daemon/web integration.

### `health`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"health"` |
| `scope` | Project/topic identity. |
| `status` | Overall health status. |
| `findings` | Stale pages, uncited sources, broken links, duplicate concepts, uncompiled sources, or store readiness findings. |
| `paths` or equivalent | Actionable file paths and health snapshot paths when available. |
| `degradations` | Missing stores, stale index, setup guidance, or unavailable optional services. |

Health checks may run on a schedule, but the daemon treats the command result as
read-only status/report data.

### `sources`

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"sources"` |
| `scope` | Project/topic identity. |
| `sources` | CLI-owned records with `id`, `kind`, `title`, `location`, `citation`, `content_hash`, `fetched_at`, `compile_status`, `raw_path`, `raw_exists`, and optional `source_asset`. |
| `degradations` | Missing raw files or source-manifest issues. |

Source listing is CLI-owned. Daemon/web code must not parse `raw/INDEX.md`,
scan raw files, or normalize the source record schema beyond the standard
HTTP/MCP envelope.

### `remove-source`

Required request fields:

| Field | Requirement |
| :--- | :--- |
| `--id <SOURCE_ID>` | Source to preview or remove. |
| `--dry-run` | Preview removal; no mutation. |
| `--yes` | Confirm removal. |
| `--keep-asset` | Preserve source asset files during confirmed removal. |

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"remove-source"` |
| `scope` | Project/topic identity. |
| `status` | Removal or preview status. |
| `dry_run` | Boolean preview marker. |
| `source` | Source record selected by `--id`. |
| `removed_paths` | Paths removed by confirmed deletion. |
| `kept_paths` | Paths intentionally kept, including `--keep-asset` results. |
| `missing_paths` | Expected paths already missing. |
| `degradations` | Conservative source-removal degradations and missing-path details. |
| `follow_up` | For example, `["audit_recommended"]` when compiled claims may need review. |
| `index_status.index_required` | CLI-owned signal used by the daemon for follow-up indexing coordination. |

`remove-source` owns all planning and mutation. The daemon must not delete wiki
files/assets directly and must not infer removal effects by inspecting the
vault. Source removal is conservative: it removes raw source provenance and raw
assets only. Compiled wiki articles, concepts, health snapshots, research
checkpoints, and `outputs/` exports stay out of scope.

### `refresh`

Required request fields:

| Field | Requirement |
| :--- | :--- |
| `--scope project\|topic` | Optional CLI scope selector when the caller supplies one explicitly. |
| `--id <SOURCE_ID>` | Optional repeated source IDs to refresh. |
| `--dry-run` | Return planned refresh set without fetching or writing. |

Required result fields:

| Field | Meaning |
| :--- | :--- |
| `command` | `"refresh"` |
| `scope` | Project/topic identity. |
| `status` | `"refreshed"`, `"partial"`, `"unchanged"`, or `"failed"`. |
| `refreshed` | Changed entries with `id`, `kind`, `previous_content_hash`, `content_hash`, `changed`, `raw_path`, and `final_url`. |
| `unchanged` | Refreshed-but-identical source IDs. |
| `failed` | Entries with `id`, `code`, and `message`. |
| `indexed` | Once-per-batch CLI indexing counts for changed sources: `documents`, `chunks`, `links`, `sources`, `ingestions`. |
| `index_status.index_required` | CLI-owned signal for follow-up coordination. |
| `degradations` | Fetch, parse, hash, setup, or indexing degradation when available. |

Partial success is a successful subprocess result. All-failed batches return
nonzero while preserving the same structured result on stdout when available.
The daemon must not re-fetch sources, compute content hashes, write raw sources,
classify refresh failures, or schedule a second index pass for the changed batch
already indexed by the CLI.

## Gateway Error Handling

`GwikiGateway` returns command results for successful subprocess JSON, including
command-level statuses that represent user-visible failures. It raises typed
gateway errors for subprocess failures, malformed JSON, missing required fields,
and timeouts.

Gateway errors must preserve:

| Field | Requirement |
| :--- | :--- |
| `command` | The attempted command. |
| `scope` | Scope identity when available. |
| `exit_code` | Nonzero exit code when the process exited. |
| `stderr` | Captured stderr. |
| `stdout_json` | Parsed structured stdout when available. |
| `raw_stdout` | Raw stdout when JSON parsing failed or partial data matters. |
| `degradations` | Parsed structured guidance from stdout or stderr when available. |
| `timeout` | Timeout metadata for killed commands. |

Malformed or incomplete CLI JSON is a gateway contract failure. Daemon/web
layers should surface that failure rather than fabricating wiki domain data.
