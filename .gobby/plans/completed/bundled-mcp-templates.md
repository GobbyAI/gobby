# Bundled MCP Server Templates and Project-Scoped Instances

Plan artifact: `.gobby/plans/bundled-mcp-templates.md`

**Plan ID:** bundled-mcp-templates

## Overview
`kind: framing`

Bundled MCP servers become opt-in, parameterised **templates** with the same
lifecycle as bundled rules and skills: YAML under
`src/gobby/install/shared/mcp/templates/`, synced to a registry table, cloneable
per project. A project (or the machine) **instantiates** a template into an
`mcp_servers` row with its own name, values, and secrets; the daemon runs one
process per instance and resolves a server name inside the caller's project
first, then the global scope. The first new template is the AWS Labs OpenAPI
MCP server. V2 proves the project-scoped flow end to end from a second
registered project using verification-only instance YAML that V2 itself
creates and removes; game-goblins is the first adopter after this plan ships
(two `openapi` instances, `lightspeed` and `tcgplayer`, committed as instance
YAML in that repository — an operator follow-up outside this plan's
deliverables and verification).
The six existing bundled servers (github, linear, brave-search, context7,
playwright, chrome-devtools) become templates; nothing is instantiated by
default any more.

Compatibility finding that shaped the plan: `awslabs.openapi-mcp-server`
(1.1.5, FastMCP 3.x, `mcp` 1.29 transitively) is stdio-only, emits a static tool
list, and runs in its own `uvx` environment, so gobby's `mcp>=2.0` pin never
meets it; the only unverified piece is the `mcp` 2.x `Client` negotiation
fallback against a 1.x server, which P1 proves before anything builds on it.

## Constraints
`kind: framing`

Decision record (confirmed in the planning session; every choice names the
`restraint` rung it stopped at):

- Template source is YAML under `src/gobby/install/shared/mcp/templates/`
  (parity with rules/skills, rung 2: reuse the bundled-content contract in
  `src/gobby/install/shared/AGENTS.md`). Templates sync to a new
  `mcp_server_templates` registry table (user decision for full parity);
  project-local templates live at `.gobby/mcp/templates/<name>.yaml` and must
  carry `override: true` when they shadow a bundled name.
- Instances are `mcp_servers` rows. New columns `template_id` (FK,
  `ON DELETE SET NULL`) and `template_values` (jsonb) record provenance so drift
  refresh can re-expand an instance from its template. Instance scope is the
  existing `project_id` column: a real project, or `GLOBAL_PROJECT_ID`
  (`src/gobby/storage/projects.py`) for every project on the machine.
- Runtime keys every live server by its row UUID. Name resolution
  (project first, then global) lives in the Python front door
  (`src/gobby/mcp_proxy/services/`); the manager/transport half exposes an
  id-keyed API only. This is the seam `docs/plans/rust-migration-epic.md`
  "Second wave" ports to the Rust multiplexer; tests written here are its
  parity fixtures. Same name in several projects is allowed; each instance is
  its own process (no process sharing — rung 1, no consumer).
- Secrets are project-scoped: `secrets.project_id` (NOT NULL, default
  `GLOBAL_PROJECT_ID`, unique `(name, project_id)`), resolved project first,
  then global. Existing rows stay global. `gobby secrets set NAME` scopes to
  the current registered project when run inside one and prints the scope;
  `--global` forces machine scope. Template `secret: true` params accept a
  secret *reference* only — `$secret:<name>`, or a bare name that already
  exists in the instance's scope; raw values are rejected on every surface.
- Instantiation surfaces all go through one `expand_template()`: project or
  global instance YAML (`.gobby/mcp/servers/<name>.yaml`,
  `~/.gobby/mcp/servers/<name>.yaml`) synced by the existing user-template
  path; `add_mcp_server(template=, values=, scope=)` MCP tool;
  `gobby mcp-proxy add-server --template … --set k=v [--global]`;
  `POST /api/mcp/servers` with `template`/`values`.
- `~/.gobby/mcp-servers.json`, `MCPConfigManager`, `DEFAULT_EXTERNAL_MCP_SERVERS`,
  `BUNDLED_EXTERNAL_MCP_SERVER_NAMES`, bundled-name global canonicalisation,
  `normalize_bundled_servers`, `install_default_mcp_servers`, and
  `_API_KEY_PROMPTS` are retired (rung 1; dead at runtime or superseded).
  `gobby install` instantiates nothing and prints the available templates.
- `openapi` template pins `awslabs.openapi-mcp-server@1.1.5`, passes
  `--log-level ERROR` (the documented `LOG_LEVEL` env var is not implemented
  upstream), and sets `connect_timeout: 120` (cold `uvx` resolve plus spec
  fetch exceed the 30 s default). Params: `api_name`, `api_base_url`,
  `spec_url` | `spec_path`, `auth_type` (none|bearer|api_key|basic),
  `auth_token`, `auth_api_key`, `auth_api_key_name`, `auth_api_key_in`,
  `auth_username`, `auth_password`, `include_tags`, `exclude_tags`,
  `allow_insecure_http`, `allow_private_networks`. Cognito and
  `ADDITIONAL_SPECS` are excluded (one instance per API). Prompts and
  resources stay unproxied (tools only).
- Upstream RFC awslabs/mcp#4122 marks the package for deprecation in favour of
  `FastMCP.from_openapi`. The template abstraction isolates that: a future
  swap edits `openapi.yaml` `command`/`args` only.

Forward-path alignment (`docs/architecture/evolution.md`,
`docs/architecture/hub-owned-files-home.md`, `ROADMAP.md`): templates,
instances, and secrets are hub semantics and live in Postgres; server
processes are machine execution and live in the per-daemon runtime; CLI
additions are thin HTTP clients like the rest of `gobby mcp-proxy`.

Schema serialisation: evolution.md forbids pairing schema leaves with herdr P2
or #19651 because all rewrite the `gcore` catalog identity files. Deliverable
2.1 is the only schema hop in this plan, carries all three changes, and takes
its version as `max(existing MIGRATIONS version, existing migration filename
prefix) + 1` at implementation start (411 at this writing — hops 408–410 landed
with #21140 while this plan was under review; rename the file if taken). No
other schema work may share its worktree.

Defaults chosen for non-material details:

- Instance YAML sync never deletes rows for removed files; removal is explicit
  (`remove_mcp_server`, `DELETE /api/mcp/servers/{name}`).
- Bundled template rows are `owner='gobby'`; user/project rows are
  `owner='user'`. Gobby-owned rows refresh on definition drift preserving the
  `enabled` toggle, exactly like `sync_rules`.
- `template_values` stores secret params as `$secret:<name>` references, never
  plaintext; `env`/`args` are the materialised expansion.
- Adoption: the first template sync attaches `template_id`/`template_values` to
  existing global rows whose name matches a Gobby-owned template and whose
  persisted `transport`, `url`, `command`, `args`, `env`, `headers`, and
  `connect_timeout` all equal the template's expansion of the row's inverted
  values — the single adoption predicate, defined once in 3.2; the first
  differing field is reported as `adoption_skipped` and the row is left
  untouched — so Josh's six rows keep working unchanged.
- `npm_config_prefer_offline=true` applies to every `npx`-launched instance
  (today only bundled names); chrome-devtools executable-path injection keys
  off the template's declared `runtime_hook`, carried on the instance config
  and materialised on the instance row at expansion so it survives template
  detachment — never off the template or instance name.
- `list_tools` from an external server stays cursorless (no server in scope
  paginates); `tools/list_changed` stays unhandled (the OpenAPI server never
  emits it).
- Shadowing ignores `enabled`: a disabled project-scoped row still hides the
  same-named global row, so a caller sees its own instance as disabled and
  never reaches the global instance's credentials. A `server_id` owned by
  another project resolves to the same unknown-server envelope as an unknown
  name.

Consumer sweep evidence (index covers `0.5.0`, this checkout):

- `gcode grep -F "BUNDLED_EXTERNAL_MCP_SERVER_NAMES" -g "src/gobby/**"`:
  `mcp_proxy/bundled.py`, `storage/mcp_servers.py`.
  `-g "tests/**"`: `tests/mcp_proxy/test_manager_coverage.py`,
  `tests/cli/installers/test_cli_installers_mcp_config.py`,
  `tests/storage/test_storage_mcp.py`.
- `gcode grep -F "is_bundled_external_mcp_server" -g "src/gobby/**"`:
  `mcp_proxy/bundled.py`, `mcp_proxy/transports/stdio.py`,
  `storage/mcp_servers.py`.
- `gcode grep -F "normalize_bundled_servers" -g "src/gobby/**"`:
  `cli/installers/mcp_config_defaults.py:194`, `runner_init/services.py:135,746`,
  `storage/mcp_servers.py:297,426,595`.
- `gcode grep -F "install_default_mcp_servers" -g "src/gobby/**"`:
  `cli/install_setup.py:346,372`, `cli/installers/__init__.py:16,34`,
  `cli/installers/mcp_config.py:15,40`, `cli/installers/mcp_config_defaults.py:26`.
  `-g "tests/**"`: `tests/cli/installers/test_cli_installers_mcp_config.py`,
  `tests/cli/test_install_setup.py`, `tests/cli/test_install_setup_gdaemon.py`.
- `gcode grep -F "MCPConfigManager" -g "src/gobby/**" -l`: `config/mcp.py` only.
  `-g "tests/**"`: `tests/config/test_config_mcp_config.py`,
  `tests/mcp_proxy/transports/test_sse_transport.py`.
- `gcode grep -F "_API_KEY_PROMPTS" -g "src/gobby/**"`:
  `cli/_install_prompts.py:22,131,297`, `cli/install.py:60,114`.
  `-g "tests/**"`: `tests/cli/test_install_prompts.py`, `tests/cli/test_cli_install.py`.
- `gcode grep -F "import_from_mcp_json" -g "src/gobby/**"`:
  `cli/installers/mcp_config_defaults.py:193`, `storage/mcp_imports.py:68`.
- `gcode grep -F "SecretStore(" -g "src/gobby/**" -l`: 25 files; every
  existing call keeps its signature (new keyword-only `project_id=None`
  defaults to global), so only the MCP resolution and CLI consumers change and
  are targeted in 2.3. Test consumers of the changed storage methods:
  `tests/storage/test_secrets.py`, `tests/storage/test_secrets_store.py`,
  `tests/storage/test_secret_set_atomic.py`, `tests/cli/test_cli_secrets.py`.
- `gcode grep -F "get_project_context" -g "src/gobby/mcp_proxy/**"`: the
  per-call project source used by 4.2 —
  `server.py:141`, `services/server_mgmt.py:59,157`,
  `services/session_context.py:68,98`, `services/tool_execution.py:354,690`.
- `gcode grep -F "get_client_session" -g "src/gobby/**"` (the facade method
  the 4.2 regex sweep omits): `github_triage/service.py:709`,
  `integrations/github_helper.py:138`, `servers/routes/source_control.py:132`,
  `sync/task_github_import.py:297` — every one passes the literal `"github"`
  and is targeted in 4.2 or 4.3.
- Migration carriers: `gcode grep -F "interactive_session_id_nullable" -g "crates/**" -l`
  → `crates/gcore/src/schema/assets.rs`, `crates/gcore/tests/schema_contract.rs`;
  version pins at `crates/gdaemon/tests/cli_contract.rs:50`,
  `crates/gcore/src/grant/tests.rs:1173`,
  `src/gobby/storage/schema_expected_identity.json`.

Production files at or above 850 lines that this plan targets:
`src/gobby/runner_init/services.py` (850 — 3.2 moves the MCP stack init into
`src/gobby/runner_init/mcp_stack.py`), `src/gobby/github_triage/service.py`
(963 — 4.2 moves the GitHub MCP call loop into
`src/gobby/github_triage/mcp_call.py`), and
`src/gobby/servers/routes/source_control.py` (928 — 4.3 moves the GitHub MCP
helpers into `src/gobby/servers/routes/source_control_github.py`). Below the
threshold but watched:
`sync/linear_task_ops.py` 814, `tool_execution.py` 795, `execution.py` 801,
`cli/mcp_proxy.py` 714, `importer.py` 736; `workflows/dry_run.py` (858) is a
consumer whose contract stays unchanged and is not targeted. Every
deliverable keeps its files under the 1,000-line ceiling; 4.1 and 4.3 name
the split target they use if a file approaches it.

Non-goals: TCGplayer OAuth client-credentials refresh (a secret-rotation
script in game-goblins; rotation is `gobby secrets set` + `gobby mcp-proxy
refresh --server <name>`); sharing one process across identical instances;
project-keyed tool embeddings beyond carrying `server_id` in the payload.

## P1: Compatibility Proof
`kind: framing`

**Goal**: Prove gobby's `mcp` 2.x client negotiates with the FastMCP 3 / `mcp`
1.x OpenAPI server over stdio before any template work depends on it.

### 1.1 Add the AWS OpenAPI MCP negotiation smoke test [category: test]
`kind: deliverable`

Targets:
- `tests/mcp_proxy/integration/__init__.py`
- `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py`
- `tests/fixtures/openapi/petstore.json`

Create `tests/fixtures/openapi/petstore.json`: an OpenAPI 3.0.3 document with
`info.title: "petstore"`, one server `{"url": "http://127.0.0.1:{port}"}`
(the test rewrites the port), and two operations: `GET /pets` (`operationId:
listPets`, tag `pets`, returns `{"pets": [...]}`) and `GET /pets/{petId}`
(`operationId: getPet`). No external `$ref`s (upstream now rejects them).

Create `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py`
(markers `integration`, `slow`; `pytest.skip` when `shutil.which("uvx")` is
None or `GOBBY_OPENAPI_SMOKE=1` is unset, so CI and default runs never spawn
`uvx`):

1. Start a `http.server.ThreadingHTTPServer` on `127.0.0.1:0` in a thread that
   serves `/openapi.json` (fixture with the live port substituted) and
   `/pets` → `{"pets": [{"id": 1, "name": "rex"}]}`.
2. Build `MCPServerConfig(name="petstore", project_id=<any uuid>,
   transport="stdio", command="uvx",
   args=["awslabs.openapi-mcp-server@1.1.5", "--log-level", "ERROR"],
   env={"API_NAME": "petstore", "API_BASE_URL": base, "API_SPEC_URL": base +
   "/openapi.json", "ALLOW_INSECURE_HTTP": "true",
   "ALLOW_PRIVATE_NETWORKS": "true"}, connect_timeout=120.0)`
   (`src/gobby/mcp_proxy/models.py::MCPServerConfig`).
3. Open `StdioTransportConnection(config)` from
   `src/gobby/mcp_proxy/transports/stdio.py`, `await connect()` under
   `asyncio.wait_for(..., 180)`, assert `is_connected`.
4. `tools = await list_tools_from_session(connection.session)`
   (`src/gobby/mcp_proxy/client_manager/tool_inventory.py`); assert a tool named
   `listPets` with a dict `inputSchema`.
5. `await connection.session.call_tool("listPets", {})`; assert the result
   text contains `"rex"`.
6. Cleanup runs in a `finally` that every exit reaches — success, a failed
   assertion in steps 3–5, an `asyncio.wait_for` timeout, or cancellation:
   `await asyncio.wait_for(connection.disconnect(), 10)`, then
   `httpd.shutdown()`, `httpd.server_close()`, and `thread.join(5)`. The
   step-1 server lives in a `yield` fixture whose teardown owns those three
   calls, so no test body can skip them. After cleanup assert the child
   process is gone (no lingering `uvx` pid — read `connection` transport
   params' process if exposed, else assert `disconnect()` returned within
   10 s and a second `is_connected` check is False) and `thread.is_alive()`
   is False. The orphan test (1.1.3) forces a failure after `connect()`
   succeeds (raise inside the body) and asserts the same postconditions from
   that path.

Record in the test docstring the negotiation path observed (`server/discover`
probe → legacy `initialize` fallback) so the adversary and future multiplexer
work can cite it. Rung 2: reuses the real transport and tool-inventory
helpers; no fake server.

**Acceptance:**

- 1.1.1 - A skip-by-default integration test launches the pinned OpenAPI server over stdio and completes initialize → list_tools → call_tool through gobby's real `StdioTransportConnection`. test: `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::test_openapi_server_negotiates_and_serves_tools`.
- 1.1.2 - The petstore fixture is self-contained OpenAPI 3.0.x with no external references. file: `tests/fixtures/openapi/petstore.json`.
- 1.1.3 - Cleanup terminates the `uvx` child and joins the fixture HTTP thread on the success path and on a forced failure after connect; the test asserts no orphaned process or thread from both paths. test: `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::test_openapi_server_disconnect_kills_child`.

## P2: Schema and Storage Foundation
`kind: framing`

**Goal**: Land the single schema hop and the storage APIs every later phase
builds on: the template registry, instance provenance, and project-scoped
secrets.

### 2.1 Add the migration hop for template registry, instance provenance, and project-scoped secrets [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/411_mcp_templates_project_secrets.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: append the new `EmbeddedMigration` entry to the `MIGRATIONS` registry
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for the new table, columns, indexes, and constraints
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: schema identity pins carried by the grant bundle
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: `expected_schema_identity_tracks_catalog_head` pins `latest_version`
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: latest-asset version, filename, checksum, and root-hash assertions
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: `latest_version`/`latest_checksum` identity assertions
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: regenerated `latest_version`/`latest_checksum`/`assets_root_hash`
- `tests/runtime_grants/test_golden_vectors.py::*` — scope-reason: golden grant vectors carry the schema identity
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: regenerated golden grant vector carrying the new schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: regenerated golden grant vector carrying the new schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: regenerated golden grant vector carrying the new schema identity
- `tests/runtime_grants/golden/payload_skew_unknown_field.json::*` — scope-reason: regenerated golden grant vector carrying the new schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: regenerated golden grant vector carrying the new schema identity

At implementation start compute the next hop as
`max(MIGRATIONS[-1].version, max numeric prefix in crates/gcore/assets/schema/migrations/) + 1`
and name the file `<hop>_mcp_templates_project_secrets.sql` (411 at this
writing; 408–410 are committed). Body:

```sql
-- Bundled MCP server templates, instance provenance, project-scoped secrets.
CREATE TABLE mcp_server_templates (
    id uuid NOT NULL,
    name text NOT NULL,
    project_id uuid NOT NULL,
    owner text NOT NULL DEFAULT 'user',        -- 'gobby' | 'user'
    source_path text,
    definition jsonb NOT NULL,
    definition_hash text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);
ALTER TABLE ONLY mcp_server_templates ADD CONSTRAINT mcp_server_templates_pkey PRIMARY KEY (id);
ALTER TABLE ONLY mcp_server_templates ADD CONSTRAINT mcp_server_templates_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX idx_mcp_server_templates_name_project ON mcp_server_templates (name, project_id);
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE mcp_server_templates TO gobby_daemon_runtime;

ALTER TABLE mcp_servers ADD COLUMN template_id uuid;
ALTER TABLE mcp_servers ADD COLUMN template_values jsonb;
ALTER TABLE mcp_servers ADD COLUMN runtime_hook text;
ALTER TABLE ONLY mcp_servers ADD CONSTRAINT mcp_servers_template_id_fkey
    FOREIGN KEY (template_id) REFERENCES mcp_server_templates(id) ON DELETE SET NULL;
CREATE INDEX idx_mcp_servers_template_id ON mcp_servers (template_id);

ALTER TABLE secrets ADD COLUMN project_id uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000002';
ALTER TABLE ONLY secrets ADD CONSTRAINT secrets_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY secrets DROP CONSTRAINT secrets_name_key;
CREATE UNIQUE INDEX idx_secrets_name_project ON secrets (name, project_id);
```

Mirror the grant lines `baseline.sql` gives `mcp_servers` and `secrets`
(`GRANT SELECT,INSERT,DELETE,UPDATE ... TO gobby_daemon_runtime`) for the new
table, plus any additional roles those tables grant. Register the hop in
`crates/gcore/src/schema/assets.rs` as an `EmbeddedMigration { version,
filename, checksum, sql: include_str!(...) }`; regenerate
`catalog.manifest.json` and `schema_expected_identity.json` through the
existing verify path (`gdaemon schema verify` / `cargo nextest run -p
gobby-core --features postgres` with `GOBBY_SCHEMA_TEST_DATABASE_URL`), never by
hand-editing checksums; update the `latest_version`/filename/checksum/root-hash
assertions in `schema_contract.rs`, `cli_contract.rs`, `grant/tests.rs`, the
`grant/bundle.rs` identity, and the golden grant vectors under
`tests/runtime_grants/golden/` via the repo's regeneration path. `baseline.sql`
stays untouched (hop-only contract, as 403–407 did). No DDL in Python. Rebuild
and install `gdaemon` via a new inode and apply the hop to the isolated test
hub before Python leaves run.

**Acceptance:**

- 2.1.1 - The hop creates `mcp_server_templates`, adds `mcp_servers.template_id`/`template_values`/`runtime_hook`, and scopes `secrets` by `(name, project_id)` with the global sentinel default. file: `crates/gcore/assets/schema/migrations/411_mcp_templates_project_secrets.sql`.
- 2.1.2 - The hop is registered as an `EmbeddedMigration` and every identity carrier (catalog manifest, expected identity JSON, schema/cli contract tests, grant identity and golden vectors) reflects the new head. file: `crates/gcore/src/schema/assets.rs`.
- 2.1.3 - `cargo nextest run -p gobby-core --features postgres -E 'test(schema)'` and `cargo nextest run -p gobby-daemon -E 'test(cli_contract)'` pass against the applied hop. behavior: "schema identity tracks catalog head" in `crates/gcore/tests/schema_contract.rs`.

### 2.2 Add template registry storage and instance provenance to MCP storage [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/mcp_templates.py`
- `src/gobby/storage/mcp.py::*` — scope-reason: compose `MCPTemplateStorageMixin` into `LocalMCPManager`; its constructor and every consumer stay unchanged
- `src/gobby/storage/mcp_models.py::MCPServer`
- `src/gobby/storage/mcp_models.py::MCPServer.from_row`
- `src/gobby/storage/mcp_models.py::MCPServer.to_dict`
- `src/gobby/storage/mcp_models.py::MCPServer.to_config`
- `src/gobby/mcp_proxy/models.py::*` — scope-reason: `MCPServerConfig` gains `id`/`template_id`/`template`/`runtime_hook`/`template_values` with defaults, so transports and tests constructing configs stay unchanged
- `src/gobby/storage/mcp_servers.py::*` — scope-reason: storage API rewrite — adds `insert_server`, `get_server_by_id`, `resolve_server`, and `refresh_template_instances`; deletes `_server_lookup_project_ids`, `_fetch_servers_by_name`, `_choose_canonical_server`, `_merge_tools_for_servers`, and `normalize_bundled_servers`; re-scopes `_persist_server`, `upsert`, `get_server`, `list_runtime_servers`, `update_server`, and `remove_server`
- `src/gobby/storage/mcp_tools.py::MCPToolStorageMixin.cache_tools`
- `src/gobby/storage/mcp_tools.py::MCPToolStorageMixin.get_cached_tools`
- `tests/storage/test_storage_mcp.py::*` — scope-reason: every bundled-canonicalisation test is replaced by template/provenance coverage
- `tests/storage/test_storage_mcp_templates.py`

Create `src/gobby/storage/mcp_templates.py` with a frozen dataclass
`MCPServerTemplateRow(id, name, project_id, owner, source_path, definition:
dict, definition_hash, enabled, created_at, updated_at)` (`from_row`,
`to_dict`) and `MCPTemplateStorageMixin`:

```python
class MCPTemplateStorageMixin:
    db: HubDatabase
    def upsert_template(self, *, name, project_id, owner, definition, source_path=None,
                        enabled=None) -> MCPServerTemplateRow: ...  # keyed (name, project_id);
        # definition_hash = sha256(canonical json); on first creation enabled =
        # `enabled` if passed else definition["enabled"] (default True); on drift for
        # owner='gobby' rows refresh definition/hash/source_path and keep the stored
        # enabled unless `enabled` is passed explicitly (the restore path)
    def get_template(self, name, *, project_id) -> MCPServerTemplateRow | None: ...
        # exact (name, project_id) row, else (name, GLOBAL_PROJECT_ID); never another
        # project; a disabled row is returned as-is (a disabled project template shadows
        # the global one) and callers refuse instantiation with `template_disabled`
    def get_template_by_id(self, template_id) -> MCPServerTemplateRow | None: ...
    def list_templates(self, *, project_id, enabled_only=True) -> list[MCPServerTemplateRow]: ...
        # project rows shadow global rows with the same name; sorted by name
    def delete_template(self, name, *, project_id) -> bool: ...
    def list_template_instances(self, template_id) -> list[MCPServer]: ...
```

Compose it into `LocalMCPManager` (`src/gobby/storage/mcp.py`). Use
`with self.db.transaction() as conn: conn.execute("... %s ...", (...))`.

`MCPServer` (`src/gobby/storage/mcp_models.py`) gains `template_id: str | None`,
`template_values: dict[str, Any] | None`, and `runtime_hook: str | None` (all
persisted — `runtime_hook` is a template-owned materialised field like
`transport`: `expand_template` (3.1) copies the template definition's
`runtime_hook` onto the instance config, `refresh_template_instances`
rewrites it when the template's value differs, a project override that
declares no hook materialises `None`, and because it lives on the instance
row it survives template detachment), plus the read-derived `template: str |
None` (template name): every server read (`get_server`, `get_server_by_id`,
`resolve_server`, `list_runtime_servers`, `list_all_servers`,
`list_template_instances`) `LEFT JOIN`s `mcp_server_templates` on
`template_id` and fills it from the joined row's `name`, so a restarted
daemon sees the template name and never a colliding instance name.
`template` is never written to `mcp_servers`. `from_row`, `to_dict`,
`to_config` carry all four (`to_config` sets `MCPServerConfig.id`,
`template_id`, `template`, `runtime_hook`, `template_values` directly).

`MCPServerConfig` (`src/gobby/mcp_proxy/models.py`) gains those fields here,
in the earliest leaf that constructs them: `id: str` (uuid4 string default via
`field(default_factory=...)`), `template_id: str | None = None`, `template: str
| None = None` (template name for listings), `runtime_hook: str | None = None`
(the transport hook selector, 3.1), and `template_values: dict[str, Any] |
None = None`; `validate()` requires a non-empty `id`. Every existing
constructor call keeps working through the defaults; 4.1 re-keys runtime state
by `config.id` without touching the model again.

`src/gobby/storage/mcp_servers.py`:

- `_persist_server` takes and writes `template_id`, `template_values`, and
  `runtime_hook`.
- `upsert` drops `canonical_project_id_for_server` and the trailing
  `normalize_bundled_servers([name])`; keeps name lowercasing and
  `protect_mcp_mapping` with `scope=project_id`; accepts `template_id`,
  `template_values`.
- Add `insert_server(...)` with `upsert`'s keyword set, ordered so the
  conflict outcome precedes every side effect: one transaction runs `INSERT
  ... ON CONFLICT (name, project_id) DO NOTHING RETURNING id` first with the
  secret-bearing `env` and `headers` withheld (stored as empty maps), and
  only a returned id runs `protect_mcp_mapping` with `scope=project_id` and
  the `UPDATE mcp_servers SET env = %s, headers = %s WHERE id = %s` that
  stores the protected mappings — the row is invisible to other transactions
  until commit, so no reader observes the unprotected interval. Returns the
  new `MCPServer`, or `None` when the `(name, project_id)` row already
  exists: the losing branch has written no row and no secret slot (the
  deterministic `MCPSecretSlot` names it would have written belong to the
  winner), so the existing row and its managed secrets are untouched.
  `upsert` stays the
  declarative-sync write (3.2); `insert_server` is the management-create
  write (4.1/4.2), so duplicate detection is the database's conflict
  outcome (rung 4) rather than a check-then-write.
- `get_server(name, project_id)` reads exactly `(name, project_id)`; add
  `get_server_by_id(server_id)` and `resolve_server(name, *, project_id)` =
  exact row else `(name, GLOBAL_PROJECT_ID)` row — the storage half of scope
  resolution. Shadowing ignores `enabled` (Constraints): a disabled project
  row is returned as the resolved instance and never falls through to the
  global row.
- `list_runtime_servers(project_id)` = rows for `project_id` ∪ rows for
  `GLOBAL_PROJECT_ID`, project rows shadowing same-named global rows whether
  or not they are enabled; no bundled-name filter.
- Delete `_server_lookup_project_ids`, `_fetch_servers_by_name`,
  `_choose_canonical_server`, `_merge_tools_for_servers`, and
  `normalize_bundled_servers`. Add
  `refresh_template_instances(expand, *, server_id=None) -> dict[str, Any]`
  that, for every row with `template_id` (or the single row `server_id`
  names — the form `refresh_server` in 4.1 uses), loads the template row,
  calls the injected `expand(template_row, row)` (from 3.1) and rewrites
  `transport`, `url`, `command`, `args`, `env`, `headers`, `connect_timeout`,
  `runtime_hook` when they differ, never touching `enabled`, `description`, or
  `template_values`. Each row is its own unit: a `ValueError` from `expand`
  (a template that gained a required param or tightened `choices`) leaves
  that row's materialised config untouched, is recorded, and never stops the
  loop. Returns `{"refreshed": n, "errors": {server_id: {"name",
  "project_id", "error"}}}` where `error` is the aggregated expansion
  message, which names params only and never carries a secret value. Rows
  whose template was deleted already have `template_id` NULL (FK `ON DELETE
  SET NULL`), are never visited, and stay materialised and untouched — no
  separate orphan accounting (rung 1: nothing consumes it).
- `update_server` allowed fields add `template_id` and `template_values`;
  `args` normalisation no longer consults bundled names.
- `remove_server(name, project_id)` deletes the single `(name, project_id)`
  row only.
- `MCPToolStorageMixin.cache_tools(server_id, tools)` and
  `get_cached_tools(server_id)` key the cache by `tools.mcp_server_id` (the
  column already exists); the `(server_name, project_id)` lookups go away.

Tests: replace the bundled-canonicalisation tests in
`tests/storage/test_storage_mcp.py` (`test_upsert_bundled_server_uses_global_project_and_strips_runtime_args`,
`test_list_runtime_servers_includes_global_bundled_servers`,
`test_normalize_bundled_servers_*`) with coverage for `resolve_server`
project-first, `list_runtime_servers` shadowing (including a disabled project
row), `refresh_template_instances` preserving `enabled`/`description` and
isolating one stale instance's expansion failure from the others, and
id-keyed tool caching; add
`tests/storage/test_storage_mcp_templates.py` for the template mixin (first
creation applies the definition's `enabled`, upsert drift refresh keeps the
stored `enabled`, `get_template` never crosses projects and returns a disabled
row as-is, `delete_template` sets instance `template_id` NULL via FK).

Rung 6: minimal complete storage; no new abstraction beyond the one mixin the
registry table needs.

**Acceptance:**

- 2.2.1 - `MCPTemplateStorageMixin` persists templates keyed by `(name, project_id)`, refreshes Gobby-owned rows on hash drift while preserving `enabled`, and resolves project rows before global rows. symbol: `MCPTemplateStorageMixin.get_template`.
- 2.2.2 - `mcp_servers` rows carry `template_id`/`template_values` through model, persist, upsert, and update paths, and every bundled-name canonicalisation path is deleted. symbol: `MCPServerStorageMixin.upsert`.
- 2.2.3 - `refresh_template_instances` re-expands template-owned fields and never rewrites `enabled`, `description`, or `template_values`. test: `tests/storage/test_storage_mcp.py::test_refresh_template_instances_preserves_instance_fields`.
- 2.2.4 - Tool cache reads and writes are keyed by server id. symbol: `MCPToolStorageMixin.cache_tools`.
- 2.2.5 - `refresh_template_instances` leaves an instance whose expansion fails untouched, refreshes every other instance, and reports the failure keyed by server id with name and scope and no secret value. test: `tests/storage/test_storage_mcp.py::test_refresh_template_instances_isolates_expansion_failures`.
- 2.2.6 - Server reads join the template row so `MCPServer.template` rehydrates from `template_id` after a restart, `runtime_hook` is read from the instance row, and an instance of a project override template that declares no hook materialises `runtime_hook` as `None`. test: `tests/storage/test_storage_mcp.py::test_server_reads_rehydrate_template_name_and_runtime_hook`.
- 2.2.7 - `MCPServerConfig` carries `id`, `template_id`, `template`, `runtime_hook`, and `template_values` with defaults, and `validate()` rejects an empty id. symbol: `MCPServerConfig.validate`.
- 2.2.8 - `insert_server` on an existing `(name, project_id)` row returns `None` and writes neither a row nor a secret slot: after a winner with credential `A` and a loser with credential `B` on the same key, the managed secret still decrypts to `A`. test: `tests/storage/test_storage_mcp.py::test_insert_server_conflict_writes_no_secret_slot`.

### 2.3 Scope the secret store by project [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/secrets.py::*` — scope-reason: keyword-only `project_id` added to every public `SecretStore` method and `SecretInfo`; all existing callers keep their signatures and global behaviour
- `src/gobby/storage/mcp_secrets.py::protect_mcp_mapping`
- `src/gobby/storage/mcp_secrets.py::cleanup_replaced_mcp_secrets`
- `src/gobby/cli/installers/remote_preflight.py::_read_remote_config`
- `src/gobby/mcp_proxy/client_manager/secrets.py::resolve_secrets_in_config`
- `src/gobby/mcp_proxy/importer.py::MCPServerImporter._find_missing_secrets`
- `src/gobby/cli/secrets.py::set_secret`
- `src/gobby/cli/secrets.py::list_secrets`
- `src/gobby/cli/secrets.py::delete_secret`
- `src/gobby/cli/secrets.py::get_secret`
- `tests/storage/test_secrets.py::*` — scope-reason: scope-aware set/get/list coverage
- `tests/storage/test_secrets_store.py::*` — scope-reason: project-first resolution coverage
- `tests/storage/test_secret_set_atomic.py::*` — scope-reason: atomic set now keys on `(name, project_id)`
- `tests/cli/test_cli_secrets.py::*` — scope-reason: `--global`/scope printing coverage
- `tests/storage/test_revisioned_config_store.py::*` — scope-reason: managed-secret cleanup now scoped
- `tests/mcp_proxy/test_manager_coverage.py::*` — scope-reason: `resolve_secrets_in_config` passes the config's project scope
- `tests/cli/test_install_coverage.py::*` — scope-reason: remote preflight reads the bootstrap secret in the global scope only

`SecretStore` (`src/gobby/storage/secrets.py`) gains keyword-only
`project_id: str | None = None` on `set`, `get`, `exists`, `delete`, `list`,
`resolve`, `resolve_dict`, and `find_persisted_secret_references`:

- Write scope: `set(..., project_id=None)` writes `project_id or
  GLOBAL_PROJECT_ID`; `delete` deletes exactly that scope.
- Read scope: `get/exists/resolve` with a real `project_id` read
  `(name, project_id)` then `(name, GLOBAL_PROJECT_ID)`; with `None` read
  global only. `list(project_id=None)` returns global rows; with a project
  returns that project's rows plus global rows not shadowed by name.
- `SecretInfo` gains `project_id` and `scope` (`"global"` | `"project"`) in
  `to_dict`.
- SQL keys move from `name` to `(name, project_id)`; the encryption envelope
  is untouched (`docs/contracts/secrets.md` envelope model).

`protect_mcp_mapping(..., scope=project_id, ...)` and
`cleanup_replaced_mcp_secrets` pass `project_id=scope` to `set`/`delete`, so
auto-protected instance secrets are scoped with their instance. Every direct
SQL read of `secrets` outside `SecretStore` selects an explicit scope once
uniqueness is `(name, project_id)`: `cleanup_replaced_mcp_secrets`' ownership
read becomes `SELECT description FROM secrets WHERE name = %s AND project_id =
%s` with the same scope, so a same-named managed secret in another project is
never inspected or deleted; `_read_remote_config`
(`src/gobby/cli/installers/remote_preflight.py`) reads the FalkorDB bootstrap
secret as `WHERE name = %s AND project_id = %s` with `GLOBAL_PROJECT_ID` — the
shared datastore password is machine-global by definition and a project row of
the same name must never satisfy preflight. `tests/storage/test_revisioned_config_store.py`
and `tests/cli/test_install_coverage.py` each seed a same-named secret in a
second project and assert cleanup leaves it untouched and preflight never
consumes it.
`resolve_secrets_in_config` (`src/gobby/mcp_proxy/client_manager/secrets.py`)
passes `project_id=config.project_id` to every `get` and fails closed: any
`$secret:` reference still unresolved after the project-first lookup raises
`MCPError("Server '<name>' needs configuration: missing secret(s) <names>")`
naming secret names only — never a value and never the surrounding env,
header, or arg. This applies to every connection path (startup load, lazy
connect, `refresh_server`), so an instance whose secret was deleted after
creation can never start with its credential silently removed. The
`strip_unresolved_secrets` / `strip_unresolved_secret_args` helpers are
deleted (rung 1: their only consumer was the bundled context7 optional key,
which 3.1 expresses as an optional template param instead);
`tests/mcp_proxy/test_manager_coverage.py::test_resolve_secrets_in_config_strips_unresolved_args`
becomes `test_resolve_secrets_in_config_fails_closed_naming_secret_names`.
`_find_missing_secrets` in the importer checks existence with the importing
project's scope.

CLI (`src/gobby/cli/secrets.py`): `gobby secrets set NAME` resolves scope as
`--global` → global; `--project ID|#N` → that project; otherwise the current
registered project via `registered_project_id(db, Path.cwd())`
(`src/gobby/cli/installers/shared.py`), else global. Print
`Stored secret 'NAME' (scope: project game-goblins)` /
`(scope: global)`. `list` shows a `scope` column and accepts `--project`;
`get`/`delete` accept the same flags with the same default.

Rung 4: uniqueness is a DB constraint (`idx_secrets_name_project`), not an
application check.

**Acceptance:**

- 2.3.1 - Secrets are stored and looked up by `(name, project_id)`; project-scoped reads fall back to the global row and existing callers with no scope keep global behaviour. symbol: `SecretStore.get`.
- 2.3.2 - Managed MCP secrets created by `protect_mcp_mapping` are written in the owning instance's scope and cleaned up in that scope. symbol: `protect_mcp_mapping`.
- 2.3.3 - `resolve_secrets_in_config` resolves `$secret:` references with the server config's project scope. test: `tests/mcp_proxy/test_manager_coverage.py::test_resolve_secrets_uses_config_project_scope`.
- 2.3.4 - `gobby secrets set/get/list/delete` accept `--global`/`--project`, default to the current registered project, and print the scope. test: `tests/cli/test_cli_secrets.py::test_set_secret_defaults_to_current_project_scope`.
- 2.3.5 - An unresolved `$secret:` reference raises an `MCPError` that names secret names only, and no strip-and-proceed path remains. test: `tests/mcp_proxy/test_manager_coverage.py::test_resolve_secrets_in_config_fails_closed_naming_secret_names`.
- 2.3.6 - Deleting a project-scoped secret removes only that row and reveals the same-named global fallback, while deleting global never removes another project's row. test: `tests/storage/test_secrets_store.py::test_delete_is_exact_scope_and_reveals_global_fallback`.
- 2.3.7 - Every direct SQL read of `secrets` outside `SecretStore` selects an explicit scope: managed-secret cleanup reads its instance scope and remote preflight reads the global row only, and a same-named row in another project is untouched by both. test: `tests/storage/test_revisioned_config_store.py::test_mcp_cleanup_ignores_same_named_secret_in_other_project`.

## P3: Templates and Sync
`kind: framing`

**Goal**: Ship templates as bundled YAML with a loader, expansion, sync into the
registry, and instance YAML sync — the parity surface with rules and skills.

### 3.1 Add the MCP server template model, loader, expansion, and bundled template YAMLs [category: code] (depends: 2.2, 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/templates.py`
- `src/gobby/mcp_proxy/bundled.py::*` — scope-reason: rewrite from a server registry to template-keyed runtime hooks
- `src/gobby/mcp_proxy/transports/stdio.py::StdioTransportConnection._open_transport`
- `src/gobby/install/shared/mcp/templates/openapi.yaml`
- `src/gobby/install/shared/mcp/templates/github.yaml`
- `src/gobby/install/shared/mcp/templates/linear.yaml`
- `src/gobby/install/shared/mcp/templates/brave-search.yaml`
- `src/gobby/install/shared/mcp/templates/context7.yaml`
- `src/gobby/install/shared/mcp/templates/playwright.yaml`
- `src/gobby/install/shared/mcp/templates/chrome-devtools.yaml`
- `src/gobby/install/shared/mcp/AGENTS.md`
- `src/gobby/install/shared/mcp/CLAUDE.md`
- `src/gobby/sync/integrity.py::*` — scope-reason: extend `BUNDLED_SYNC_CONTENT_TYPES`, `CONTENT_TYPE_DIRS`, `_GIT_PROTECTED_PATHS` with `mcp`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated for the new `mcp/` tree
- `tests/mcp_proxy/test_templates.py`
- `tests/mcp_proxy/transports/test_stdio_transport.py::*` — scope-reason: bundled-arg tests become template-keyed hook tests
- `tests/sync/test_integrity.py::*` — scope-reason: every synced content type must map a protected path
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: manifest covers the new `mcp/` tree
- `src/gobby/install/shared/AGENTS.md`

Template YAML schema (`src/gobby/install/shared/mcp/templates/<name>.yaml`):

```yaml
name: openapi
description: MCP tools generated from an OpenAPI 3.x specification (AWS Labs openapi-mcp-server)
version: 1
enabled: true
transport: stdio
command: uvx
args: ["awslabs.openapi-mcp-server@1.1.5", "--log-level", "ERROR"]
connect_timeout: 120
env: {}
params:
  - {name: api_name, env: API_NAME, required: true, description: Short name used in tool descriptions}
  - {name: api_base_url, env: API_BASE_URL, required: true}
  - {name: spec_url, env: API_SPEC_URL}
  - {name: spec_path, env: API_SPEC_PATH, description: Absolute path; the daemon's cwd is not the project}
  - {name: auth_type, env: AUTH_TYPE, default: none, choices: [none, bearer, api_key, basic]}
  - {name: auth_token, env: AUTH_TOKEN, secret: true}
  - {name: auth_api_key, env: AUTH_API_KEY, secret: true}
  - {name: auth_api_key_name, env: AUTH_API_KEY_NAME, default: api_key}
  - {name: auth_api_key_in, env: AUTH_API_KEY_IN, default: header, choices: [header, query, cookie]}
  - {name: auth_username, env: AUTH_USERNAME}
  - {name: auth_password, env: AUTH_PASSWORD, secret: true}
  - {name: include_tags, env: INCLUDE_TAGS}
  - {name: exclude_tags, env: EXCLUDE_TAGS}
  - {name: allow_insecure_http, env: ALLOW_INSECURE_HTTP, choices: ["true", "false"]}
  - {name: allow_private_networks, env: ALLOW_PRIVATE_NETWORKS, choices: ["true", "false"]}
require_one_of:
  - [spec_url, spec_path]
require_when:
  - {param: auth_type, equals: bearer, requires: [auth_token]}
  - {param: auth_type, equals: api_key, requires: [auth_api_key]}
  - {param: auth_type, equals: basic, requires: [auth_username, auth_password]}
```

The six conversions carry today's `DEFAULT_EXTERNAL_MCP_SERVERS` values
(`src/gobby/mcp_proxy/bundled.py:21-71`): `github` (`npx -y
@modelcontextprotocol/server-github`, param `token` → env
`GITHUB_PERSONAL_ACCESS_TOKEN`, `secret: true`, `required: true`,
`default_secret: github_personal_access_token`); `linear` (`LINEAR_API_KEY`,
`default_secret: linear_api_key`); `brave-search` (`BRAVE_API_KEY`,
`default_secret: brave_api_key`); `context7` (optional param `api_key`,
`secret: true`, `arg_flag: --api-key`, `default_secret: context7_api_key`);
`playwright` and `chrome-devtools` (no params; chrome keeps the
`chrome-devtools-mcp@0.21.0 --no-usage-statistics` args and declares
`runtime_hook: chrome_executable_path`). `default_secret` means: when the
param is omitted, the instance references `$secret:<default_secret>` and the
instantiation report lists it as missing if unset — today's install behaviour
without the install-time prompt.

`enabled` (default `true`) is the bundled-content lifecycle field from
`src/gobby/install/shared/AGENTS.md` carried into the template contract: sync
(3.2) writes it when the registry row is first created and preserves the
stored toggle on ordinary drift, exactly as `sync_rules` does. A disabled
template still shadows a same-named global template (Constraints), and every
instantiation surface — instance YAML sync (3.2), `add_mcp_server`, the CLI,
and `POST /api/mcp/servers` (4.2, 4.3) — refuses a disabled template with a
`template_disabled` result naming the template and its scope instead of
expanding it.

Create `src/gobby/mcp_proxy/templates.py`:

```python
@dataclass(frozen=True)
class TemplateParam:
    name: str; env: str | None = None; arg_flag: str | None = None
    required: bool = False; secret: bool = False; default: str | None = None
    default_secret: str | None = None; choices: tuple[str, ...] = (); description: str = ""

@dataclass(frozen=True)
class MCPServerTemplate:
    name: str; description: str; version: int; transport: str
    command: str | None; args: tuple[str, ...]; url: str | None
    env: dict[str, str]; headers: dict[str, str]; connect_timeout: float
    params: tuple[TemplateParam, ...]; require_one_of: tuple[tuple[str, ...], ...]
    require_when: tuple[RequireWhen, ...]; runtime_hook: str | None; override: bool
    enabled: bool = True
    def to_definition(self) -> dict[str, Any]: ...
    @classmethod
    def from_definition(cls, data: dict[str, Any]) -> "MCPServerTemplate": ...  # validates

def load_template_file(path: Path) -> MCPServerTemplate  # yaml.safe_load + from_definition
def get_bundled_templates_path() -> Path  # install/shared/mcp/templates

@dataclass(frozen=True)
class ExpandedInstance:
    config: MCPServerConfig; template_values: dict[str, str]
    missing_secrets: list[str]            # required secret names absent from the store
    optional_missing_secrets: list[str]   # optional secret names absent from the store

def expand_template(template: MCPServerTemplate, *, name: str, project_id: str,
                    values: Mapping[str, str], description: str | None,
                    secret_exists: Callable[[str], bool]) -> ExpandedInstance
```

`expand_template` rules: unknown value keys → `ValueError` listing known
params; `required`, `require_one_of`, `require_when`, and `choices` are
validated with one aggregated message; a `secret: true` value is a reference,
never a credential, and the grammar alone cannot tell them apart (`ghp_…`
tokens match `SECRET_NAME_GRAMMAR`), so the rule is: an explicit
`$secret:<name>` whose name matches `SECRET_NAME_GRAMMAR`
(`src/gobby/storage/secret_names.py`) is always accepted as a forward
reference; a bare string is accepted as a secret name only when it matches
the grammar **and** `secret_exists(value)` is True in the instance's scope;
every other string — a grammar mismatch, or a grammar-shaped string naming no
stored secret — is rejected with a message that names the parameter and says
to write `$secret:<name>` for a secret that will be set later, without
echoing the value (trust-boundary validation, never simplified); omitted secret params with
`default_secret` reference it; `missing_secrets` lists the **required**
referenced secret names for which `secret_exists(name)` is False and
`optional_missing_secrets` the optional ones — only `missing_secrets` sets
`needs_configuration` and suppresses connection (4.1). Env params materialise into
`config.env`; `arg_flag` params append `[flag, "$secret:<name>"]` (secret) or
`[flag, value]` to `args`; `template_values` holds the normalised values with
secrets as `$secret:` references. Secret materialisation follows the param's
requiredness: a **required** secret param (`required: true`, or required by
`require_when`) whose secret is absent is still materialised as its
`$secret:` reference, so the instance fails closed at connect time (2.3) and
`needs_configuration` is true; an **optional** secret param (context7's
`api_key`) whose secret is absent is omitted from `env`/`args` entirely, kept
as the reference in `template_values`, and listed in
`optional_missing_secrets` — the instance starts without it and materialises
it on the next re-expansion (`refresh_template_instances` at daemon start or
`refresh_server`, 4.1). This is today's context7 behaviour expressed by the
template's `required` flag instead of arg-shape heuristics. `config.name` is
lowercased and validated by `MCPServerConfig.validate()`, and
`config.runtime_hook` is the template's declared `runtime_hook` (`None` when
the template declares none) — the materialised field 2.2 persists on the
instance row.

`src/gobby/mcp_proxy/bundled.py` shrinks to runtime hooks keyed by hook
name: `resolve_runtime_stdio_args(runtime_hook, args)` (chrome executable
path injection when `runtime_hook == "chrome_executable_path"`),
`resolve_chrome_devtools_executable_path()`, and
`prefers_offline_npx(command)`; delete `DEFAULT_EXTERNAL_MCP_SERVERS`,
`BUNDLED_EXTERNAL_MCP_SERVER_NAMES`, `is_bundled_external_mcp_server`,
`canonical_project_id_for_server`, `normalize_persisted_args`,
`normalize_bundled_managed_args`, `normalize_bundled_server_config`,
`_pin_chrome_devtools_package`. `StdioTransportConnection._open_transport`
reads `self.config.runtime_hook` (added to `MCPServerConfig` in 2.2) for the
hook — never
the template or instance name, so a project override named `chrome-devtools`
whose definition declares no hook gets no injection — and sets
`npm_config_prefer_offline` for any `npx` command.
`tests/mcp_proxy/transports/test_stdio_transport.py` pins the chrome hook
firing on `runtime_hook`, a same-named instance without a hook receiving no
injection, and every `npx` command preferring offline.

Add `src/gobby/install/shared/mcp/AGENTS.md` (plus the sibling `CLAUDE.md`
shim that imports it, matching the other nested instruction files) documenting the template schema, `override: true`, the instance YAML format
(3.2), and the secret-reference rule (`$secret:<name>` always works; a bare
name only for a secret that already exists). Extend `src/gobby/sync/integrity.py`
(`BUNDLED_SYNC_CONTENT_TYPES` += `"mcp_templates"`, `CONTENT_TYPE_DIRS["mcp"]
= "mcp_templates"`, `_GIT_PROTECTED_PATHS` += `"mcp"`) and regenerate
`src/gobby/install/bundled_content_manifest.json` with
`write_bundled_content_manifest` (`src/gobby/install/manifest.py`).

Rung 2/6: YAML loading via the installed `pyyaml`; one dataclass pair; no
plugin registry for hooks beyond a two-entry dict.

**Acceptance:**

- 3.1.1 - Seven bundled templates load and validate, and `openapi` declares the pinned command, `--log-level ERROR`, `connect_timeout: 120`, and the parameter contract in Constraints. file: `src/gobby/install/shared/mcp/templates/openapi.yaml`.
- 3.1.2 - `expand_template` materialises env/args, rejects unknown params, raw secret values, missing required/conditional params, and reports missing secrets by name. symbol: `expand_template`.
- 3.1.3 - `bundled.py` exposes only template-keyed runtime hooks; every bundled-name constant and canonicalisation helper is deleted. file: `src/gobby/mcp_proxy/bundled.py`.
- 3.1.4 - The `mcp/` tree is a protected, manifest-covered bundled content type. test: `tests/sync/test_integrity.py::test_every_synced_content_type_maps_a_protected_path`.
- 3.1.5 - Secret parameters normalize to $secret references in template_values and no supplied credential value is retained. test: `tests/mcp_proxy/test_templates.py::test_expand_template_normalizes_secret_references`.
- 3.1.6 - Stdio runtime hooks dispatch on `config.runtime_hook`: a same-named instance whose template declares no hook receives no injection, and every `npx` command prefers offline. test: `tests/mcp_proxy/transports/test_stdio_transport.py::test_runtime_hook_dispatches_on_config_not_name`.
- 3.1.7 - `expand_template` materialises a required secret param as its `$secret:` reference when the secret is absent and omits an absent optional secret param from `env`/`args` while listing it in `optional_missing_secrets`. test: `tests/mcp_proxy/test_templates.py::test_expand_template_required_and_optional_missing_secrets`.
- 3.1.8 - All six converted legacy templates preserve their exact command, arguments, secret mappings, optional behavior, and runtime hook contracts. test: `tests/mcp_proxy/test_templates.py::test_bundled_template_definitions_match_legacy_contracts`.
- 3.1.9 - Template YAML `enabled` loads into `MCPServerTemplate.enabled`, defaults to true, and round-trips through `to_definition`/`from_definition`. test: `tests/mcp_proxy/test_templates.py::test_template_enabled_defaults_true_and_round_trips`.
- 3.1.10 - A credential-shaped bare value (`ghp_abc123`) naming no stored secret is rejected without being echoed, a bare name of an existing secret normalises to its `$secret:` reference, and `$secret:<name>` is accepted before the secret exists. test: `tests/mcp_proxy/test_templates.py::test_secret_params_require_reference_or_existing_name`.
- 3.1.11 - The parent shared-content contract documents `.gobby/mcp/templates/` and `.gobby/mcp/servers/` as the MCP-specific project/global override roots and states their override semantics. file: `src/gobby/install/shared/AGENTS.md`.

### 3.2 Add template and instance sync targets for bundled, global, and project YAML [category: code] (depends: 3.1, 2.3)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/sync_templates.py`
- `src/gobby/mcp_proxy/sync_servers.py`
- `src/gobby/sync_registry.py::*` — scope-reason: extend the `SYNC_TARGETS` registry with `mcp_templates`
- `src/gobby/paths.py::*` — scope-reason: add `get_global_mcp_templates_dir`, `get_global_mcp_servers_dir`, `get_project_mcp_templates_dir`, `get_project_mcp_servers_dir`
- `src/gobby/cli/installers/shared.py::_sync_user_templates_to_db`
- `src/gobby/runner_init/services.py::_init_stateful_dependencies`
- `src/gobby/runner_init/services.py::_init_mcp_stack`
- `src/gobby/runner_init/mcp_stack.py`
- `src/gobby/workflows/pipeline_loader.py::detect_override_conflict`
- `tests/mcp_proxy/test_sync_templates.py`
- `tests/mcp_proxy/test_sync_servers.py`
- `tests/cli/installers/test_bundled_sync_logging.py::*` — scope-reason: fan-out now includes `mcp_templates`
- `tests/workflows/test_user_template_sync.py::*` — scope-reason: user-template sync gains the MCP template/server pairs
- `tests/runner_init/test_services_mcp_stack.py`

`src/gobby/mcp_proxy/sync_templates.py::sync_bundled_mcp_templates(db,
templates_path: Path | list[Path] | None = None, tag: str = "gobby", *,
project_id: str | None = None, project_root: Path | None = None) ->
dict[str, Any]` mirrors `sync_bundled_rules`
(`src/gobby/workflows/sync_rules.py`): iterate `*.yaml` under each root
(skipping `deprecated/`), `load_template_file`, and
`upsert_template(name, project_id=<owner scope>, owner="gobby"|"user",
definition, source_path)` — `enabled` is never passed, so first creation takes
the definition's `enabled` and drift keeps the stored toggle (3.1). Owner scope: bundled root and the global user root →
`GLOBAL_PROJECT_ID`; the `project_root` → `project_id`. A user template whose
name matches a bundled template and lacks `override: true` raises through
`detect_override_conflict` (generalise its `is_bundled_template` check to
accept the template row's `owner == "gobby"`). Gobby-owned rows refresh on
`definition_hash` drift preserving `enabled`; a user-named collision in the
same scope is skipped with a warning. Return `{"synced", "updated",
"skipped", "orphaned", "errors", "adoption_skipped"}`. After syncing, run the adoption
step once per call: for each `mcp_servers` row with `template_id IS NULL`
whose `name` equals a Gobby-owned template name, derive candidate
`template_values` by inverting env and `arg_flag` params (`env[param.env]` →
value, preserving `$secret:` refs), run `expand_template` on them, and compare
every template-owned runtime field (`transport`, `url`, `command`, `args`,
`env`, `headers`, `connect_timeout`) of the expansion with the persisted row.
Only an exact match adopts: one `UPDATE` writes `template_id` and
`template_values` together. Any difference — an extra env key, an extra arg,
a custom header or timeout — leaves the row untouched and records
`adoption_skipped[name] = <first differing field>`, so the startup refresh
(2.2) can never strip a customisation. This is what keeps Josh's six existing
global rows working unchanged.

Removal mirrors `sync_bundled_rules`' orphan pass. The scan records every
`(name, scope)` it saw on disk and every authoritative root it read
successfully; when the scan was authoritative (the bundled roots, or a user
scan covering both the global and the project root), every root it covers
exists and was read without error, and it recorded no `errors`, it deletes
each `mcp_server_templates` row whose `owner` is the scan's owner (`gobby`
for the bundled scan, `user` for the user scan), whose `project_id` is one of
the scanned scopes (`GLOBAL_PROJECT_ID`, plus `project_id` when a project
root was scanned), and whose `(name, project_id)` is not in the on-disk set,
counting them in `orphaned`. The guard is the set of successfully scanned
roots, never a file count: an existing root that has become empty is a valid
state and prunes its scope's rows, so deleting the last project template or
the last global override detaches its instances (and, for a global override,
restores the bundled definition in the same run); a root directory that does
not exist is not scanned and its scope's rows are never pruned; an unreadable
root or any file error prunes nothing across the whole scan.
`delete_template` is the hard delete the table supports (no soft-delete
column); the FK sets each instance's `template_id` NULL, so the instance
keeps its materialised fields as a manual row — `runtime_hook` included, so a
detached `chrome-devtools` instance keeps its executable-path dispatch on its
next connection — and `refresh_template_instances` stops visiting it (2.2);
the prune logs every detached instance by name and scope. Override reversion
follows from shadowing: a pruned project override
row reveals the bundled global row again, and a pruned global user override
(same scope as the bundled row) is replaced in the same `gobby sync` run
because `_sync_user_templates_to_db` re-runs `sync_bundled_mcp_templates()`
once whenever its user pass pruned a global row.

`src/gobby/mcp_proxy/sync_servers.py::sync_mcp_server_files(db, roots:
list[Path], *, project_id: str | None, project_root: Path | None,
secret_store: SecretStore) -> dict[str, Any]` reads instance YAML:

```yaml
# .gobby/mcp/servers/lightspeed.yaml
name: lightspeed            # optional; defaults to the file stem
template: openapi
description: Lightspeed X-Series retail API
enabled: true
values:
  api_name: lightspeed
  api_base_url: https://example.retail.lightspeed.app/api
  spec_url: https://x-series-api.lightspeedhq.com/openapi/x-series.json
  auth_type: bearer
  auth_token: $secret:lightspeed_api_token   # a reference; committed files use the prefix so a fresh clone syncs before the secret is set
```

For each file: `instance_name` = the `name` field, else the file stem;
`template_name` = the required `template` field — the two are independent
inputs (the documented `lightspeed.yaml` names template `openapi`); scope =
`project_id` when the file is under `project_root`, else `GLOBAL_PROJECT_ID`;
`template = get_template(template_name, project_id=scope)` (project row
first, then global; missing → error entry naming `template_name`; disabled →
`template_disabled` error entry naming template and scope, no row written);
`expand_template(template, name=instance_name, project_id=scope, values=...,
secret_exists=lambda n: secret_store.exists(n, project_id=scope))`;
`upsert(instance_name, ... template_id, template_values)`. `instance_name`
never reaches `get_template` and `template_name` never reaches `upsert`;
`tests/mcp_proxy/test_sync_servers.py` pins a file whose stem, `name`, and
`template` all differ. Rows are never deleted for
removed files. Return `{"synced", "updated", "affected_ids": [row uuids
created or updated], "needs_configuration": {name: [missing secret names]},
"optional_missing": {name: [...]}, "errors"}` and log each
`needs_configuration` entry with the exact `gobby secrets set <name>
[--global]` command.

Wire-up: `SYNC_TARGETS` in `src/gobby/sync_registry.py` gains
`("mcp_templates", "gobby.mcp_proxy.sync_templates",
"sync_bundled_mcp_templates")`. `paths.py` adds the four helpers
(`~/.gobby/mcp/templates`, `~/.gobby/mcp/servers`,
`<project>/.gobby/mcp/templates`, `<project>/.gobby/mcp/servers`).
`_sync_user_templates_to_db` (`src/gobby/cli/installers/shared.py`) adds two
pairs after rules/variables: templates (project + global roots via
`sync_bundled_mcp_templates(tag="user", project_id, project_root)`) and
servers (`sync_mcp_server_files`), so `gobby install` and `gobby sync` run
from a project pick up its instance YAML. 3.2's postcondition is the persisted
row plus its id in `affected_ids`; the live-daemon reconciliation of those ids
(`POST /api/mcp/refresh {"server_id": …}` → `refresh_server`) belongs to 4.3,
which sits after both `refresh_server` (4.1) and the scoped refresh route, so
it is proven when it executes. Until 4.3 lands, `load_initial_configs` loads
the rows at the next start. `_init_stateful_dependencies` and
`_init_mcp_stack` (`src/gobby/runner_init/services.py`) replace the
`normalize_bundled_servers()` calls with
`refresh_template_instances(expand=...)` (2.2) after bundled sync, logging
`refreshed` plus one warning per `errors` entry naming the instance, its
scope, and the `gobby mcp-proxy add-server --template … --set …` /
`gobby secrets set` fix; a stale instance never blocks daemon start.

`src/gobby/runner_init/services.py` is at 850 lines: move `_init_mcp_stack`
and the MCP-storage portion of `_init_stateful_dependencies` (the
`LocalMCPManager` construction plus the new template refresh) into a new
`src/gobby/runner_init/mcp_stack.py` module exposing `init_mcp_db_manager(runner)`
and `init_mcp_stack(runner)`; `services.py` keeps two one-line delegations so
its line count falls rather than grows.

Rung 2: reuses the rules sync shape, the user-template path, and the
override-conflict helper rather than inventing a second sync mechanism.

**Acceptance:**

- 3.2.1 - Bundled, global, and project template YAML sync to `mcp_server_templates` rows with the owner/scope rules and the `override: true` collision guard. symbol: `sync_bundled_mcp_templates`.
- 3.2.2 - Instance YAML under `.gobby/mcp/servers/` and `~/.gobby/mcp/servers/` becomes project- or global-scoped `mcp_servers` rows with provenance, an `affected_ids` list of the created or updated row ids, and a `needs_configuration` report naming missing secrets. symbol: `sync_mcp_server_files`.
- 3.2.3 - The first sync adopts a pre-existing global row for a bundled name only when its expanded template config matches the row exactly, writes `template_id`/`template_values` in one update, changes no runtime field, and skips customised rows (extra env, extra args, differing secret references) with an `adoption_skipped` reason. test: `tests/mcp_proxy/test_sync_templates.py::test_sync_adopts_only_exact_legacy_bundled_rows`.
- 3.2.4 - Daemon start refreshes template instances instead of normalising bundled servers, from the new `mcp_stack` module. test: `tests/runner_init/test_services_mcp_stack.py::test_init_mcp_stack_refreshes_template_instances`.
- 3.2.5 - MCP stack initialisation lives in `src/gobby/runner_init/mcp_stack.py` and `services.py` shrinks. file: `src/gobby/runner_init/mcp_stack.py`.
- 3.2.6 - Daemon start logs each template-instance expansion failure by name and scope with its fix command and still completes MCP stack initialisation. test: `tests/runner_init/test_services_mcp_stack.py::test_init_mcp_stack_reports_stale_instance_without_failing`.
- 3.2.7 - Removing an instance YAML file leaves its persisted row enabled and unchanged until explicit removal. test: `tests/mcp_proxy/test_sync_servers.py::test_removed_instance_file_does_not_delete_row`.
- 3.2.8 - Instance YAML naming a disabled template is recorded as a `template_disabled` error naming the template and scope, and no row is written. test: `tests/mcp_proxy/test_sync_servers.py::test_disabled_template_blocks_instance_sync`.
- 3.2.9 - An instance file whose stem, `name`, and `template` all differ syncs a row named by `name` expanded from the template named by `template`. test: `tests/mcp_proxy/test_sync_servers.py::test_instance_name_and_template_name_are_independent`.
- 3.2.10 - Removing a template YAML file prunes its row on the next authoritative error-free scan and detaches its instances as manual rows, a removed global user override is replaced by the bundled definition in the same `gobby sync` run, and an erroring scan prunes nothing. test: `tests/mcp_proxy/test_sync_templates.py::test_removed_template_file_prunes_row_and_restores_bundled_definition`.
- 3.2.11 - Deleting the last template file in an existing project or global root prunes that scope's rows on the next scan, a scope whose root directory does not exist is never pruned, and an unreadable root prunes nothing across the scan. test: `tests/mcp_proxy/test_sync_templates.py::test_last_template_deletion_prunes_and_missing_root_does_not`.
- 3.2.12 - An instance detached by template pruning keeps its materialised `runtime_hook` and dispatches the same stdio hook on its next connection. test: `tests/mcp_proxy/test_sync_templates.py::test_detached_instance_keeps_runtime_hook_after_reconnect`.

## P4: UUID-Keyed Runtime
`kind: framing`

**Goal**: Key live servers by row UUID, resolve names per caller project in the
front door, and expose templates and scope on every management surface.

### 4.1 Key the client manager, connections, health, and tool cache by server id [category: code] (depends: 2.2, 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/manager.py::*` — scope-reason: every facade method takes a server id instead of a name
- `src/gobby/mcp_proxy/connection_cleanup.py::*` — scope-reason: `discard_connection` becomes identity-conditional and keyed by server id; its two callers are targeted here
- `src/gobby/mcp_proxy/client_manager/server_registry.py::*` — scope-reason: config/connection registry re-keyed by id
- `src/gobby/mcp_proxy/client_manager/connections.py::*` — scope-reason: locks, retries, connect/disconnect keyed by id
- `src/gobby/mcp_proxy/client_manager/health.py::*` — scope-reason: health map keyed by id, reports carry name and scope
- `src/gobby/mcp_proxy/client_manager/invocation.py::call_tool`
- `src/gobby/mcp_proxy/client_manager/invocation.py::read_resource`
- `src/gobby/mcp_proxy/client_manager/tool_inventory.py::*` — scope-reason: schema cache and DB tool cache keyed by id
- `src/gobby/mcp_proxy/server_list.py::*` — scope-reason: listing rows gain `scope`/`template`; the hooks dispatcher consumer reads the same keys
- `tests/mcp_proxy/test_mcp_manager.py::*` — scope-reason: manager API keyed by id
- `tests/mcp_proxy/test_server_registry.py::*` — scope-reason: add/update/remove by id
- `tests/mcp_proxy/test_manager_coverage.py::*` — scope-reason: config fixtures gain ids; bundled fixtures removed
- `tests/mcp_proxy/test_tool_inventory.py::*` — scope-reason: cache keyed by id
- `tests/mcp_proxy/test_manager_stale_sessions.py::*` — scope-reason: stale-session discard keyed by id
- `tests/mcp_proxy/test_manager_disconnect_cancellation.py::*` — scope-reason: disconnect keyed by id
- `tests/mcp_proxy/test_lazy.py::*` — scope-reason: connector registered with ids
- `tests/mcp_proxy/transports/test_stdio_transport.py::*` — scope-reason: configs carry id/template
- `src/gobby/mcp_proxy/tools/workflows/__init__.py::*` — scope-reason: `_WorkflowMCPInventory` and `workflow_mcp_inventory` carry the caller's project into the scoped manager inventory; the tool handlers that build it pass a project resolver
- `src/gobby/mcp_proxy/registries.py::*` — scope-reason: the build-time `workflow_mcp_inventory(...)` call passes the per-call project resolver
- `tests/workflows/test_dry_run_tool_gates.py::*` — scope-reason: pins the project-scoped name-keyed inventory contract against the id-keyed manager
- `tests/mcp_proxy/tools/test_agents_spawn_evaluation.py::*` — scope-reason: workflow inventory fixtures carry id-keyed manager state

`MCPServerConfig` already carries `id`, `template_id`, `template`,
`runtime_hook`, and `template_values` (2.2); 4.1 re-keys runtime state by
`config.id` and touches no model field.

`MCPClientManager` (`src/gobby/mcp_proxy/manager.py`) and the
`client_manager/` modules key `_configs`, `_connections`,
`_tool_schema_cache`, `_tool_cache_dirty`, `health`, and
`LazyServerConnector` registrations by `config.id`. Public facade methods
rename their first parameter from `server_name`/`name` to `server_id`:
`get_client`, `has_server`, `get_server_config`, `is_connected`,
`remove_server`, `update_server`, `set_server_description`,
`set_server_enabled`, `disconnect_server`, `ensure_connected`,
`get_client_session`, `call_tool`, `read_resource`, `list_tools`,
`_list_tools_for_server`, `cache_discovered_tools`, `get_tool_input_schema`,
`get_tool_info`, `_reconnect`, `remove_server_config`.
`load_initial_configs` loads `list_all_servers(enabled_only=False)` (the
daemon serves every project) and maps rows via `MCPServer.to_config()` so ids
come from the DB. `add_server(config)` persists through
`mcp_db_manager.insert_server(...)` (2.2) first and adopts the returned row id
before connecting; a `None` return (the `(name, project_id)` row already
exists) raises the duplicate `MCPError` and registers nothing. `get_available_servers(*, project_id)` keeps returning
`list[str]` and `list_tools(*, project_id)` keeps its `{name: tools}` shape,
both filtered to the caller's visible set — configs whose `project_id` is the
caller's project or `GLOBAL_PROJECT_ID`, a project row shadowing a same-named
global row — because `workflows/dry_run.py::evaluate_agent_definition` /
`_check_semantics` and `_WorkflowMCPInventory` feed both straight into
`set()` / `set.update()` / `dict.update()`. Once the manager holds every
project an unscoped name set would let a workflow validate against another
project's server and a name-keyed dict would drop one of two same-named
instances; the filter is a `project_id` predicate over `_configs`, never a
name lookup. `workflow_mcp_inventory(internal_manager, mcp_manager_resolver,
project_id_resolver)` (`mcp_proxy/tools/workflows/__init__.py`) gains a
project resolver in the same shape as the existing manager resolver, so the
build-time call in `mcp_proxy/registries.py` and the per-call constructions
in the workflow tool handlers need no project at build; the resolver returns
the effective session's project (the 4.2 caller-project rule) or
`GLOBAL_PROJECT_ID`. `_WorkflowMCPInventory` keeps its no-argument
`get_available_servers()` / `list_tools()` (so `dry_run.py` and
`MCPInventoryProtocol` stay untouched) and passes the resolved project to the
manager; `tests/workflows/test_dry_run_tool_gates.py` pins it with two
same-named instances carrying different tool schemas in two projects.
`RecommendationService` (4.2) passes its caller project the same way. Scoped id/name/scope/template
listings come only from `server_configs()` through `compact_mcp_server_list`
(`src/gobby/mcp_proxy/server_list.py`) in the front door (4.2), which emits
`name`, `scope` (`"global"` | project name), `template`, `state`,
`transport`, `enabled` and carries `id` only when the caller asks
(`include_ids=True`). Nothing in `manager.py` or `client_manager/` resolves
by name: name resolution is `find_config_ids` in
`services/server_resolution.py` (4.2), on the services side of the
UUID-only boundary that Constraints fixes and the Rust multiplexer ports.
Tool cache persistence calls `cache_tools(server_id, tools)` /
`get_cached_tools(server_id)` (2.2). Health reports and logs include
`name` and `project_id` next to the id so operators never read bare UUIDs.

Missing secrets fail closed on every connection path: `load_initial_configs`,
`ensure_connected` / `_connect_with_retries`, and `refresh_server` call
`resolve_secrets_in_config` (2.3) and catch its `MCPError`; the instance stays
registered, its health entry becomes `state: "needs_configuration"` with the
missing secret names, no transport is started, and `list_mcp_servers` /
`GET /api/mcp/servers` surface `missing_secrets` from that health entry. A
daemon restart with a deleted secret and a secret deleted after creation both
land in that state; `gobby secrets set` + `refresh` recovers. For a
template-owned config (`template_id` set) each of those paths first re-expands
the row through `refresh_template_instances(expand, server_id=...)` (2.2) and
rebuilds the config from the refreshed row, so an optional secret that
appeared or disappeared since the last materialisation is reflected before
resolution; only the required names in `missing_secrets` (3.1) ever suppress
the connection, and a deleted optional secret simply drops out of
`env`/`args` instead of failing closed.

Add `refresh_server(server_id)` to the facade — the one refresh operation
every surface reaches after scope resolution (the HTTP `refresh_mcp_tools`
route, which `gobby mcp-proxy refresh`, the stdio proxy, and the sync
reconciliation in 4.3 call). Its ordering contract: the existing per-id
connection lock (`_acquire_connection_lock`) is the single mutation lock for
that id — `refresh_server`, `reconnect`, `remove_server`, `update_server`,
and `set_server_enabled` (`client_manager/server_registry.py`) all hold it
across their whole transition, and `ensure_connected` takes it before any
connect attempt as today. Under the lock `refresh_server` (1) re-expands the
row when it is template-owned (`refresh_template_instances(expand,
server_id=...)`, 2.2, so newly set optional secrets materialise) and consumes
its envelope: an `errors[server_id]` entry means the template now rejects the
stored `template_values` (a gained required param, a tightened `choices`), so
the refresh leaves `_configs`, the caches, and any live connection exactly as
they were, sets that id's health to `state: "stale_template"` carrying the
expansion message (parameter names only), and raises `MCPError` naming the
instance, its scope, and the `gobby mcp-proxy add-server --template … --set …`
fix — a last-known-good connection keeps serving and nothing reconnects on
stale configuration; (2) re-reads the row with `get_server_by_id` (2.2) — a
row deleted concurrently is treated as unknown: the stale config and
connection are dropped and an unknown-server `MCPError` is raised; (3)
rebuilds the unresolved config through `MCPServer.to_config()` and atomically
swaps it into `_configs[server_id]`, drops that id's `_tool_schema_cache`
entry and marks its tool cache dirty — `_configs` holds `$secret:` references
only, exactly as `load_initial_configs` and `add_server` store them today; (4)
pops the old connection through the identity-conditional discard, keeps the
popped object in a local, and awaits its already-bounded disconnect inside
`asyncio.shield`, so a cancellation arriving mid-teardown lets the bounded
teardown run to completion (or to its timeout and force-close) before
`CancelledError` re-raises — the per-id lock is never released with a live,
ownerless transport; (4b) branches on `enabled`: a disabled row keeps its
swapped config registered, drops its connection and cache state, unregisters
its lazy connector, sets health `state: "disabled"`, and returns before any
secret resolution or transport creation, so 4.3's live-sync reconciliation of
an `enabled: false` instance installs the row and starts nothing; (5)
resolves a transient copy with `resolve_secrets_in_config(...,
project_id=config.project_id)` (2.3) so a rotated project-first secret is
picked up — on its `MCPError` the old connection is already gone, the id's
health becomes `needs_configuration` with the secret names, and the error
propagates, so no transport authenticated with the deleted or stale credential
stays reachable; (6) hands only that resolved copy to `create_connection`
inside the already-locked `_connect_with_retries` (the split `connect_server`
makes today: the registry keeps references, the connection object gets
values), then `cache_discovered_tools`. Startup load applies the same
step-(1) rule per instance: a stale-template instance is registered with
`stale_template` health and never blocks the others. A `server_id` the manager has never loaded (a row
synced while the daemon runs, 3.2/4.3) is installed from its DB row at step 2
instead of raising. Stale-session recovery is identity-conditional:
`discard_connection` (`src/gobby/mcp_proxy/connection_cleanup.py`) takes the
connection object the caller was using and pops `_connections[server_id]`
only when it is still that object, so the recovery in `call_tool` and
`retry_list_tools_after_failure` never discards a connection that
`refresh_server` installed after the failed call began. Calls already in
flight when the refresh starts may complete on the old connection or fail and
retry on the new one — both are allowed; the required safety property is that
every call that starts after `refresh_server` returns uses the new config and
secret, and no path revives the old config. On reconnect failure the DB row
is untouched, health reports that id as disconnected with the error, and
same-named instances in other scopes are never touched. `_reconnect` reuses
this path. No lease, drain, or generation counter beyond the connection
identity check (rung 2).

Rung 6: a key swap plus one lookup helper; no new abstraction over transports
or lazy connection state (`lazy.py` keys are opaque strings already).

**Acceptance:**

- 4.1.1 - Configs loaded at startup and rows persisted by `add_server` carry the DB row id as `MCPServerConfig.id`, and every manager map is keyed by that id. symbol: `load_initial_configs`.
- 4.1.2 - Manager configs, connections, schema cache, health, and lazy-connector state are keyed by server id, and two configs sharing a name in different projects coexist as independent connections. test: `tests/mcp_proxy/test_mcp_manager.py::test_same_name_in_two_projects_are_independent_servers`.
- 4.1.3 - No name-based lookup remains in `manager.py` or `client_manager/`; every facade method and registry helper takes a server id, and the project-scoped `get_available_servers(project_id=)` / `list_tools(project_id=)` keep their name-keyed inventory shapes for the workflow consumers. behavior: "`gcode grep -F 'find_config_ids' -g 'src/gobby/mcp_proxy/client_manager/**' -g 'src/gobby/mcp_proxy/manager.py'` returns no hits" in `src/gobby/mcp_proxy/manager.py`.
- 4.1.4 - Discovered tools persist and load by server id. symbol: `cache_discovered_tools`.
- 4.1.5 - `refresh_server(server_id)` rebuilds only the selected instance's config with current scoped secrets, invalidates its caches, and reconnects it; with two same-named instances in different projects, rotating one project's secret changes only that instance's next call. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_rotates_secret_for_selected_instance_only`.
- 4.1.6 - `refresh_server`, `reconnect`, `remove_server`, `update_server`, and `set_server_enabled` share the per-id connection lock, stale-session discard pops a connection only when it is the one the failed call used, and under a barrier-controlled concurrent `call_tool` every call that starts after `refresh_server` returns uses the new secret while an in-flight call either completes on the old connection or retries on the new one; a concurrently deleted row yields the unknown-server error. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_is_linearizable_against_concurrent_calls`.
- 4.1.7 - An instance whose required secret is missing at startup load, lazy connect, or refresh never starts a transport, reports `needs_configuration` with secret names only, and connects after the secret is set and the instance refreshed. test: `tests/mcp_proxy/test_mcp_manager.py::test_missing_required_secret_fails_closed_on_every_connection_path`.
- 4.1.8 - `get_available_servers(project_id=)` and `list_tools(project_id=)` return only the caller-visible set, so two same-named instances with different tool schemas in two projects never appear in each other's workflow inventory. test: `tests/workflows/test_dry_run_tool_gates.py::test_workflow_inventory_is_scoped_to_workflow_project`.
- 4.1.9 - Template-owned configs re-expand before startup load, lazy connect, and refresh so an optional secret's appearance or deletion materializes or removes its env/arg without entering needs_configuration. test: `tests/mcp_proxy/test_mcp_manager.py::test_optional_secret_reexpands_on_all_connection_paths`.
- 4.1.10 - `_configs` retains `$secret:` references after startup load, lazy connect, and refresh while only the connection object receives resolved values. test: `tests/mcp_proxy/test_mcp_manager.py::test_registry_config_keeps_secret_references_after_refresh`.
- 4.1.11 - A refresh whose template re-expansion fails leaves the live connection and registry state untouched, reports `stale_template` health naming parameters only, and raises; startup load registers the same instance as stale without blocking the others. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_keeps_last_known_good_on_expansion_error`.
- 4.1.12 - Refreshing an instance whose required secret was deleted disconnects and discards the old transport before reporting `needs_configuration`, and a later `call_tool` cannot reuse it. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_with_deleted_secret_disconnects_old_transport`.
- 4.1.13 - Cancelling `refresh_server` while it awaits the old connection's disconnect still completes the bounded teardown before the cancellation propagates, leaving no live transport outside the registry and the per-id lock released. test: `tests/mcp_proxy/test_manager_disconnect_cancellation.py::test_refresh_cancelled_during_old_disconnect_finishes_teardown`.
- 4.1.14 - `refresh_server` on a disabled instance, reached directly or through the live-sync refresh route, installs the config, clears connection and cache state, reports `disabled` health, and never resolves secrets or creates a transport. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_never_connects_disabled_instance`.

### 4.2 Resolve server names by project scope in the proxy front door [category: code] (depends: 4.1, 3.1, 2.3, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/services/server_resolution.py::*` — scope-reason: every resolver takes the caller's project scope and returns a config id
- `src/gobby/mcp_proxy/services/tool_execution.py::*` — scope-reason: every external-branch path threads the caller's project scope and dispatches by resolved id
- `src/gobby/mcp_proxy/services/tool_proxy.py::*` — scope-reason: `ToolProxyService` facade forwards project scope to execution, resource, and resolution helpers
- `src/gobby/mcp_proxy/services/resource_operations.py::*` — scope-reason: resource reads resolve by scope
- `src/gobby/mcp_proxy/services/recommendation.py::*` — scope-reason: recommendations limited to the caller's visible servers
- `src/gobby/mcp_proxy/services/fallback.py::*` — scope-reason: fallback suggestions search only visible servers
- `src/gobby/mcp_proxy/services/server_mgmt.py::*` — scope-reason: add/remove/import take template, values, and scope
- `src/gobby/mcp_proxy/actions.py::add_mcp_server`
- `src/gobby/mcp_proxy/actions.py::remove_mcp_server`
- `src/gobby/mcp_proxy/importer.py::MCPServerImporter.import_from_project`
- `src/gobby/mcp_proxy/importer.py::MCPServerImporter._add_server`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch._embed_all_tools_admitted`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.search_tools`
- `src/gobby/mcp_proxy/server.py::*` — scope-reason: every external-server method on `GobbyDaemonTools` passes the caller's project scope; `add_mcp_server`/`list_mcp_servers` gain template fields
- `tests/mcp_proxy/test_gobby_daemon_tools.py::*` — scope-reason: daemon tool surface gains template/scope fields
- `tests/mcp_proxy/test_mcp_server_factory.py::*` — scope-reason: factory-registered tool signatures change
- `tests/mcp_proxy/services/test_result_offload.py::*` — scope-reason: dispatch helpers take resolved ids
- `tests/config/test_config_runtime_config_resolution.py::*` — scope-reason: recommendation/import service calls carry scope
- `tests/mcp_proxy/services/test_tool_proxy_coverage.py::*` — scope-reason: resolution by project scope
- `tests/mcp_proxy/services/test_tool_proxy_validation.py::*` — scope-reason: schema lookups by resolved id
- `tests/mcp_proxy/services/test_tool_proxy_invalid_arguments_schema.py::*` — scope-reason: external schema lookups by resolved id
- `tests/mcp_proxy/services/test_recommendation_coverage.py::*` — scope-reason: recommendations scoped to visible servers
- `tests/mcp_proxy/test_server_mgmt.py::*` — scope-reason: add/remove by scope and template
- `tests/mcp_proxy/test_mcp_actions.py::*` — scope-reason: actions take template expansion
- `tests/mcp_proxy/test_mcp_proxy_actions.py::*` — scope-reason: actions take template expansion
- `tests/mcp_proxy/test_mcp_proxy_importer.py::*` — scope-reason: import resolves scope-aware missing secrets
- `tests/mcp_proxy/test_proxy_server.py::*` — scope-reason: daemon tools resolve by caller project
- `tests/mcp_proxy/test_semantic_search.py::*` — scope-reason: embeddings carry server id and scope
- `tests/mcp_proxy/services/test_scope_resolution_matrix.py`
- `src/gobby/mcp_proxy/services/_manager_compat.py::disconnect_manager_server`
- `src/gobby/github_triage/service.py::GitHubIssueTriageService._github_call`
- `src/gobby/github_triage/mcp_call.py`
- `src/gobby/integrations/github_helper.py::GitHubMCPHelper._call_github_mcp`
- `src/gobby/sync/task_github_import.py::GitHubIssueImporter._fetch_github_issues_mcp`
- `src/gobby/integrations/github.py::*` — scope-reason: the constructor gains keyword-only `project_id=None`; `_check_availability` and `get_unavailable_reason` resolve the github instance by that project and read id-keyed health; every existing caller keeps working
- `src/gobby/integrations/linear.py::*` — scope-reason: the constructor gains keyword-only `project_id=None`; `_check_availability` and `get_unavailable_reason` resolve the linear instance by that project and read id-keyed health; every existing caller keeps working
- `src/gobby/sync/linear.py::*` — scope-reason: `LinearSyncService.__init__` passes its `project_id` to `LinearIntegration`; signature and callers unchanged
- `src/gobby/mcp_proxy/tools/tasks/_delivery.py::_open_or_reuse_github_pr`
- `src/gobby/mcp_proxy/tools/tasks/_delivery.py::_find_existing_pr`
- `src/gobby/servers/websocket/handlers/core.py::HandlerMixin._handle_tool_call`
- `src/gobby/sync/github.py::*` — scope-reason: `_call_github_mcp` dispatches by resolved id and `__init__` passes `project_id` to `GitHubIntegration`; signatures and callers unchanged
- `src/gobby/sync/github_issue_sync.py::GitHubIssueSyncService._call`
- `src/gobby/sync/linear_project_ops.py::LinearProjectOpsMixin.list_teams`
- `src/gobby/sync/linear_project_ops.py::LinearProjectOpsMixin.list_projects`
- `src/gobby/sync/linear_project_ops.py::LinearProjectOpsMixin.ensure_linear_project`
- `src/gobby/sync/linear_task_ops.py::LinearTaskOpsMixin._list_issues_via_mcp`
- `src/gobby/sync/linear_task_ops.py::LinearTaskOpsMixin._sync_task_to_linear`
- `src/gobby/sync/linear_task_ops.py::LinearTaskOpsMixin.create_issue_for_task`
- `tests/github_triage/test_github_triage_service.py::*` — scope-reason: triage resolves the github instance by the issue's project scope
- `tests/integrations/test_github_helper.py::*` — scope-reason: the helper resolves the github instance by its project and dispatches by id
- `tests/cli/test_cli_import.py::*` — scope-reason: `GitHubIssueImporter` resolves the github instance by the importing project and dispatches by id
- `tests/integrations/test_github_integration.py::*` — scope-reason: availability checks resolve by scope and read id-keyed health
- `tests/external_integrations/test_github.py::*` — scope-reason: unavailable-reason fixtures carry id-keyed health
- `tests/external_integrations/test_linear.py::*` — scope-reason: unavailable-reason fixtures carry id-keyed health
- `tests/sync/test_github_sync.py::*` — scope-reason: sync calls dispatch by resolved id
- `tests/sync/test_github_issue_sync.py::*` — scope-reason: sync calls dispatch by resolved id
- `tests/sync/test_linear_sync.py::*` — scope-reason: Linear ops dispatch by resolved id
- `tests/mcp_proxy/services/test_scope_resolution_consumers.py`
- `src/gobby/runner_init/mcp_stack.py`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.store_embedding`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch._store_embedding_admitted`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.embed_tool`
- `src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch._embed_tool_admitted`
- `src/gobby/ai/embedding_switch_runner.py::EmbeddingSwitchRunner._project_tool_change`

`src/gobby/mcp_proxy/services/server_resolution.py` becomes the scope
resolver and gains `find_config_ids(manager, name, *, project_id) ->
list[str]` (over `manager.server_configs()`: exact `project_id` match first,
then `GLOBAL_PROJECT_ID`) — the single name-to-id lookup in the codebase,
kept on the services side of the UUID-only manager boundary.
`resolve_server(service, server_name=None, *, server_id=None,
project_id) -> MCPServerConfig | None` — a name resolves via
`find_config_ids` (exact `project_id` match, then `GLOBAL_PROJECT_ID`); an id
resolves only when that config's `project_id` is the caller's project or
`GLOBAL_PROJECT_ID`, so another project's UUID yields the same unknown-server
envelope as an unknown name; a disabled project row is returned as the
resolved, disabled instance and never falls through to the global row
(Constraints). `resolve_server_name` keeps alias handling and returns
the resolved config, `find_tool_server(service, tool_name, *, project_id)`
searches only servers visible to the project (project ∪ global),
`get_server_suggestion` suggests from the visible set, and
`list_servers(service, *, project_id, name_filter=None)` lists the visible
set via `compact_mcp_server_list`.

The caller's project is one total function of the call's inputs,
`resolve_request_scope(*, session_project_id, project_id, scope,
fallback_project_id, project_exists) -> str` in `server_resolution.py`,
shared with every HTTP route in 4.3; it reads no environment and no database
itself — every input that changes its result is a parameter — and evaluates
in this order: (1) explicit `scope: "global"` → `GLOBAL_PROJECT_ID`, from any
caller, because `scope` names a kind and never a project; (2) a session-bound
call → the effective session's `project_id` (`tool_execution.py` already
reads it for injection); (3) a non-empty explicit `project_id` → that
project, and `project_scope_unresolved` when `project_exists(project_id)` is
false (the adapters bind the project registry lookup); (4) explicit `scope:
"project"` with nothing above → `project_scope_unresolved`; (5) nothing at
all → `fallback_project_id`: the MCP front door passes the
`get_project_context()` id (`src/gobby/utils/project_context.py`), or
`GLOBAL_PROJECT_ID` when none, and the sessionless HTTP routes pass
`GLOBAL_PROJECT_ID` for the legacy payload (4.3), so the identical all-empty
tuple resolves through the same row on both surfaces. Rows (2)–(3) never
conflict with (1): a session-bound
`add_mcp_server(scope="global")` lands in the global scope. Thread that
value into every external-branch call in `list_tools`, `call_tool`,
`_call_tool_impl`, `_execute_tool_dispatch`, `_execute_tool`,
`get_tool_schema`, `call_tool_by_name`, `read_resource`, and
`RecommendationService.recommend_tools`; every `_mcp_manager.<method>(name)`
becomes `<method>(config.id)` after resolution, and `_unknown_server_result`
says which scope was searched. Internal `gobby-*` registries are unaffected
(`is_internal()` branch unchanged — the Rust-multiplexer seam).

Every direct manager consumer outside the front door crosses the same
boundary (`gcode grep -E "mcp_manager\.(call_tool|ensure_connected|list_tools|get_server_config|has_server|is_connected|disconnect_server|get_client|get_client_session)\(" -g "src/gobby/**"`
is the sweep — `get_client_session` included, which adds
`GitHubMCPHelper._call_github_mcp`,
`GitHubIssueImporter._fetch_github_issues_mcp`, and the source-control route's
`_call_github_mcp` (4.3); the Targets above are its non-HTTP hits, the HTTP
hits are 4.3's): each calls `resolve_server(service, name, project_id=<its
project>)` and passes `config.id` to the manager, where the project is the
one the caller already carries — the task's or issue's `project_id` for
`_delivery.py`, `github_triage`, `sync/github*.py`, and `sync/linear_*.py`;
the effective session project for `websocket/handlers/core.py`; the
integration's constructor project for `integrations/github.py` and
`integrations/linear.py` — `GitHubIntegration(mcp_manager, *,
project_id=None)` and `LinearIntegration(mcp_manager, *, project_id=None)`
gain the keyword-only parameter, `GitHubSyncService`, `LinearSyncService`,
`GitHubMCPHelper`, and `GitHubIssueImporter` pass the project they already
hold, and every other constructor call keeps the global default (their
`has_server` + `health[...]` availability checks read the resolved config's
id and the id-keyed health map); `GLOBAL_PROJECT_ID` when no project exists. `_manager_compat.py` takes an id.
`src/gobby/github_triage/service.py` is at 963 lines, so 4.2 splits it:
move the GitHub MCP call loop into the new
`src/gobby/github_triage/mcp_call.py`, which holds `github_call(manager,
server_id, tool_name, arguments, *, required, time_func, sleep_func,
max_rate_limit_delay)` (the retry loop and `_parse_mcp_result` today inlined
in `_github_call`), and `GitHubIssueTriageService._github_call` becomes a
one-line delegation that resolves the `github` instance for the issue's
project and passes its id.
`tests/mcp_proxy/services/test_scope_resolution_consumers.py` gives each
consumer a same-named project and global instance and asserts the project
instance's id reaches the manager.

Pin the resolution contract with one table-driven matrix,
`tests/mcp_proxy/services/test_scope_resolution_matrix.py`, parametrised over
five cases — enabled project row wins; no project row falls back to global;
disabled project row is returned disabled and the global instance's
credentials are never used; another project's `server_id` gets the
unknown-server envelope; removing the project row reveals the global row —
and driven through `call_tool`, `list_tools`, and `get_tool_schema`. 4.3
reuses the same case table against the HTTP execution routes.

`ServerManagementService.add_server(name, transport=None, ..., template=None,
values=None, scope="project" | "global", description=None, project_id=None)`
(`src/gobby/mcp_proxy/services/server_mgmt.py`): with `template`, look up
`get_template(template, project_id=scope project)`, `expand_template` with a
`secret_exists` bound to that scope, persist through
`actions.add_mcp_server(... config=expanded.config, template_values=...)`, and
return `{"success", "name", "id", "scope", "template", "connected",
"missing_secrets": [...], "needs_configuration": bool, "configure": ["gobby
secrets set <name> [--global]", ...]}`; when secrets are missing the row is
persisted and connection is skipped with `connected: false`; a template whose
resolved row is disabled (3.1) returns `success: false, error:
"template_disabled"` naming the template and its scope, and nothing is
persisted. Without `template`, behaviour is today's manual path with the same
envelope. Creation is one atomic database outcome (rung 4): the service
persists through `insert_server` (2.2), never through a check followed by
`upsert`; when it returns `None` the service loads the existing `(name,
scope)` row and returns `success: false, error: "duplicate"` with that row's
template and scope, registering nothing in the manager — two concurrent adds
of the same name and scope yield one winner, one duplicate envelope, one
row, one UUID, and one connection. `remove_server(name, *, scope)` targets
the exact `(name, <scope project>)` row through `get_server(name,
project_id=...)` (2.2) and returns unknown-server when that row does not
exist, even when a same-named global row does: project-first/global fallback
is a read policy (`resolve_server`, used by the execution, schema, resource,
and listing paths) and never selects a mutation target. `import_server` and `MCPServerImporter._add_server` route through the
same `add_server`. `actions.add_mcp_server` accepts a prebuilt config and skips
LLM description generation when the template supplies `description`.

`GobbyDaemonTools` (`src/gobby/mcp_proxy/server.py`) passes
`_caller_project_ref()`-derived project ids into the service calls above and
extends `add_mcp_server(..., template=None, values=None, scope="project")` and
`list_mcp_servers` (adds `templates: [{name, description, params:
[{name, required, secret, choices, default}]}]` from `list_templates`).
`SemanticToolSearch._embed_all_tools_admitted` iterates
`manager.server_configs()` and stores `server_id`, `server_name`,
`project_id` in each payload; the incremental writer chain (`embed_tool` →
`_embed_tool_admitted` → `store_embedding` → `_store_embedding_admitted`)
takes a required `server_id` and stores the same three keys, so the refresh
route (4.3) and `EmbeddingSwitchRunner._project_tool_change` (which
re-embeds `tools` rows on a provider switch and passes
`row["mcp_server_id"]`) write scoped points too; `search_tools(..., project_id)` filters hits to
the visible set. Points embedded before this change carry `server_name` only,
so that filter would drop every adopted instance's tools until a forced
refresh. The migration reuses the force-embed pipeline and makes it
idempotent: before writing a server's points, `_embed_all_tools_admitted`
deletes that server's points whose payload lacks `server_id` (matched on
`server_name` and the legacy `project_id`), so superseded legacy points never
coexist with scoped ones. `init_mcp_stack` (`src/gobby/runner_init/mcp_stack.py`,
3.2) runs `embed_all_tools` once after `load_initial_configs` when the
`config_store` key `mcp.tool_embeddings.scoped_payload_version` is absent or
below `1`, then writes `1`: the backfill happens on the first start after
the change and never again, a failure leaves the key unset and is logged so
the next start retries, and `tests/mcp_proxy/test_semantic_search.py` seeds
legacy payloads to prove the rewrite, the removal, and the no-op second run.

Rung 6: resolution is one function pair in the existing service module; no
resolver class.

**Acceptance:**

- 4.2.1 - `call_tool`, `list_tools`, `get_tool_schema`, and `read_resource` resolve an external server name within the caller's project first and the global scope second, and never reach another project's instance. test: `tests/mcp_proxy/services/test_tool_proxy_coverage.py::test_call_tool_resolves_project_instance_before_global`.
- 4.2.2 - `add_mcp_server` with `template`/`values` persists an expanded instance in the requested scope and returns `missing_secrets` plus the exact configure commands without connecting. symbol: `ServerManagementService.add_server`.
- 4.2.3 - `list_mcp_servers` reports only the caller's visible instances with `scope` and `template`, plus the available templates and their params. symbol: `GobbyDaemonTools.list_mcp_servers`.
- 4.2.4 - Tool embeddings carry the server id and scope and semantic search filters to visible servers. test: `tests/mcp_proxy/test_semantic_search.py::test_search_tools_filters_to_visible_servers`.
- 4.2.5 - The scope-resolution matrix (enabled project row, global fallback, disabled project shadow, foreign-project id, project-row removal) passes for `call_tool`, `list_tools`, and `get_tool_schema`. test: `tests/mcp_proxy/services/test_scope_resolution_matrix.py::test_scope_resolution_matrix`.
- 4.2.6 - Every non-HTTP direct manager consumer (task delivery, GitHub triage, GitHub/Linear sync, integrations, websocket tool calls) resolves its server through `resolve_server` with its own project scope and passes only the resolved id across the manager boundary; with a same-named project and global instance the project instance is used. test: `tests/mcp_proxy/services/test_scope_resolution_consumers.py::test_consumers_resolve_project_instance_by_id`.
- 4.2.7 - Adding from a disabled project or global template returns template_disabled and persists no mcp_servers row. test: `tests/mcp_proxy/test_server_mgmt.py::test_add_disabled_template_returns_template_disabled_without_persisting`.
- 4.2.8 - Two concurrent `add_server` calls for the same name and scope produce one persisted row, one manager registration, and one duplicate envelope, with the winning row and its managed secret rows unmodified even when the loser carries distinct credential values and keys. test: `tests/mcp_proxy/test_server_mgmt.py::test_concurrent_add_same_name_and_scope_has_one_winner`.
- 4.2.9 - The first daemon start after the change rewrites every legacy external tool point with `server_id`, `server_name`, and owning `project_id`, removes the superseded legacy points, records the backfill version, and a second start embeds nothing. test: `tests/mcp_proxy/test_semantic_search.py::test_scoped_payload_backfill_rewrites_legacy_points_once`.
- 4.2.10 - `resolve_request_scope` is total over its explicit inputs: the all-empty tuple returns `fallback_project_id`, an explicit project is refused when `project_exists` is false, and the MCP front door and the HTTP routes drive the same function with only their fallback differing. test: `tests/mcp_proxy/services/test_scope_resolution_matrix.py::test_resolve_request_scope_is_total_over_explicit_inputs`.

### 4.3 Expose templates and scope through HTTP routes, the CLI, and the stdio proxy [category: code] (depends: 4.2, 3.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/mcp/endpoints/server.py::_build_mcp_server_config`
- `src/gobby/servers/routes/mcp/endpoints/server.py::list_mcp_servers`
- `src/gobby/servers/routes/mcp/endpoints/server.py::add_mcp_server`
- `src/gobby/servers/routes/mcp/endpoints/server.py::update_mcp_server`
- `src/gobby/servers/routes/mcp/endpoints/server.py::remove_mcp_server`
- `src/gobby/servers/routes/mcp/endpoints/server.py::set_mcp_server_enabled`
- `src/gobby/servers/routes/mcp/endpoints/templates.py`
- `src/gobby/servers/routes/mcp/endpoints/__init__.py`
- `src/gobby/servers/routes/mcp/tools.py::*` — scope-reason: register the templates route on the existing router; app wiring consumers unchanged
- `src/gobby/servers/routes/mcp/endpoints/registry.py::*` — scope-reason: refresh/status routes resolve by project scope and read id-keyed health
- `src/gobby/servers/routes/mcp/endpoints/discovery.py::*` — scope-reason: discovery routes resolve the server by project scope and pass ids to the manager
- `src/gobby/servers/routes/admin/_health.py::*` — scope-reason: admin health reads the id-keyed health map
- `src/gobby/servers/routes/mcp/endpoints/execution.py::*` — scope-reason: every execution route resolves the server by project scope and accepts `server_id`
- `tests/servers/routes/mcp_endpoints/test_resolved_session_ownership.py::*` — scope-reason: execution routes resolve by scope
- `tests/servers/routes/mcp_endpoints/test_execution_offload.py::*` — scope-reason: execution routes resolve by scope
- `tests/servers/test_auth_service.py::*` — scope-reason: server listing response gains scope fields
- `src/gobby/mcp_proxy/stdio_tools.py::add_mcp_server`
- `src/gobby/mcp_proxy/stdio_tools.py::remove_mcp_server`
- `src/gobby/mcp_proxy/stdio_tools.py::list_mcp_servers`
- `src/gobby/mcp_proxy/stdio_proxy.py::DaemonProxy.add_mcp_server`
- `src/gobby/mcp_proxy/stdio_proxy.py::DaemonProxy.remove_mcp_server`
- `src/gobby/cli/mcp_proxy.py::list_servers`
- `src/gobby/cli/mcp_proxy.py::add_server`
- `src/gobby/cli/mcp_proxy.py::remove_server`
- `src/gobby/cli/mcp_proxy.py::refresh_tools`
- `src/gobby/cli/mcp_proxy_templates.py`
- `docs/guides/http-endpoints.md`
- `tests/servers/routes/mcp_endpoints/test_registry_routes.py::*` — scope-reason: server routes scoped by project and template-aware
- `tests/servers/routes/mcp_endpoints/test_discovery_routes.py::*` — scope-reason: discovery routes resolve by project
- `tests/servers/routes/mcp_endpoints/test_template_routes.py`
- `tests/cli/test_cli_mcp_proxy.py::*` — scope-reason: template flags and scope output
- `tests/mcp_proxy/test_stdio_proxy.py::*` — scope-reason: proxied add/list carry template fields
- `tests/mcp_proxy/test_mcp_tools.py::*` — scope-reason: tool signatures gain template/scope params
- `tests/servers/routes/mcp_endpoints/test_scope_resolution_matrix.py`
- `src/gobby/servers/routes/mcp/endpoints/server.py::import_mcp_server`
- `src/gobby/servers/routes/source_control.py::*` — scope-reason: the GitHub MCP helpers move out to `source_control_github.py`; the route handlers import them and pass the request's resolved project
- `src/gobby/servers/routes/source_control_github.py`
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: GitHub-backed routes resolve the github instance by the request's project and dispatch by id
- `src/gobby/cli/installers/shared.py::_sync_user_templates_to_db`
- `tests/workflows/test_user_template_sync.py::*` — scope-reason: the live reconciliation step after the server pass
- `src/gobby/servers/routes/mcp/endpoints/server.py::_current_project_id`
- `src/gobby/servers/routes/mcp/endpoints/server.py::_body_project_id`

HTTP (`src/gobby/servers/routes/mcp/endpoints/server.py`): every server,
execution, discovery, and registry route derives the caller's project through
the 4.2 `resolve_request_scope` case table and nothing else, passing
`fallback_project_id=GLOBAL_PROJECT_ID` and the project registry lookup as
`project_exists`:

| session | `scope` | `project_id` | result |
| --- | --- | --- | --- |
| any | `global` | any | `GLOBAL_PROJECT_ID` |
| bound | absent or `project` | any | the effective session's project (the session the stdio proxy identifies on each call) |
| none | absent or `project` | non-empty, registered | that project (the CLI, tests, a future web picker) |
| none | absent or `project` | non-empty, unregistered | `400 project_scope_unresolved` |
| none | `project` | empty or absent | `400 project_scope_unresolved` |
| none | absent | empty or absent | `GLOBAL_PROJECT_ID` — the existing web MCP-tab payload (`web/src/components/activity/mcp/McpTabActions.ts` posts `project_id: ""` and no `scope`), the documented legacy path until D2 adds project selection |

A session-bound request that says `scope: "global"` therefore lands in the
global scope, and no request that names a project scope without saying which
project ever lands anywhere. The daemon-checkout
fallback `_current_project_id()` and its caller `_body_project_id()` are
deleted from these routes, so a request
that names a project scope without saying which project never lands in the
daemon's own project.
`GET /api/mcp/servers` returns the visible set with additive fields `id`,
`scope`, `template`, `template_values` (secrets shown as `$secret:` refs via
`_public_secret_refs`), and `missing_secrets`. `POST /api/mcp/servers` accepts
`{name, template?, values?, scope?, description?, enabled?, connect_timeout?,
...manual fields}` and returns the 4.2 envelope; the existing manual body (the
web `useMcp.addServer` payload: `name, transport, command, args, url, env,
enabled`) keeps working unchanged. `PATCH`/`DELETE`/enable routes target the exact
`(name, <resolved project>)` row through `get_server` (2.2), never through the
read-side fallback: a project-scoped PATCH, DELETE, or enable for a name that
exists only globally returns `404` unknown-server, and `scope: "global"` /
`--global` is the only way to mutate the global row. `update_mcp_server` resolves the row first and builds
the updated config from it — `_build_mcp_server_config(body, name=,
project_id=, base=row)` copies `id`, `project_id`, `template_id`,
`template`, `runtime_hook`, and `template_values` from the resolved row, so
an update never mints a new UUID or drops provenance; `manager.update_server`
does the same from `_configs[server_id]`. For a template-owned instance the
template-owned runtime fields (`transport`, `url`, `command`, `args`, `env`,
`headers`, `connect_timeout`) are immutable through PATCH: a body naming any
of them returns `400 template_owned_fields` listing them, and `values` in the
body is a partial update merged over the persisted `template_values`: keys
present replace, keys absent keep their stored value (so a one-field PATCH
never drops an optional or `default_secret` reference), and a key set to
`null` removes the parameter (a removed `default_secret` param returns to
its default reference on re-expansion, a removed optional param disappears
from `env`/`args`). The merged map goes through the full `expand_template`
validation (required, `require_one_of`, `require_when`, `choices`, secret
references); a failure returns `400 template_values_invalid` with the
aggregated message and writes nothing. The route performs no
read-modify-write of its own: it resolves the name to the row's id and hands
the body (the `values` patch plus any non-template fields) to one id-keyed
manager operation, `manager.update_server(server_id, patch)`, which holds the
per-id connection lock (4.1) across the whole transition — reread the row
with `get_server_by_id`, build the updated config from it through
`_build_mcp_server_config(..., base=row)`, merge, validate, persist the
normalised `template_values` and the materialised fields in one storage
`update_server` call, and refresh the connection in place — so two
concurrent partial PATCHes serialise and both keys survive, and `DELETE`,
which holds the same lock, is a non-resurrecting winner: a PATCH queued
behind it rereads no row and returns `404` unknown-server. A startup refresh
after either outcome finds nothing to overwrite. New `src/gobby/servers/routes/mcp/endpoints/templates.py`:
`GET /api/mcp/templates` → `{templates: [{name, description, owner, scope,
params}]}` for the resolved project, registered in `endpoints/__init__.py` and
`create_mcp_router` (`src/gobby/servers/routes/mcp/tools.py`).
`refresh_mcp_tools`, `get_mcp_status`, `list_mcp_tools`, `get_tool_schema`,
`call_mcp_tool`, and `mcp_proxy` pass the resolved project into the 4.2
services and accept `server_id` as an alternative to `server_name`;
`server_id` goes through the same `resolve_server`, so a foreign project's id
is unknown. `refresh_mcp_tools` keeps its existing pipeline —
`SchemaHashManager.check_tools_for_changes` → `store_hash` /
`update_verification_time` → `cleanup_stale_hashes` → `embed_tool` for new
and changed tools, `force` treating every tool as new, and the per-server
statistics envelope — and replaces only its runtime-reload step
(`ensure_connected` + `session.list_tools()` + `cache_discovered_tools`) with
`refresh_server(config.id)` (4.1) on each resolved row, reading the refreshed
tool list from the manager afterwards. Hash rows and embeddings stay keyed by
the resolved config's `(name, project_id)`, which is unique per instance, so
`schema_hash.py` is untouched; embedding payloads carry `server_id` (4.2);
`by_server` statistics are keyed by id and carry `name` and `scope`. The CLI
`refresh --server NAME [--global]` and the stdio proxy reach it through this
route, so every surface rotates a secret the same way.

Live reconciliation of synced instance rows (moved here from 3.2 so it
follows `refresh_server` and this route): after the server pass,
`_sync_user_templates_to_db` (`src/gobby/cli/installers/shared.py`)
reconciles every `affected_ids` entry into the running manager: it loads
each row with `get_server_by_id` (2.2) and posts `POST /api/mcp/refresh
{"server_id": …, "project_id": row.project_id}` for a project row or
`{"server_id": …, "scope": "global"}` for a global row through
`call_mcp_api`, so the sessionless request carries the row's own scope
authority and never falls into the legacy global path; `refresh_server`
installs a config the manager has never loaded from its DB row before
connecting, so an instance synced while the daemon runs is callable without a
restart. When no daemon is reachable the step prints one line and is skipped;
`load_initial_configs` loads the rows at the next start.

`src/gobby/servers/routes/source_control.py` is at 928 lines, so 4.3 splits
it: move `_get_github`, `_call_github_mcp`, and `_parse_github_repo` into the
new `src/gobby/servers/routes/source_control_github.py`, where
`_call_github_mcp(server, project_id, tool_name, arguments)` resolves the
`github` instance for the request's project (`_resolve_project` already
yields it) through `resolve_server` and dispatches by id;
`source_control.py` imports them and shrinks. The 4.2 scope matrix runs again against `call_mcp_tool` and
`get_tool_schema` in
`tests/servers/routes/mcp_endpoints/test_scope_resolution_matrix.py`. Document
all of it in `docs/guides/http-endpoints.md` (the `/api/mcp` table and the
body examples).

Stdio surfaces: `stdio_tools.py` `add_mcp_server` gains `template: str | None
= None`, `values: dict[str, str] | None = None`, `scope: str = "project"`,
`description: str | None = None`; `remove_mcp_server(name, scope="project")`;
`list_mcp_servers` docstring documents `templates`. `DaemonProxy`
(`stdio_proxy.py`) forwards the new fields to `/api/mcp/servers` together
with the caller's `project_id` — the effective project it already resolves
per call (`GOBBY_PROJECT_ID` or the nearest `.gobby/project.json`, then the
effective session) — so a proxy running in game-goblins never lands in the
daemon checkout's project.

CLI (`src/gobby/cli/mcp_proxy.py`, thin HTTP client via `call_mcp_api`):
every scoped command resolves the caller's project the way `gobby secrets
set` does (2.3) — `registered_project_id(db, Path.cwd())` through
`require_cli_database()` — and sends it as `project_id`; `--global` sends
`scope: "global"`; outside a registered project without `--global` the
command exits with `not inside a registered Gobby project; pass --global for
a machine-wide instance` and sends nothing.
`add-server NAME --template T --set key=value ... [--global] [--description]`
(manual flags unchanged); when the response lists `missing_secrets` and the
terminal is interactive, prompt for each (hidden input) and store via the
secrets CLI helper in the instance's scope, then `POST /api/mcp/refresh
--server NAME`; non-interactive prints the configure commands and exits 0
with `needs_configuration`. `remove-server NAME [--global]`. `list-servers`
prints `NAME  SCOPE  TEMPLATE  STATE`. New `list-templates` (and
`show-template NAME` printing params) in `src/gobby/cli/mcp_proxy_templates.py`
registered on the `mcp_proxy` group (kept out of `cli/mcp_proxy.py`, 714
lines, to stay well under the ceiling). `refresh --server NAME [--global]`
resolves the scoped row.

Rung 6: additive fields on existing routes; one new GET; no new client
abstraction.

**Acceptance:**

- 4.3.1 - `POST /api/mcp/servers` accepts `template`/`values`/`scope`, keeps the manual payload the web MCP tab sends working, and `GET /api/mcp/servers` lists the caller's visible instances with `scope`, `template`, and `missing_secrets`. symbol: `add_mcp_server`.
- 4.3.2 - `GET /api/mcp/templates` lists templates visible to the resolved project with their parameter contracts. file: `src/gobby/servers/routes/mcp/endpoints/templates.py`.
- 4.3.3 - `gobby mcp-proxy add-server --template … --set … [--global]` instantiates, prompts for missing secrets interactively, and prints configure commands non-interactively; `list-templates`/`show-template` exist. test: `tests/cli/test_cli_mcp_proxy.py::test_add_server_from_template_prompts_for_missing_secrets`.
- 4.3.4 - The stdio proxy and `add_mcp_server`/`remove_mcp_server`/`list_mcp_servers` MCP tools carry `template`, `values`, and `scope` end to end. test: `tests/mcp_proxy/test_stdio_proxy.py::test_add_mcp_server_forwards_template_fields`.
- 4.3.5 - `docs/guides/http-endpoints.md` documents the scoped `/api/mcp/servers` contract and `/api/mcp/templates`. file: `docs/guides/http-endpoints.md`.
- 4.3.6 - HTTP execution routes resolve `server_name` and `server_id` through the shared scope resolver and pass the 4.2 scope matrix, and `POST /api/mcp/refresh` rotates only the resolved instance via `refresh_server`. test: `tests/servers/routes/mcp_endpoints/test_scope_resolution_matrix.py::test_http_scope_resolution_matrix`.
- 4.3.7 - HTTP import resolves project and global scope through the shared management path. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_import_mcp_server_respects_project_and_global_scope`.
- 4.3.8 - `PATCH /api/mcp/servers/{name}` preserves the resolved row's id, scope, and template provenance, rejects template-owned runtime-field edits on a templated instance with `400 template_owned_fields`, re-expands `values` through the template, and a following startup refresh changes nothing. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_update_mcp_server_preserves_identity_and_rejects_template_owned_fields`.
- 4.3.9 - Scope resolution is the single `resolve_request_scope` case table: explicit `scope: "global"` wins from any caller including a session-bound one, a session-bound request otherwise uses its effective project, the CLI sends the registered project of its cwd, the stdio proxy sends its effective project, an unregistered or missing project under `scope: "project"` is refused with `400 project_scope_unresolved`, the unchanged sessionless web-tab payload (`project_id: ""`, no `scope`) lands in the global scope, and a request from a second project never touches the daemon checkout's project. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_project_scope_precedence_and_web_legacy_payload`.
- 4.3.10 - Instance YAML synced while the daemon runs is reconciled into the live manager through `POST /api/mcp/refresh` carrying each affected row's id and its own `project_id` or `scope: "global"`, and becomes callable without a restart; with no daemon reachable the sync still succeeds and says the step was skipped. test: `tests/workflows/test_user_template_sync.py::test_synced_instance_is_reconciled_into_live_manager`.
- 4.3.11 - `POST /api/mcp/refresh` on a resolved instance re-hashes changed tool schemas, removes stale hashes and embeddings for tools the server no longer serves, regenerates embeddings for new and changed tools, honours `force`, and reports per-instance statistics carrying name and scope. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_refresh_preserves_schema_hash_and_embedding_pipeline`.
- 4.3.12 - The source-control GitHub routes resolve the `github` instance for the request's project and dispatch by id from `source_control_github.py`. test: `tests/servers/routes/test_source_control_routes.py::test_github_routes_resolve_project_instance`.
- 4.3.13 - HTTP, CLI, stdio, and MCP template-instantiation adapters preserve the shared template_disabled result and create no instance. behavior: "disabled-template instantiation parity across adapters" in `tests/servers/routes/mcp_endpoints/test_template_routes.py`.
- 4.3.14 - Project-scoped PATCH, DELETE, and enable requests for a name that exists only in the global scope return unknown-server and leave the global row untouched; the same requests with `scope: "global"` mutate it. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_project_scoped_mutations_never_fall_back_to_global`.
- 4.3.15 - A templated PATCH merges `values` over the stored `template_values`, keeps absent keys, removes `null` keys, rejects an invalid merge with `400 template_values_invalid` and no write, and persists the normalised map with the materialised fields atomically. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_update_mcp_server_merges_values_and_null_removes_parameter`.
- 4.3.16 - Refresh-route embedding writes carry the resolved server id, server name, and owning project id and remain visible to scoped semantic search. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_refresh_embeddings_carry_scoped_server_identity`.
- 4.3.17 - Under barrier control, two disjoint concurrent `values` PATCHes both survive, a `null`-removal PATCH racing another PATCH serialises, and a PATCH racing `DELETE` either applies before the delete or returns `404` without re-creating the row. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_concurrent_patches_and_delete_serialize_under_per_id_lock`.

## P5: Retirement and Documentation
`kind: framing`

**Goal**: Remove the legacy machine-local install path and teach agents and
operators the template model.

### 5.1 Retire the legacy bundled-install path and machine-local MCP registry [category: code] (depends: 3.2, 4.3)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/mcp_config_defaults.py::install_default_mcp_servers`
- `src/gobby/cli/installers/mcp_config.py`
- `src/gobby/cli/installers/__init__.py`
- `src/gobby/cli/install_setup.py::run_daemon_setup`
- `src/gobby/cli/install.py::*` — scope-reason: drop the `_API_KEY_PROMPTS`/`_prompt_api_keys` re-exports; the `install` command signature is unchanged
- `src/gobby/cli/_install_prompts.py::*` — scope-reason: delete the API-key prompt flow and its export; summary prints templates
- `tests/cli/test_install_coverage.py::*` — scope-reason: summary/prompt patches updated
- `src/gobby/config/mcp.py::*` — scope-reason: delete the dead `MCPConfigManager` module
- `src/gobby/storage/mcp_imports.py::MCPImportStorageMixin.import_from_mcp_json`
- `src/gobby/storage/mcp_imports.py::MCPImportStorageMixin._upsert_imported_mcp_server`
- `src/gobby/storage/mcp_imports.py::_ImportManager`
- `src/gobby/storage/mcp_imports.py::_import_manager`
- `src/gobby/install/shared/config/.mcp-template.json`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated after the template removal
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: config-contract carrier for the deleted `config/mcp.py`; regenerated and expected unchanged
- `docs/guides/configuration.md`
- `tests/cli/installers/test_cli_installers_mcp_config.py::*` — scope-reason: default-install tests deleted; facade tests kept
- `tests/config/test_config_mcp_config.py::*` — scope-reason: deleted with the module
- `tests/mcp_proxy/transports/test_sse_transport.py::*` — scope-reason: drop the `MCPConfigManager` import
- `tests/cli/test_install_prompts.py::*` — scope-reason: API-key prompt tests replaced by template listing output
- `tests/cli/test_cli_install.py::*` — scope-reason: `_API_KEY_PROMPTS` export removed
- `tests/cli/test_install_setup.py::*` — scope-reason: setup no longer installs default servers
- `tests/cli/test_install_setup_gdaemon.py::*` — scope-reason: setup no longer installs default servers

Delete `src/gobby/cli/installers/mcp_config_defaults.py`,
`src/gobby/config/mcp.py` (`MCPConfigManager`, `DEFAULT_MCP_CONFIG_PATH`), the
orphaned `src/gobby/install/shared/config/.mcp-template.json`, and
`import_from_mcp_json` plus its `_ImportManager`/`_upsert_imported_mcp_server`
helpers in `src/gobby/storage/mcp_imports.py` (keep
`import_tools_from_filesystem`). Remove the `install_default_mcp_servers`
import/export from `installers/__init__.py` and `installers/mcp_config.py`,
and its call in `run_daemon_setup` (`src/gobby/cli/install_setup.py:372`);
replace with an install-summary line listing bundled templates from
`list_templates(project_id=GLOBAL_PROJECT_ID)` and the command
`gobby mcp-proxy add-server <name> --template <template> [--global]`. Remove
`_API_KEY_PROMPTS` and `_prompt_api_keys` from `_install_prompts.py` and the
`install.py` re-exports (`_prompt_hub_api_keys` — hub credentials — stays).
`~/.gobby/mcp-servers.json` is no longer read or written; `gobby install`
logs one line when the file exists: "`~/.gobby/mcp-servers.json` is no longer
used; instances live in the hub — delete it when convenient" (no migration,
per the adoption step in 3.2). Regenerate `bundled_content_manifest.json`.
Deleting `src/gobby/config/mcp.py` triggers the config-contract carrier:
regenerate `crates/gcore/assets/config/runtime_config_contract.json` through
its existing generator and confirm the diff is empty (the module held no
runtime-config keys). Rewrite the "Downstream MCP servers" section of `docs/guides/configuration.md`
(lines ~552–586 and the table row at line 26, the troubleshooting note at
line ~721) around templates, instance YAML, scopes, and project-scoped
secrets.

Rung 1: every removed mechanism is dead at runtime or superseded by P3/P4.

**Acceptance:**

- 5.1.1 - `install_default_mcp_servers`, `MCPConfigManager`, `import_from_mcp_json`, `_API_KEY_PROMPTS`, and `.mcp-template.json` are deleted and no production import references them. behavior: "`gcode grep -F 'install_default_mcp_servers' -g 'src/**'` returns no hits" in `src/gobby/cli/install_setup.py`.
- 5.1.2 - `gobby install` instantiates no MCP servers and prints the bundled template list with the add-server command. test: `tests/cli/test_install_setup.py::test_daemon_setup_lists_mcp_templates_without_installing`.
- 5.1.3 - `docs/guides/configuration.md` describes templates, instance YAML, scopes, and project-scoped secrets and no longer references `~/.gobby/mcp-servers.json` as live config. file: `docs/guides/configuration.md`.
- 5.1.4 - When the retired mcp-servers.json file exists, install emits the exact retirement warning and neither reads nor modifies the file. test: `tests/cli/test_install_setup.py::test_daemon_setup_warns_about_retired_mcp_servers_file`.

### 5.2 Add the mcp-servers skill and rewrite the MCP tool docs [category: docs] (depends: 4.3, 5.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/mcp-servers/SKILL.md`
- `src/gobby/install/shared/skills/browser-testing/SKILL.md`
- `docs/guides/mcp-tools.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated for the new skill

Create `src/gobby/install/shared/skills/mcp-servers/SKILL.md` (frontmatter
per `context7/SKILL.md`: `name: mcp-servers`, `category: core`, `triggers:
mcp server, template, openapi, instantiate, add_mcp_server, secrets`,
`metadata.gobby.audience: all`). Sections: how templates and instances
relate; listing (`call_tool("gobby", "list_mcp_servers")` → `templates`);
instantiating with `add_mcp_server(name, template, values, scope)` and the
`needs_configuration` flow (`gobby secrets set NAME` in the right scope, then
`gobby mcp-proxy refresh --server NAME`); the declarative alternative
(`.gobby/mcp/servers/<name>.yaml`, committed; `$secret:<name>` references,
never values); the
progressive-discovery flow for instance tools (`list_tools(<instance>)` →
`get_tool_schema` → `call_tool`); rotation (`gobby secrets set` +
refresh). An `openapi` section lists the parameter contract, that the spec
must be OpenAPI 3.0/3.1 with no external `$ref`, HTTPS unless
`allow_insecure_http`, tag-only filtering, one instance per API, the ~5–20 s
first-connect delay, and that tool names are slugified `operationId`s
truncated at 56 characters. Update
`src/gobby/install/shared/skills/browser-testing/SKILL.md` to say
`playwright`/`chrome-devtools` are templates that must be instantiated
(instance names default to the template name). Rewrite the "External
(downstream) MCP servers" passage in `docs/guides/mcp-tools.md` (lines ~21–23)
and the `add_mcp_server`/`list_mcp_servers` tool descriptions for templates
and scope. Regenerate the bundled content manifest.

**Acceptance:**

- 5.2.1 - A bundled `mcp-servers` skill teaches template discovery, instantiation, secret configuration, declarative instance YAML, and the OpenAPI caveats. file: `src/gobby/install/shared/skills/mcp-servers/SKILL.md`.
- 5.2.2 - `docs/guides/mcp-tools.md` and `browser-testing/SKILL.md` describe external servers as template instances with scope. file: `docs/guides/mcp-tools.md`.
- 5.2.3 - The regenerated bundled content manifest contains the new mcp-servers skill and passes its integrity check. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.

## D1 Install-wizard template checklist
`kind: deferred`

`gobby install` offering a multi-select of bundled templates to instantiate
globally (with per-template secret prompts) belongs in the installer-wizard
redesign (#20151, idempotent reconciler); an interim click checklist would be
thrown away. Until then `gobby install` prints the template list and the
`add-server --template` command (5.1). At finalization the coordinator creates
the deferral task under this plan's epic with label
`deferred-from:bundled-mcp-templates:D1` and a `blocked-by` edge on #20151.

```yaml
deferral:
  task_ref: "#TBD"
  reason: "Install-wizard UX is being redesigned under #20151; a template checklist must land in the reconciler, not the retiring prompt flow."
  owner: "installer-wizard redesign (#20151)"
  original_acceptance_items:
    - 5.1.2
```

## D2 Web MCP tab template picker
`kind: deferred`

The web MCP tab (`web/src/components/activity/mcp/`, `web/src/hooks/useMcp.ts`)
keeps working through the unchanged manual `POST /api/mcp/servers` payload
(4.3). A template dropdown with generated value fields and missing-secret
status is frontend work under the `.impeccable.md` contract. At finalization
the coordinator creates the deferral task under this plan's epic with label
`deferred-from:bundled-mcp-templates:D2`, depending on leaf 4.3.

```yaml
deferral:
  task_ref: "#TBD"
  reason: "Frontend template picker is a separate design-contract deliverable; the backend contract it consumes ships in 4.3."
  owner: "frontend-developer"
  original_acceptance_items:
    - 4.3.2
```

## V2 End-to-End Verification
`kind: verification`

1. `GOBBY_OPENAPI_SMOKE=1 DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py -v`
   passes (P1).
2. Apply the hop to the isolated test hub; `cargo nextest run -p gobby-core --features postgres -E 'test(schema)'`
   and `cargo nextest run -p gobby-daemon -E 'test(cli_contract)'` pass; `gdaemon schema verify` is clean.
3. Focused Python suites, each run exactly as
   `DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest <paths>`
   with these bounded path sets (never the full suite):
   - `tests/storage/test_storage_mcp.py tests/storage/test_storage_mcp_templates.py tests/storage/test_secrets.py tests/storage/test_secrets_store.py tests/storage/test_secret_set_atomic.py tests/storage/test_revisioned_config_store.py`
   - `tests/mcp_proxy/test_templates.py tests/mcp_proxy/test_sync_templates.py tests/mcp_proxy/test_sync_servers.py tests/mcp_proxy/test_mcp_manager.py tests/mcp_proxy/test_manager_coverage.py tests/mcp_proxy/test_server_registry.py tests/mcp_proxy/test_tool_inventory.py tests/mcp_proxy/test_manager_stale_sessions.py tests/mcp_proxy/test_manager_disconnect_cancellation.py tests/mcp_proxy/test_lazy.py tests/mcp_proxy/transports/`
   - `tests/mcp_proxy/services/ tests/mcp_proxy/test_server_mgmt.py tests/mcp_proxy/test_mcp_actions.py tests/mcp_proxy/test_mcp_proxy_actions.py tests/mcp_proxy/test_mcp_proxy_importer.py tests/mcp_proxy/test_proxy_server.py tests/mcp_proxy/test_gobby_daemon_tools.py tests/mcp_proxy/test_mcp_server_factory.py tests/mcp_proxy/test_semantic_search.py tests/mcp_proxy/test_stdio_proxy.py tests/mcp_proxy/test_mcp_tools.py tests/mcp_proxy/tools/test_agents_spawn_evaluation.py tests/workflows/test_dry_run_tool_gates.py tests/workflows/test_user_template_sync.py`
   - `tests/servers/routes/mcp_endpoints/ tests/servers/routes/test_source_control_routes.py tests/servers/test_auth_service.py`
   - `tests/cli/test_cli_mcp_proxy.py tests/cli/test_cli_secrets.py tests/cli/test_install_setup.py tests/cli/test_install_setup_gdaemon.py tests/cli/test_install_prompts.py tests/cli/test_cli_install.py tests/cli/test_cli_import.py tests/cli/installers/ tests/sync/test_integrity.py tests/install/test_bundled_content_manifest.py tests/runner_init/test_services_mcp_stack.py tests/config/test_config_runtime_config_resolution.py`
   - `tests/github_triage/ tests/integrations/ tests/external_integrations/ tests/sync/test_github_sync.py tests/sync/test_github_issue_sync.py tests/sync/test_linear_sync.py`
   Then `uv run ruff check src/`, `uv run ruff format --check src/`,
   `uv run mypy src/`.
4. Live daemon (main checkout, announced restart): `gobby sync` reports seven
   `mcp_templates` rows; `list_mcp_servers` from a gobby session shows the six
   adopted global instances with `template` set and unchanged connectivity.
5. Project isolation from a second registered project, with verification-only
   fixtures this step creates and removes (game-goblins adoption is the
   operator follow-up named in Overview): register or pick a second project
   `<other>` (`gobby init` in a scratch checkout is enough); serve the 1.1
   petstore fixture over local HTTP as that test does; write
   `<other>/.gobby/mcp/servers/lightspeed.yaml` (`template: openapi`,
   `values`: `api_name: lightspeed`, `api_base_url` and `spec_url` pointing at
   the served fixture, `auth_type: bearer`, `auth_token: $secret:lightspeed_api_token`,
   `allow_insecure_http: "true"`, `allow_private_networks: "true"`) and a
   second file `tcgplayer.yaml` with the same shape; run `gobby sync` from
   `<other>` while the daemon runs; sync reports both rows in `affected_ids`
   reconciled into the live daemon and both in `needs_configuration`;
   `list_mcp_servers` from an `<other>` session reports both with
   `needs_configuration` and no restart has happened; `gobby secrets set
   lightspeed_api_token` (project scope) then `gobby mcp-proxy refresh --server
   lightspeed` (the CLI sends `<other>`'s project id);
   `list_tools("lightspeed")` returns `listPets` and `getPet`;
   `get_tool_schema` + `call_tool("lightspeed", "listPets")` returns `rex`. A
   gobby session calling `call_tool("lightspeed", …)` gets the unknown-server
   error naming the searched scopes. Finish by deleting the two YAML files,
   `remove_mcp_server` for both rows, and `gobby secrets delete
   lightspeed_api_token` in `<other>`'s scope.
6. Rotation: change the secret, `refresh --server`, the next call uses the new
   token (no daemon restart).

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: b747d4a3-48c9-4f24-9db5-eeb7c130ddae
- enhancer_session: 7e0b4c0f-b7d6-499b-b739-1f074081eb0b
- converged: false
- suggestions_presented: 4
- accepted:
  - E1 / better / scope-resolution acceptance matrix (disabled project shadow, foreign-project id) shared by the MCP and HTTP entry points
  - E2 / better / adoption only on an exact expanded-config match, otherwise `adoption_skipped`
  - E3 / better / one UUID-scoped `refresh_server` behind every refresh surface, with a two-same-name rotation test
  - E4 / better / per-instance isolation and an `errors` map in `refresh_template_instances`
- declined: none
- resolution_notes: All four accepted at restraint rung 2 (each composes helpers the plan already introduces). Folded into Constraints (disabled-shadow and foreign-id rule), 2.2 (`get_server_by_id`, refresh isolation and `errors` envelope, 2.2.5), 3.2 (exact-match adoption with `adoption_skipped`, startup error logging, 3.2.3 rewritten, 3.2.6), 4.1 (`refresh_server`, 4.1.5), 4.2 (`server_id` resolution rule, scope matrix, 4.2.5), and 4.3 (ids through the shared resolver, refresh wiring, HTTP matrix, 4.3.6). No Bigger suggestion survived the enhancer's rung-1 check; D1/D2 stay deferred.

**Round 2** `kind: verification`

- reviewer_run: cdad1f76-e697-4b29-aa2f-806b422d1b7a
- reviewer_session: dd769e89-9571-4937-b05e-9f101207b219
- adversary_round: 1 of 3
- verdict: needs_review
- findings:
- F1 / blocking / 3.1 must depend on 1.1 so template work inherits the P1 negotiation proof
- F2 / blocking / `find_config_ids` belongs in `services/server_resolution.py`; every direct manager caller (sync, integrations, task delivery, websocket, discovery/execution/registry routes) must cross the id boundary through scoped resolution
- F3 / blocking / `get_available_servers()` must stay `list[str]` for the dry-run and workflow inventory consumers that call `set()` on it
- F4 / blocking / the game-goblins `lightspeed`/`tcgplayer` YAML files have no deliverable owner; V2 creates them instead of verifying them
- F5 / blocking / instance YAML → `gobby sync` → `call_tool` is unreachable without a daemon restart; sync must reconcile the live manager via `refresh_server`
- F6 / blocking / template name and `runtime_hook` are not rehydratable from `template_id` after restart; hook dispatch by template name leaks the chrome hook to a project override
- F7 / blocking / `refresh_server` must hold the per-id connection lock across reread, swap, disconnect, and reconnect
- F8 / blocking / missing secrets must fail closed on startup, lazy connect, and refresh, not only on initial add
- F9 / blocking / HTTP PATCH rebuilds a fresh config, losing row id and template provenance; template-owned runtime fields need an edit policy
- F10 / blocking / `import_mcp_server` route missing from 4.3 Targets and acceptance
- F11 / blocking / no acceptance pins that removing an instance YAML file never deletes or disables its row
- F12 / blocking / no acceptance pins the `~/.gobby/mcp-servers.json` retirement warning and non-mutation
- F13 / blocking / no acceptance pins `$secret` normalisation and plaintext absence in `template_values`
- F14 / blocking / thin clients (CLI, stdio proxy) send a scope kind only; the HTTP fallback reads the daemon checkout's project, so `scope=project` from another checkout lands in the wrong project
- F15 / nit / the `orphaned` counter in `refresh_template_instances` is unobservable (FK sets `template_id` NULL before the loop sees the row)
- resolution_notes: All fifteen accepted; every vote walked the restraint ladder. Typed repairs (F1 dependency edge; F10 target and acceptance; F11, F12, F13 acceptance items) applied through `apply_plan_review_repairs`. Prose fixes hand-applied: F2 (resolver moved to 4.2, caller sweep added to 4.2 Targets and description, rung 2), F3 (`list[str]` kept; scoped listings via `server_configs()`/`compact_mcp_server_list`; workflow inventory protocols targeted, rung 2), F4 (prose-only: Overview and V2 name the game-goblins YAML as hand-authored operator verification input outside this plan's deliverables; a game-goblins task is rung 1 for this repo), F5 (one shared sync orchestration returns affected row ids and reconciles the live manager through `POST /api/mcp/refresh` → `refresh_server`, extended to install an absent config from the DB; no-restart acceptance added, rung 2), F6 (template name and `runtime_hook` joined on server reads and carried through `MCPServer`/`to_config`/`MCPServerConfig`; hooks dispatch on `config.runtime_hook`, rung 2), F7 (`refresh_server` runs under the existing per-id connection lock like `reconnect`; concurrently deleted row is unknown; barrier test added, rung 2), F8 (refined beyond the finding: required secret params fail closed with a name-only `MCPError` and `needs_configuration`; optional `default_secret` params omit the env/arg and start — today's context7 behaviour driven by the template `required` flag; the heuristic strip path is deleted outright because its only consumer was the retired bundled context7 row, rung 1/6), F9 (PATCH copies id, scope, and provenance from the resolved row; template-owned runtime fields are immutable for templated instances, values change only through `expand_template`, rung 2), F14 (CLI resolves `registered_project_id(Path.cwd())`, stdio proxy threads the effective session project, `project_id` sent explicitly, project scope rejected when unresolvable instead of falling back to daemon CWD, rung 2), F15 (`orphaned` removed from the refresh envelope and startup logging, rung 1).

```json plan-review-round
{"evidence_id":"a5b605cc-ba72-4854-b8c3-26573754533a","plan_hash":"6d64c8e7ea7973e4fc51fa175499c5b69cbad767fc622952f3fce7aa9ddd1a32","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"4b221514fb16245128823d0d338e07c00026c52ef480668de7bd0748ad968a3f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":15,"total":19},"evidence_id":"a5b605cc-ba72-4854-b8c3-26573754533a","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"62ed69bde33bdb2ad1096a6d468cea8cf51453397168d5821b94ff4e4fd5f841","status":"valid"},"source_digest":"885ef809e4f2c6bb23feefcc65ec370025be788c29ef876f0a5b806c0bc2d70b","version":1},"findings":[{"category":"bad-sequencing","check_key":"compatibility-proof-precedes-dependent-template","description":"Section 3.1 can begin without 1.1 even though P1 exists to prove the pinned OpenAPI server negotiates before template implementation depends on it.","finding_id":"F1","fix":"At restraint rung 6, add the single missing edge from 3.1 to 1.1; downstream template work then inherits the proof gate transitively.","location":"P3 / §3.1 dependency declaration","prevention":"Trace each phase goal phrased as “before anything builds on it” into explicit deliverable dependencies.","principle":"A proof phase must gate every deliverable whose correctness assumes that proof.","repairs":[{"kind":"add_dependency","on":["1.1"],"section_id":"3.1"}],"root_cause":"The OpenAPI template deliverable depends on storage only, so expansion can schedule it before the negotiation smoke test succeeds.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"uuid-boundary-consumer-completeness","description":"The confirmed design puts project/global name resolution in services and makes manager/client-manager UUID-only. Section 4.1 instead adds a name resolver under client_manager, while untargeted production callers in task delivery, websocket handling, GitHub/Linear sync and integrations, admin health, and discovery still pass literal names.","finding_id":"F2","fix":"At restraint rung 2, move find_config_ids into the existing services/server_resolution.py work in 4.2. Expand 4.2 Targets to every direct manager caller found by the sweep, thread caller project scope into that shared resolver, and pass only config.id across the manager boundary; add focused same-name cross-project coverage for those consumers.","location":"P4 / §§4.1–4.3 manager boundary and Targets","prevention":"For every renamed manager method, run a bounded usages sweep and assign each caller either scoped service resolution or an id-native contract.","principle":"A key-identity migration is complete only when resolution stays at the declared boundary and every direct consumer crosses that boundary.","root_cause":"The plan places find_config_ids inside client_manager despite the id-only decision and inventories service-layer callers without covering all direct manager consumers.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"available-servers-return-shape-compatibility","description":"The proposed mapping return makes current dry-run and workflow inventory consumers call set(...) or set.update(...) on dicts, raising TypeError. It also mixes detailed scoped listing with a compatibility-oriented name inventory.","finding_id":"F3","fix":"At restraint rung 2, keep get_available_servers() as list[str] for existing inventory consumers. Use the already-planned server_configs()/compact_mcp_server_list path inside scoped front-door services for id/name/scope/template mappings, and target the two workflow inventory protocols plus their tests.","location":"P4 / §4.1 get_available_servers contract","prevention":"Before changing a public return shape, inspect all callers for hashing, iteration, serialization, and field assumptions.","principle":"A return-shape change must migrate every consumer or preserve the established contract.","root_cause":"The plan changes a list of names into a list of mappings without accounting for inventory consumers that feed the result directly into set operations.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"cross-project-instance-deliverable-ownership","description":"The plan promises that game-goblins instantiates lightspeed and tcgplayer, yet no deliverable owns those two YAML files. V2 tells verification to create and commit them, leaving the promised outcome outside expansion.","finding_id":"F4","fix":"Create a separately governed game-goblins config task or plan for the two instance YAML files and link its stable reference and ordering here. Change V2 to verify those committed files instead of creating them; the unanswered requirement is which game-goblins task owns and gates that work.","location":"Overview, §3.2, and V2 game-goblins steps","prevention":"For each cross-repository outcome in Overview, record a stable task/plan owner in that repository before treating it as verification input.","principle":"Every shipped outcome that changes another repository needs an executable owner, not a verification-side mutation.","root_cause":"The game-goblins YAML commits appear only in end-to-end verification, which cannot generate a Task Manifest leaf or own a cross-project worktree.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"file-sync-to-live-runtime-transition","description":"The V2 path “instance YAML → gobby sync → list_tools/call_tool” is unreachable. Newly persisted rows are absent from the running manager, and the plan defines no live reconciliation before sync reports success.","finding_id":"F5","fix":"At restraint rung 2, route gobby sync and installer sync through one shared MCP template/instance orchestration. Return affected row UUIDs and reuse refresh_server for each, extending it to install an absent config from DB, before reporting success. Add acceptance proving instance YAML becomes callable without a daemon restart.","location":"P3 / §3.2 sync wiring through P4 runtime","prevention":"Trace every declarative sync from file discovery through persistence, live registry reconciliation, and a callable postcondition without restart.","principle":"A successful configuration sync must make the new state reachable in the live runtime when the acceptance flow promises immediate use.","root_cause":"gobby sync reaches bundled registry sync, user MCP file sync is wired through the installer path, and the UUID-keyed manager loads rows only at startup.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-metadata-runtime-rehydration","description":"Restarted instances cannot recover the template name needed for listings and chrome runtime behavior. Dispatching hooks by template name also makes a project override named chrome-devtools inherit the bundled hook even when its definition declares none.","finding_id":"F6","fix":"At restraint rung 2, join the referenced template on server reads and carry its name and explicit runtime_hook through MCPServer, to_config, and MCPServerConfig. Dispatch on config.runtime_hook rather than template name. Add restart, project-override-without-hook, chrome-hook, and all-npx offline-preference transport tests.","location":"P2 §2.2, P3 §3.1, and P4 §4.1","prevention":"For each persisted foreign key, enumerate every runtime field derived from the referenced row across create, restart, override, refresh, and deletion branches.","principle":"Runtime behavior derived from persisted provenance must be rehydratable after restart and must follow the resolved definition rather than a colliding name.","root_cause":"mcp_servers and MCPServer carry template_id/template_values only, while MCPServerConfig.template and runtime hook dispatch need template metadata that no read joins or lookups provide.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"refresh-server-linearizable-transition","description":"A concurrent call can reconnect with the old config between disconnect and replacement; ensure_connected can then observe that connection and let refresh return without replacing it.","finding_id":"F7","fix":"At restraint rung 2, reuse the existing server-id connection lock across DB reread, secret resolution, atomic config/cache swap, old-connection disconnect, and an already-locked reconnect helper. Treat a concurrently deleted row as unknown and add a barrier-controlled refresh-versus-call test.","location":"P4 / §4.1 refresh_server","prevention":"For every multi-step runtime refresh, mark the lock owner and prove interleavings with invocation, deletion, and reconnect.","principle":"A credential refresh must be linearizable: no call after reported success may use the prior secret.","root_cause":"The plan sequences reread, config replacement, disconnect, cache invalidation, and reconnect without holding the existing per-id connection lock across the whole transition.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-missing-secret-fail-closed","description":"An enabled template instance can start after restart or secret deletion with its required env/header/arg removed, bypassing the needs_configuration promise and potentially invoking an unauthenticated endpoint.","finding_id":"F8","fix":"At restraint rung 6, make unresolved references on template-owned configs raise a safe MCPError naming secret names only. Mark the instance disconnected/needs_configuration, never start a transport, and add restart plus post-deletion recovery tests. Preserve legacy stripping for manual configs only if a stated consumer requires it.","location":"P2 §2.3 through P4 connection and refresh paths","prevention":"Test required-secret absence at creation, daemon restart, post-creation deletion, lazy connect, and refresh.","principle":"Required authentication material must fail closed on every connection path.","root_cause":"The plan skips connection only during initial add, while the existing resolver strips unresolved references and proceeds during startup, lazy connection, and refresh.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-update-identity-and-provenance","description":"A scoped PATCH can replace the row UUID/provenance in memory or persistence. It can also mutate template-owned command/env/args only to have startup refresh silently overwrite them.","finding_id":"F9","fix":"At restraint rung 2, copy id, project_id, template_id, template metadata, and template_values from the resolved row before update. Reject direct template-owned runtime-field edits for templated instances; accept values changes only through expand_template. Add PATCH and subsequent-refresh coverage.","location":"P4 / §4.3 HTTP PATCH and manager update","prevention":"For every update surface, classify immutable identity, user-owned fields, and derived fields, then test refresh after update.","principle":"Updates addressed by stable identity must preserve identity and define ownership for derived fields.","root_cause":"_build_mcp_server_config creates a fresh default UUID and omits template provenance, while direct edits to template-owned runtime fields have no policy.","section_id":"4.3","severity":"blocking"},{"category":"traceability","check_key":"http-import-scope-target-coverage","description":"The current import route constructs MCPServerImporter from daemon project context, so it cannot honor the planned project/global scope behavior unless explicitly changed.","finding_id":"F10","fix":"At restraint rung 2, add import_mcp_server to 4.3 Targets, route it through the same scoped management/add path, and add a project/global import-route test.","location":"P4 / §4.3 Targets and management routes","prevention":"Inventory sibling HTTP endpoints whenever a shared add/remove/import contract changes.","principle":"Every existing entry point covered by a changed scope contract must appear in exact Targets and acceptance.","repairs":[{"entries":["`src/gobby/servers/routes/mcp/endpoints/server.py::import_mcp_server`"],"kind":"add_targets","section_id":"4.3"},{"items":[{"artifact":"test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_import_mcp_server_respects_project_and_global_scope`","prose":"HTTP import resolves project and global scope through the shared management path"}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"The plan says all management routes become scope-aware but omits the existing import_mcp_server endpoint from 4.3 Targets.","section_id":"4.3","severity":"blocking"},{"category":"weak-testability","check_key":"instance-file-removal-nondeletion-acceptance","description":"All current 3.2 acceptance items can pass even if removing an instance YAML file deletes or disables its database row.","finding_id":"F11","fix":"At restraint rung 6, add one focused acceptance item using the existing sync-server test module.","location":"P3 / §3.2 Acceptance","prevention":"Map every Constraints persistence/deletion rule to one acceptance artifact.","principle":"A confirmed persistence policy needs close-gate evidence for its destructive edge.","repairs":[{"items":[{"artifact":"test: `tests/mcp_proxy/test_sync_servers.py::test_removed_instance_file_does_not_delete_row`","prose":"Removing an instance YAML file leaves its persisted row enabled and unchanged until explicit removal"}],"kind":"add_acceptance","section_id":"3.2"}],"root_cause":"The non-deletion behavior is present in prose but absent from emitted validation criteria.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","check_key":"legacy-file-warning-acceptance","description":"Section 5.1 can close without emitting the promised warning when ~/.gobby/mcp-servers.json exists.","finding_id":"F12","fix":"At restraint rung 6, add one installer acceptance item that asserts the exact warning and confirms the file is neither read nor modified.","location":"P5 / §5.1 Acceptance","prevention":"Give each migration/retirement warning an acceptance test for message, read behavior, and file preservation.","principle":"A user-visible retirement warning must be asserted, including its non-mutating behavior.","repairs":[{"items":[{"artifact":"test: `tests/cli/test_install_setup.py::test_daemon_setup_warns_about_retired_mcp_servers_file`","prose":"When the retired mcp-servers.json file exists, install emits the exact retirement warning and neither reads nor modifies the file"}],"kind":"add_acceptance","section_id":"5.1"}],"root_cause":"The exact warning is specified in the body but omitted from validation criteria.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"secret-reference-normalization-acceptance","description":"The plan can satisfy current acceptance while retaining an unnormalized secret input in template_values, violating the confirmed no-plaintext provenance rule.","finding_id":"F13","fix":"At restraint rung 6, add a focused expand_template acceptance item for $secret normalization and plaintext absence.","location":"P3 / §3.1 Acceptance","prevention":"For every secret-bearing serialization boundary, assert the exact stored reference shape and absence of plaintext.","principle":"A security-sensitive persisted representation requires explicit evidence that plaintext cannot survive normalization.","repairs":[{"items":[{"artifact":"test: `tests/mcp_proxy/test_templates.py::test_expand_template_normalizes_secret_references`","prose":"Secret parameters normalize to $secret references in template_values and no supplied credential value is retained"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"Acceptance covers raw-value rejection and missing-secret reporting without pinning ExpandedInstance.template_values.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"thin-client-caller-project-identity","description":"Running gobby mcp-proxy add-server or refresh from game-goblins can target the daemon checkout's project. The instance and its correctly project-scoped secret then land in different scopes, breaking the V2 flow.","finding_id":"F14","fix":"At restraint rung 2, reuse registered_project_id(Path.cwd()) in the CLI and thread the effective session project through stdio proxy calls. Send project_id explicitly for project scope, use the global sentinel for --global, and reject project scope when no caller project can be resolved instead of falling back to daemon CWD.","location":"P4 / §4.3 CLI, stdio proxy, and HTTP scope resolution","prevention":"For every thin client, test from a checkout whose project differs from the daemon checkout and assert the persisted row scope.","principle":"A project-scoped request must carry an authoritative project identity from the caller.","root_cause":"scope='project' names a scope kind only; the planned thin-client payloads do not resolve/send the invoking checkout's project UUID, and the HTTP fallback reads daemon process context.","section_id":"4.3","severity":"blocking"},{"category":"over-engineering","check_key":"unobservable-orphan-accounting","description":"The orphaned counter is unobservable as designed and adds ceremony around an already-complete SET NULL recovery policy.","finding_id":"F15","fix":"At restraint rung 1, remove orphaned from refresh_template_instances and startup logging. Keep detached instances materialized and untouched under the existing FK behavior.","location":"P2 §2.2 refresh result and P3 §3.2 startup logging","prevention":"For every proposed status counter, identify the durable source state and the consumer that changes behavior based on it.","principle":"Operational fields need both an observable source and a concrete consumer.","root_cause":"ON DELETE SET NULL removes the template id before refresh, while orphaned exists only for logging and cannot be computed from the stated iteration.","section_id":"2.2","severity":"nit"}],"reviewer_session":"dd769e89-9571-4937-b05e-9f101207b219","round":1,"verdict":"needs_review"},"session_id":"997e04ce-bdf0-4be9-aa5f-92f307376b63"}
```

**Round 3** `kind: verification`

- reviewer_run: 230e5218-26ad-432a-9072-8ec3a008cf14
- reviewer_session: 0b42fc1e-e4b6-43fa-b7e7-ce95ac5c0aa3
- adversary_round: 2 of 3
- verdict: needs_review
- findings:
- F16 / blocking / `get_client_session("github")` callers in `integrations/github_helper.py`, `servers/routes/source_control.py`, and `sync/task_github_import.py` are untargeted, and `GitHubIntegration`/`LinearIntegration` are constructed without the project their availability checks need
- F17 / blocking / V2 still requires game-goblins instance YAML that no deliverable owns; verification must be self-contained
- F18 / blocking / `MCPServerTemplate` carries no `enabled`, so template YAML cannot request an initially disabled row and instantiation through a disabled template is undefined
- F19 / blocking / 5.1 is labelled `refactor` although it changes installer output, warnings, and persistence behaviour
- F20 / blocking / V2 step 3 lists bare pytest paths without the isolated test-hub prefix
- F21 / blocking / the six converted legacy templates have no acceptance pinning their exact command, args, secret mappings, optional behaviour, and runtime hook
- F22 / blocking / 3.2 acceptance 3.2.8 depends on `refresh_server` (4.1) and the `server_id` refresh route (4.3) that land after it
- F23 / blocking / `refresh_server` linearizability: already-connected calls bypass the lock, stale-session recovery can discard the newly installed connection, and remove/update do not share the lock
- F24 / blocking / name-keyed `get_available_servers()` / `list_tools()` cannot represent same-named instances across projects once the manager loads every project
- F25 / blocking / `ExpandedInstance` declares only `missing_secrets` while 3.2 consumes `optional_missing_secrets`; a materialised optional secret deleted later fails closed on lazy connect
- F26 / blocking / Constraints states adoption as command/args equality while 3.2 requires every template-owned runtime field
- F27 / blocking / `refresh_mcp_tools` must keep its schema-hash, stale-tool cleanup, embedding, force, and statistics pipeline around `refresh_server`
- F28 / blocking / `MCPServerConfig` fields are added in 4.1 while 2.2 and 3.1 construct and test against them earlier
- F29 / blocking / HTTP scope precedence conflicts between 4.2 and 4.3, and the sessionless web MCP-tab payload would be refused with `project_scope_unresolved`
- resolution_notes: All fourteen accepted; every vote walked the restraint ladder. Typed repair (F21 acceptance 3.1.8) applied through `apply_plan_review_repairs`. Prose fixes hand-applied: F16 (rung 2: `github_helper.py`, `task_github_import.py` and their tests added to 4.2; `source_control.py` and its test added to 4.3 with a new `servers/routes/source_control_github.py` split target; both integrations gain keyword-only `project_id` that the sync services pass), F17 (rung 6: V2 step 5 runs from any second registered project with verification-only instance YAML; Overview names game-goblins as the first adopter after the plan ships), F18 (rung 2: `enabled` on the template YAML/model/definition, applied on first creation, preserved on drift; a disabled template shadows and returns `template_disabled` on every add/sync surface), F19 (rung 6: 5.1 recategorised as `code`), F20 (rung 6: V2 step 3 rendered as bounded, prefixed pytest commands), F22 (rung 2: live reconciliation and acceptance 3.2.8 moved from 3.2 to 4.3 as 4.3.10; 3.2 keeps persistence and `affected_ids`), F23 (rung 2: refresh/remove/update share the per-id lock; stale discard is conditional on connection identity; 4.1.6 restated as the safety property), F24 (rung 2: `get_available_servers(project_id=)` and `list_tools(project_id=)` filter to the visible set; the workflow inventory threads the workflow project; same-name regression added), F25 (rung 2: `ExpandedInstance` carries `missing_secrets` and `optional_missing_secrets`; only required names suppress connection; template-owned configs re-expand through `refresh_template_instances(server_id=)` before secret resolution on lazy connect), F26 (rung 6: Constraints restated with the 3.2 field-exact predicate), F27 (rung 2: `refresh_server` is the reload phase inside the existing route pipeline; hashes stay keyed by `(name, project_id)`; changed-schema and removed-tool acceptance added), F28 (rung 2: `MCPServerConfig` fields and the `models.py` target moved to 2.2; staging guards removed; 4.1.1 restated), F29 (rung 6: one precedence order — effective session, explicit non-empty `project_id`, explicit `scope: "global"`; sessionless request with neither resolves to global as the documented web-tab legacy path until D2; precedence and web-compat assertions added to 4.3.9).

```json plan-review-round
{"evidence_id":"3400d703-827c-42c2-9027-0ab5f1ea16d2","plan_hash":"d2e5a65199feb4da7bf0dc9b3a10a208b46ee2348a900487a84afb78862ec3d8","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1b5476d44ccf370306313158768ba5793b70d6824d2c486a8f2f4f93edb60eb8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":14,"total":16},"evidence_id":"3400d703-827c-42c2-9027-0ab5f1ea16d2","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"6ce855b3bb4c1fa431e791c88392c2490629fca15b3223a9b1e51fd74f26ca79","status":"valid"},"source_digest":"a24dd41ab59194732874704f4f8f37778b9fc3d600b1ea09dcf526bcd68eba49","version":1},"findings":[{"category":"traceability","causal_finding_id":"F2","causal_section_ids":["4.2"],"check_key":"uuid-boundary-consumer-completeness","description":"GitHubMCPHelper, the source-control route, and GitHubIssueImporter still call get_client_session with the literal name github; GitHubSyncService and LinearSyncService also construct integrations without the project_id that §4.2 says their availability checks use.","finding_id":"F16","fix":"At restraint rung 2, extend §4.2 Targets to those callers, constructors, and focused tests; resolve through services/server_resolution.py and pass config.id. Split source_control.py into a same-extension MCP helper before adding work so the 928-line file stays below the ceiling.","introduced_in_round":1,"location":"P4 / §4.2 direct manager-consumer sweep and Targets","prevention":"Sweep every renamed facade method, then trace constructor inputs and focused tests for each production caller.","principle":"A key-identity migration is complete only when every direct consumer resolves at the declared boundary and crosses it with the new key.","root_cause":"The repaired sweep regex omits get_client_session, and unchanged integration constructors carry no project identity.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"F4","causal_section_ids":["Overview","V2"],"check_key":"cross-project-instance-deliverable-ownership","description":"V2 cannot complete unless lightspeed.yaml and tcgplayer.yaml already exist in game-goblins, yet this plan, a Gobby task, and D1/D2 own none of that work.","finding_id":"F17","fix":"At restraint rung 6, make V2 self-contained: create a temporary registered second project and temporary instance YAML during verification, exercise sync and project isolation there, and recast game-goblins adoption in Overview as a post-plan operator follow-up. A typed game-goblins deferral is the alternative when that repository outcome remains required.","introduced_in_round":1,"location":"Overview and V2 game-goblins verification prerequisite","prevention":"Trace every cross-repository verification fixture to a manifest leaf or typed deferral before calling it an input.","principle":"A required verification input needs an executable owner inside the plan or a typed external dependency.","root_cause":"The round-1 repair relabeled two game-goblins files as operator input while V2 still requires them to exist.","section_id":"V2","severity":"blocking"},{"category":"missing-requirement","check_key":"bundled-template-enabled-lifecycle","description":"Template YAML cannot request an initially disabled registry row, and the plan leaves explicit instantiation through a disabled project or global template undefined.","finding_id":"F18","fix":"At restraint rung 2, reuse the bundled-content contract: add enabled: true to the YAML/model/definition, apply it on initial creation or explicit restore, preserve the stored toggle on ordinary drift, and make a disabled project template shadow the global template with a template_disabled result on every add/sync surface.","location":"Constraints and §§2.2, 3.1–3.2 template lifecycle","prevention":"Diff each new bundled-content model and sync transition against src/gobby/install/shared/AGENTS.md, including create, drift, disable, shadow, and restore paths.","principle":"A plan claiming lifecycle parity must carry every governing lifecycle field from source definition through installed behavior.","root_cause":"MCPServerTemplate omits enabled even though the canonical bundled-content contract applies a template's initial enabled value and preserves the installed toggle on drift.","section_id":"3.1","severity":"blocking"},{"category":"gobby-format","check_key":"manifest-category-behavior-parity","description":"The current shadow manifest routes §5.1 as a behavior-free refactor, so its observable retirement and installer changes receive the wrong execution and test policy.","finding_id":"F19","fix":"At restraint rung 6, change §5.1 to category code, route it with implementation_domain backend, and use tdd: true for its installer and retirement behavior.","location":"P5 / §5.1 heading and shadow-manifest routing","prevention":"Classify a deliverable as refactor only when every acceptance item preserves behavior.","principle":"Manifest category must reflect the behavior implemented because category controls agent routing and TDD.","root_cause":"Section 5.1 is labeled refactor although it changes installer output, warnings, import behavior, and persistence behavior.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"isolated-pytest-command-contract","description":"An executor can run the V2 focused MCP, storage, route, and CLI suites against the user's daemon database because the safety prefix appears only on the smoke command.","finding_id":"F20","fix":"At restraint rung 6, replace the path list with bounded commands prefixed exactly by DATABASE_URL=\"${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}\" GOBBY_TEST_PROTECT=1 uv run pytest.","location":"V2 step 3 focused Python suites","prevention":"Render every pytest verification line as a copyable protected command and keep packages bounded.","principle":"Repository verification commands must preserve the isolated test-hub boundary.","root_cause":"V2 lists broad pytest paths without the mandatory DATABASE_URL and GOBBY_TEST_PROTECT=1 prefix.","section_id":"V2","severity":"blocking"},{"category":"weak-testability","check_key":"legacy-template-definition-parity","description":"The leaf can close with a loadable github, linear, brave-search, context7, playwright, or chrome-devtools template that differs from the legacy runtime contract and then prevents safe adoption.","finding_id":"F21","fix":"At restraint rung 6, add one parameterized test over all six legacy templates, including default_secret mappings, context7 optional omission, exact package args, and the chrome runtime hook.","location":"P3 / §3.1 Acceptance","prevention":"Map every converted registry row to a parameterized assertion of its persisted definition.","principle":"A conversion that feeds exact-match adoption needs acceptance evidence for each converted definition.","repairs":[{"items":[{"artifact":"test: `tests/mcp_proxy/test_templates.py::test_bundled_template_definitions_match_legacy_contracts`","prose":"All six converted legacy templates preserve their exact command, arguments, secret mappings, optional behavior, and runtime hook contracts"}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"Acceptance checks that seven templates load and pins OpenAPI, while the six legacy command, argument, secret, optional, and hook contracts remain prose-only.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F5","causal_section_ids":["3.2","4.1","4.3"],"check_key":"file-sync-to-live-runtime-transition","description":"Section 3.2 cannot prove live no-restart reconciliation when it executes because refresh_server and the server_id-aware HTTP refresh behavior have not landed.","finding_id":"F22","fix":"At restraint rung 2, keep §3.2 responsible for persistence and affected_ids; move _sync_user_templates_to_db live reconciliation, its target, and acceptance 3.2.8 into §4.3, which already depends on §3.2. Avoid a new leaf.","introduced_in_round":1,"location":"P3 / §3.2 dependency and acceptance 3.2.8","prevention":"For every cross-section call named in acceptance, verify the callee owner precedes the caller in the manifest graph.","principle":"A leaf's acceptance must be satisfiable from its declared predecessors.","root_cause":"The round-1 repair makes §3.2 call refresh_server and a scoped refresh route owned by §4.1/§4.3, while §4.3 already depends on §3.2.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F7","causal_section_ids":["4.1"],"check_key":"refresh-server-linearizable-transition","description":"Refresh can disconnect an in-flight call, that call's stale-session recovery can pop the newly installed connection, and a concurrent remove or update can race after the DB reread.","finding_id":"F23","fix":"At restraint rung 2, reuse the per-id lock for refresh/remove/update and make stale discard conditional on the exact connection generation or identity it used. Specify that overlapping calls may finish or retry, then test the required safety property: every call starting after refresh returns uses the new secret. Do not add a lease/drain subsystem.","introduced_in_round":1,"location":"P4 / §4.1 refresh_server concurrency contract","prevention":"Enumerate refresh against active call, stale retry, remove, update, enable/disable, and reconnect before claiming linearizability.","principle":"A credential refresh must order every mutation and prevent stale recovery from discarding newer state.","root_cause":"The existing connection lock protects connection establishment; already-connected calls use sessions outside it, and remove/update/stale discard do not share the proposed refresh ordering.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F3","causal_section_ids":["4.1"],"check_key":"available-servers-return-shape-compatibility","description":"No-argument get_available_servers and list_tools cannot represent two same-named instances: a name-keyed dict overwrites one scope and a global name set can validate a workflow against another project's server.","finding_id":"F24","fix":"At restraint rung 2, preserve list[str] and dict[name, tools] only after caller-scoped project/global selection. Thread the workflow's project_id into _WorkflowMCPInventory and add a same-name/different-schema regression.","introduced_in_round":1,"location":"P4 / §§4.1–4.2 workflow MCP inventory","prevention":"Test duplicate names with different schemas through each compatibility inventory consumer.","principle":"A compatibility return shape must still represent the scoped domain after the backing registry changes.","root_cause":"The round-1 repair preserves name-keyed inventories after load_initial_configs expands the manager to every project.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F8","causal_section_ids":["2.3","3.1","4.1"],"check_key":"template-missing-secret-fail-closed","description":"The result schema cannot produce §3.2's optional_missing report, and deleting a context7 optional secret after materialization can send lazy connection into needs_configuration before re-expansion.","finding_id":"F25","fix":"At restraint rung 6, add explicit required missing_secrets and optional_missing_secrets fields, let only required names drive connection suppression, and reuse template re-expansion before resolving references on template-owned reconnect paths.","introduced_in_round":1,"location":"P3 §3.1 expansion result through P4 connection paths","prevention":"Test create, appearance, deletion, lazy reconnect, refresh, and restart for one required and one optional secret parameter.","principle":"Required and optional secret absence must remain distinct across serialization and every reconnect path.","root_cause":"ExpandedInstance declares only missing_secrets, later prose consumes optional_missing_secrets, and a formerly materialized optional reference becomes indistinguishable from a required reference after deletion.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"legacy-adoption-predicate-consistency","description":"An implementation following Constraints can attach a customized env, header, URL, or timeout row and let the next template refresh overwrite that customization.","finding_id":"F26","fix":"At restraint rung 6, replace the Constraints sentence with §3.2's exact expanded-config predicate over transport, URL, command, args, env, headers, and connect_timeout, retaining the first-differing-field adoption_skipped report.","location":"Constraints adoption default versus §3.2 adoption algorithm","prevention":"State the adoption predicate once and reuse its field list in prose, code, and acceptance.","principle":"A provenance adoption predicate must have one exact definition because the next refresh trusts it.","root_cause":"Constraints permits adoption on base command/args equality while §3.2 requires equality across every template-owned runtime field.","section_id":"Constraints","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"F5","causal_section_ids":["3.2","4.1","4.3"],"check_key":"refresh-pipeline-semantic-index-preservation","description":"A scoped refresh can reconnect successfully while removed tools remain indexed and changed schemas remain stale in semantic search.","finding_id":"F27","fix":"At restraint rung 2, make refresh_server the runtime-reload phase inside the existing refresh pipeline. Preserve schema comparison, hash updates, stale-tool cleanup, embedding regeneration, force semantics, and response statistics keyed by id with name/scope metadata; add changed-schema and removed-tool acceptance.","introduced_in_round":1,"location":"P4 / §4.3 refresh_mcp_tools route","prevention":"Diff every existing refresh outcome before substituting a new runtime reload primitive.","principle":"A runtime reload must compose with the established cache, index, and observability pipeline.","root_cause":"The round-1 repair says the route calls refresh_server and cache_discovered_tools without preserving the route's schema hashes, stale-tool cleanup, embeddings, force behavior, or statistics.","section_id":"4.3","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"F6","causal_section_ids":["2.2","3.1","4.1"],"check_key":"template-metadata-runtime-rehydration","description":"Sections 2.2 and 3.1 cannot cleanly construct MCPServerConfig(id, template_id, template, runtime_hook, template_values) or run hook-dispatch acceptance before their downstream dependent §4.1 lands.","finding_id":"F28","fix":"At restraint rung 2, move those MCPServerConfig fields and the models.py Target into §2.2, remove the temporary hasattr/getattr staging from §§2.2/3.1, and leave §4.1 responsible only for re-keying runtime state.","introduced_in_round":1,"location":"P2 §2.2, P3 §3.1, and P4 §4.1 MCPServerConfig ownership","prevention":"Place shared model fields in the earliest dependency and remove temporary compatibility branches before finalizing sequencing.","principle":"A field must exist before upstream leaves construct it or close acceptance against it.","root_cause":"The round-1 repair makes §2.2 to_config and §3.1 runtime-hook tests need fields that §4.1 adds after §3.1.","section_id":"3.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"F14","causal_section_ids":["4.3"],"check_key":"thin-client-caller-project-identity","description":"The promised unchanged web MCP payload will receive project_scope_unresolved, and session-bound requests have two conflicting project-authority orders.","finding_id":"F29","fix":"At restraint rung 6, define one order: effective session project first, explicit project_id for sessionless thin clients second, explicit global when requested. Preserve the current sessionless manual web payload as a documented global legacy path until D2 adds project selection, and add precedence plus web compatibility tests.","introduced_in_round":1,"location":"P4 / §§4.2–4.3 project authority and D2 web compatibility","prevention":"Run the scope matrix through CLI, stdio, session-bound HTTP, and sessionless web management payloads.","principle":"Every management surface needs one authoritative scope rule that preserves its stated compatibility contract.","root_cause":"The round-1 repair refuses unresolved project scope and gives HTTP body/query precedence, while §4.2 gives effective session precedence and the existing web client supplies neither project_id nor scope.","section_id":"4.3","severity":"blocking"}],"reviewer_session":"0b42fc1e-e4b6-43fa-b7e7-ce95ac5c0aa3","round":2,"verdict":"needs_review"},"session_id":"997e04ce-bdf0-4be9-aa5f-92f307376b63"}
```

**Round 4** `kind: verification`

- reviewer_run: d229aafd-3ea1-44a8-b27e-fe13e2836189
- reviewer_session: 81f232ed-6b51-4b5c-9e62-580ea33f74b1
- adversary_round: 3 of 4 (cap raised from 3 to 4 at this checkpoint by the user)
- verdict: needs_review
- findings:
- F30 / blocking / HTTP scope precedence lists session project ahead of explicit global while promising explicit global wins, and the sessionless reconciliation payload `{server_id}` lands in the global legacy path so a project row's id is unknown
- F31 / blocking / optional-secret appearance and deletion across startup, lazy connect, and refresh have no acceptance oracle
- F32 / blocking / only instance-YAML sync pins `template_disabled`; the management path and its HTTP, CLI, stdio, and MCP adapters have no rejection-plus-non-persistence acceptance
- F33 / blocking / the five regenerated `tests/runtime_grants/golden/*.json` identity carriers are unowned by any Targets block
- F34 / blocking / 5.2 targets the bundled content manifest but no acceptance proves the new skill is in it
- F35 / nit / `client_manager/registry_ids.py` is a speculative split target with no consumer (manager.py is 268 lines, server_registry.py 445)
- F36 / blocking / exact-scope secret delete and the global fallback it reveals have no acceptance
- F37 / blocking / the smoke test cleans up only after every assertion; an early failure orphans `uvx` and the fixture HTTP thread
- F38 / blocking / `cleanup_replaced_mcp_secrets` and `remote_preflight._read_remote_config` still read `secrets` by name only
- F39 / blocking / instance YAML sync calls `get_template(name, ...)` with the instance name instead of the parsed `template` field
- F40 / blocking / template sync never prunes rows whose source file disappeared and never restores a bundled definition after a user override is removed
- F41 / blocking / `refresh_server` swaps the secret-resolved config into `_configs`, exposing plaintext to registry consumers
- F42 / blocking / management add is check-then-upsert; two concurrent adds both pass and `ON CONFLICT UPDATE` silently overwrites
- F43 / blocking / PATCH, DELETE, enable, and remove resolve with global fallback, so a project-scoped mutation can alter the global row
- F44 / blocking / `refresh_server` ignores the re-expansion `errors` envelope and reconnects stale configuration as success
- F45 / blocking / legacy embedding points carry no `server_id`/`project_id`, so scoped semantic search drops them until a forced refresh
- F46 / blocking / templated PATCH `values` has no merge, removal, or atomic-failure semantics against persisted `template_values`
- F47 / blocking / a bare token matching `[A-Za-z_][A-Za-z0-9_]*` is indistinguishable from a secret name, so a raw credential can be persisted as `$secret:<credential>`
- F48 / blocking / refresh resolves secrets before disconnecting, so a deleted required secret raises `needs_configuration` while the old credentialed transport stays callable
- resolution_notes: All nineteen accepted; every vote walked the restraint ladder and none was declined over-mechanism. Typed repairs (F31 acceptance 4.1.9; F32 acceptances 4.2.7 and 4.3.13; F33 five golden-vector Targets in 2.1; F34 acceptance 5.2.3; F36 acceptance 2.3.6) applied through `apply_plan_review_repairs`. Prose fixes hand-applied: F30 (rung 6: one precedence case table shared by 4.2/4.3 with explicit global first, and reconciliation sends each affected row's own `project_id` or `scope: "global"`); F35 (rung 1: `registry_ids.py` target and split clause removed); F37 (rung 2: bounded `finally` disconnect, `shutdown()`/`server_close()`, thread join); F38 (rung 2: scoped metadata read in `cleanup_replaced_mcp_secrets`, `remote_preflight._read_remote_config` targeted and pinned to `GLOBAL_PROJECT_ID`); F39 (rung 2: `get_template(template_name, ...)`, `instance_name` only to expansion and upsert); F40 (rung 2: `sync_rules`-style authoritative prune after a non-empty error-free scan, FK `SET NULL` detaches instances as manual rows, same-run bundled restore); F41 (rung 2: `_configs` keeps the unresolved config, the resolved copy reaches only the connection); F42 (rung 4: `insert_server` with `ON CONFLICT DO NOTHING RETURNING` for management creation, `upsert` stays for sync); F43 (rung 2: mutations use exact `(name, project_id)` lookup, fallback only for reads); F44 (rung 2: expansion errors keep the last-known-good connection and set stale-template health); F45 (rung 2: one idempotent scoped-payload backfill from `init_mcp_stack` guarded by a `config_store` marker); F46 (rung 2: persisted `template_values` merge base, `null` removes a parameter, atomic write); F47 (rung 2: bare names only when the secret exists, forward references need `$secret:`); F48 (rung 2: configuration failure under the lock pops and disconnects the exact old connection). Base and expansion validation clean after the edits.

```json plan-review-round
{"evidence_id":"2986d3fc-0011-402c-94b1-b5968112d80e","plan_hash":"0efc3da3c818fe952cda3bba06a5d03035123c495177daadcd75004980597b0e","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ab9c86114cc0a6102cfbb83193e4c23715ff34640d6c24fab041cb32a621dbe2","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":19,"total":20},"evidence_id":"2986d3fc-0011-402c-94b1-b5968112d80e","lanes":[{"candidate_count":7,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":13,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"307ace984f9029a86d75f46e7e3d2fb5690f00fc47c4af4cce17220df2f881c8","status":"valid"},"source_digest":"8b146740dc64cc4bdc986666e68a3dd9a0c60d2dffbc89ebb9a4801090fada3e","version":1},"findings":[{"category":"unhandled-edge","check_key":"scope-authority-total-function","description":"The scope contract cannot be implemented literally. It also makes §4.3.10's project-row reconciliation fail because `POST /api/mcp/refresh {server_id}` supplies no project authority.","finding_id":"F30","fix":"At restraint rung 6, replace both precedence passages with one case table and helper contract: explicit `scope=\"global\"` selects global; otherwise a session-bound request uses its effective project; otherwise non-empty `project_id` selects that project; unresolved explicit project is 400; only the documented sessionless legacy payload defaults global. Require reconciliation to load each affected row with `get_server_by_id` and send its `project_id` or explicit global scope.","location":"§4.2 caller-project resolution / §4.3 HTTP precedence and live reconciliation","prevention":"Write a truth table for session, explicit project_id, explicit global, unresolved project, and legacy payloads; exercise every conflicting combination and every internal caller.","principle":"Scope selection must be one total function of request inputs, and privileged global mutation must be explicit.","root_cause":"The prose lists session project ahead of explicit global while also promising that explicit global wins for session-bound callers; the sessionless live-reconciliation payload sends only server_id and therefore falls into the global legacy path.","section_id":"4.3","severity":"blocking"},{"category":"weak-testability","check_key":"optional-secret-lifecycle-oracle","description":"An implementation can pass current acceptance while failing to materialize a newly added optional secret or remove a deleted one before a later connection.","finding_id":"F31","fix":"At restraint rung 6, add one parameterized manager test covering the complete optional-secret lifecycle; no additional runtime abstraction is needed.","location":"§3.1 optional secret expansion / §4.1 startup, lazy-connect, and refresh paths","prevention":"For each secret class, test absent, added, rotated, deleted, restart, lazy connect, and explicit refresh.","principle":"Every stateful credential transition needs an acceptance oracle at the runtime boundary.","repairs":[{"items":[{"artifact":"test: `tests/mcp_proxy/test_mcp_manager.py::test_optional_secret_reexpands_on_all_connection_paths`","prose":"Template-owned configs re-expand before startup load, lazy connect, and refresh so an optional secret's appearance or deletion materializes or removes its env/arg without entering needs_configuration."}],"kind":"add_acceptance","section_id":"4.1"}],"root_cause":"The plan tests pure optional-secret expansion and required-secret failure, leaving optional-secret appearance and deletion across connection paths unpinned.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"disabled-template-surface-oracle","description":"The plan promises disabled-template rejection on every surface, yet an adapter can persist or misreport a disabled template without violating current acceptance.","finding_id":"F32","fix":"At restraint rung 2, reuse the single management path and add one core non-persistence test plus one adapter-parity acceptance; no per-adapter policy implementation is needed.","location":"§3.1 enabled lifecycle / §4.2 management service / §4.3 adapters","prevention":"Enumerate every instantiation surface for lifecycle gates and cover the shared core plus adapter parity.","principle":"A shared rejection policy is complete only when the core write boundary and its externally visible adapters are acceptance-covered.","repairs":[{"items":[{"artifact":"test: `tests/mcp_proxy/test_server_mgmt.py::test_add_disabled_template_returns_template_disabled_without_persisting`","prose":"Adding from a disabled project or global template returns template_disabled and persists no mcp_servers row."}],"kind":"add_acceptance","section_id":"4.2"},{"items":[{"artifact":"behavior: \"disabled-template instantiation parity across adapters\"","prose":"HTTP, CLI, stdio, and MCP template-instantiation adapters preserve the shared template_disabled result and create no instance."}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"Only instance-YAML sync pins `template_disabled`; the management, HTTP, CLI, stdio, and MCP add paths have no acceptance for rejection plus non-persistence.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"targets-generated-carriers","description":"The runtime-grant golden vectors are required by the body and acceptance 2.1.2 but are unowned by any Targets block.","finding_id":"F33","fix":"At restraint rung 2, add the five existing generated carriers to §2.1 Targets. Acceptance 2.1.2 already covers their identity update, so no extra criterion is needed.","location":"§2.1 Targets and golden-vector regeneration paragraph","prevention":"Expand every regeneration instruction into its complete emitted-file inventory before validation.","principle":"Every file a deliverable requires changing must appear in that deliverable's Targets.","repairs":[{"entries":["`tests/runtime_grants/golden/brokered_datastores.json`","`tests/runtime_grants/golden/direct_datastores.json`","`tests/runtime_grants/golden/old_client_new_grant.json`","`tests/runtime_grants/golden/payload_skew_unknown_field.json`","`tests/runtime_grants/golden/unavailable_datastores.json`"],"kind":"add_targets","section_id":"2.1"}],"root_cause":"The test harness is targeted, while the five generated JSON identity carriers it rewrites are omitted.","section_id":"2.1","severity":"blocking"},{"category":"weak-testability","check_key":"manifest-regeneration-acceptance","description":"The expanded §5.2 task can close without proving that `mcp-servers/SKILL.md` was added to the bundled content manifest.","finding_id":"F34","fix":"At restraint rung 2, reuse the existing bundled-content integrity test and add one acceptance item.","location":"§5.2 bundled-content manifest target","prevention":"Pair every generated artifact Target with a focused integrity acceptance item.","principle":"Every shipped output in a manifest leaf needs a source-section validation criterion.","repairs":[{"items":[{"artifact":"test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_shared_tree`","prose":"The regenerated bundled content manifest contains the new mcp-servers skill and passes its integrity check."}],"kind":"add_acceptance","section_id":"5.2"}],"root_cause":"The section requires and targets manifest regeneration, while acceptance covers only the skill and documentation text.","section_id":"5.2","severity":"blocking"},{"category":"over-engineering","check_key":"proportionality-conditional-split","description":"`registry_ids.py` adds a module with no concrete consumer or acceptance item.","finding_id":"F35","fix":"At restraint rung 1, remove `registry_ids.py` from Targets and delete the speculative split clause. If implementation growth actually requires decomposition, name the moved helpers and acceptance in this leaf.","location":"§4.1 `registry_ids.py` Target and conditional split clause","prevention":"Target conditional decomposition files only after naming the symbols they will own and the requirement that forces the split.","principle":"A new module needs a concrete consumer and a committed ownership boundary.","root_cause":"The file is targeted unconditionally but exists only for hypothetical growth in source files currently far below the ceiling.","section_id":"4.1","severity":"nit"},{"category":"weak-testability","check_key":"secret-delete-scope-oracle","description":"A delete can remove the wrong same-named row or fail to reveal the intended global fallback without failing current acceptance.","finding_id":"F36","fix":"At restraint rung 6, add one focused same-name deletion matrix using the already-targeted store tests.","location":"§2.3 exact-scope delete and project-first fallback","prevention":"For scoped stores, test same-name rows across project/global scopes for create, read, delete, fallback, and isolation.","principle":"Security-sensitive scope transitions require explicit before-and-after acceptance checks.","repairs":[{"items":[{"artifact":"test: `tests/storage/test_secrets_store.py::test_delete_is_exact_scope_and_reveals_global_fallback`","prose":"Deleting a project-scoped secret removes only that row and reveals the same-named global fallback, while deleting global never removes another project's row."}],"kind":"add_acceptance","section_id":"2.3"}],"root_cause":"Acceptance covers scoped lookup and CLI flags, while exact deletion and the fallback revealed afterward are implicit.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"smoke-cleanup-all-exits","description":"An early assertion or timeout can orphan `uvx` or leave the fixture HTTP thread and socket alive, contaminating subsequent tests.","finding_id":"F37","fix":"At restraint rung 2, use existing fixture/context-manager patterns: always disconnect under a bounded timeout in `finally`, then call `shutdown()`/`server_close()` and join the HTTP thread. Make the orphan assertion observe that path.","location":"§1.1 smoke-test steps 1-6","prevention":"Require resource-owning test fixtures to use bounded `finally` cleanup and assert cleanup from a forced-failure path.","principle":"Every spawned process, socket, and thread must be released on success, assertion failure, timeout, and cancellation.","root_cause":"Cleanup occurs only after all assertions, and HTTP shutdown, socket close, and thread join are unspecified.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"secret-sql-scope-completeness","description":"Same-named rows can make managed cleanup or remote preflight consume metadata from another project.","finding_id":"F38","fix":"At restraint rung 2, scope `cleanup_replaced_mcp_secrets` metadata reads to its supplied scope, add `remote_preflight.py` to §2.3 and constrain its bootstrap read to `GLOBAL_PROJECT_ID`, then add same-name cross-project tests.","location":"§2.3 direct secret consumers","prevention":"After changing a uniqueness key, run a direct-SQL consumer sweep for the old key and classify every read/write by scope.","principle":"Once uniqueness becomes `(name, project_id)`, every direct SQL read must select an explicit scope.","root_cause":"The plan scopes store APIs and cleanup deletion but misses the name-only ownership SELECT in managed cleanup and the name-only remote-preflight SELECT.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-selector-identity","description":"The documented `lightspeed` instance resolves a nonexistent `lightspeed` template instead of `openapi` and fails as template-missing.","finding_id":"F39","fix":"At restraint rung 2, reuse the parsed template field for `get_template(template_name, project_id=scope)` and pass `instance_name` only to expansion and upsert; add the three-distinct-names regression.","location":"§3.2 instance-YAML algorithm","prevention":"Use distinct `instance_name` and `template_name` variables and test with filename, instance, and template names all different.","principle":"Instance identity and template identity are separate inputs and must stay separate through lookup and persistence.","root_cause":"The algorithm parses `template: openapi` but calls `get_template(name, ...)`, where `name` is the instance name.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-sync-authoritative-removal","description":"Removed template definitions can remain enabled indefinitely and continue refreshing instances from stale DB definitions.","finding_id":"F40","fix":"At restraint rung 2, reuse `sync_rules`' error-safe authoritative scan: track `(name, scope)` present on disk, prune only rows owned by the scanned source after a non-empty error-free scan, and restore bundled definitions in the same run when overrides disappear. Add transition tests.","location":"§3.2 template sync lifecycle","prevention":"For every authoritative file sync, walk add, edit, remove, error, override, and override-removal states.","principle":"A registry claiming file-backed lifecycle parity must define create, edit, removal, and override-reversion transitions.","root_cause":"The sync algorithm only iterates and upserts present files; it never prunes rows owned by removed sources or restores a bundled definition after a user override disappears.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"registry-secret-reference-boundary","description":"Manager listings, updates, and other config consumers can observe plaintext after refresh, diverging from the current transient-resolution contract.","finding_id":"F41","fix":"At restraint rung 2, retain the unresolved DB-derived config in `_configs` and pass a resolved copy only to connection creation. Add startup/connect/refresh assertions that registry state still contains `$secret:` references while the connection receives values.","location":"§4.1 refresh ordering step 4","prevention":"At every secret-resolution call, identify whether the returned object is ephemeral or retained and assert reference/plaintext boundaries.","principle":"Plaintext secrets belong only in ephemeral transport configuration; persistent manager registry state must retain references.","root_cause":"Refresh resolves secrets and then swaps that resolved config into `_configs[server_id]`.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"concurrent-add-atomicity","description":"Two concurrent adds can both pass the check, overwrite one row through `ON CONFLICT UPDATE`, adopt the same UUID, and race independent connections.","finding_id":"F42","fix":"At restraint rung 4, use `INSERT ... ON CONFLICT DO NOTHING RETURNING` for management creation, keep upsert for declarative sync, return the existing-row envelope on conflict before runtime registration, and test one winner/one duplicate/no overwrite.","location":"§4.2 duplicate `(name, scope)` management add","prevention":"For every check-then-write path, require a database conflict outcome and a barrier-controlled concurrency test.","principle":"Duplicate detection and creation must be one atomic database outcome.","root_cause":"A service existence check followed by storage `upsert` is a TOCTOU race; UUID-keyed runtime state also removes the current name-keyed in-memory guard.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"mutation-scope-no-fallback","description":"A project-scoped delete, patch, or enable request can alter the global instance even though `--global` is the explicit machine-scope selector.","finding_id":"F43","fix":"At restraint rung 2, reuse exact `(name, project_id)` storage lookup for PATCH/DELETE/enable/remove and keep fallback only for execution, schema, resource, and listing reads. Test that project-scoped mutations return unknown when only global exists.","location":"§4.2 remove service / §4.3 PATCH, DELETE, and enable routes","prevention":"Separate read resolution from mutation targeting and include global-only rows in the mutation scope matrix.","principle":"Visibility fallback is a read policy; mutations must target the exact explicitly selected scope.","root_cause":"Mutation routes reuse project-first/global-fallback resolution, so absence of a project row reveals the global row as a write target.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"refresh-expansion-error-propagation","description":"A tightened choice or newly required template parameter can produce an expansion error while explicit refresh reconnects stale configuration and appears successful.","finding_id":"F44","fix":"At restraint rung 2, consume the existing per-id `errors` envelope. On error preserve any last-known-good connection, skip swap/reconnect, set stale-template health with parameter names only, return the error, and test explicit refresh plus startup behavior.","location":"§2.2 `refresh_template_instances` error envelope / §4.1 `refresh_server` ordering","prevention":"For every staged refresh, define the outcome and state transition for failure at each prerequisite.","principle":"A refresh cannot report success or reconnect after a prerequisite re-expansion failed.","root_cause":"The per-id expansion result is ignored; refresh rereads unchanged materialized fields and proceeds.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"embedding-payload-backfill","description":"Scoped semantic search can omit legacy points or confuse same-named project/global instances after cutover.","finding_id":"F45","fix":"At restraint rung 2, reuse the existing force-embed pipeline for a one-time idempotent reconciliation of all external tool points, writing `server_id`, `server_name`, and owning `project_id` and removing superseded legacy points. Seed legacy payloads in the test.","location":"§4.2 semantic payload migration / §4.3 refresh pipeline","prevention":"For every payload-schema change, inventory existing points and specify backfill, stale-point removal, idempotence, and pre/post tests.","principle":"Changing an indexed identity or filter requires a bounded migration for existing indexed records.","root_cause":"Only future embedding writes carry `server_id` and owning scope; unchanged adopted servers retain legacy vector payloads until a forced refresh.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"patch-values-merge-semantics","description":"A one-field PATCH can either fail required validation or silently discard optional/default-secret references depending on an unstated implementation choice.","finding_id":"F46","fix":"At restraint rung 2, use persisted `template_values` as the merge base, define an explicit parameter-removal sentinel, run full expansion validation, and atomically update normalized values plus materialized fields. Add partial-update, removal, and validation-failure tests.","location":"§4.3 templated PATCH `values`","prevention":"For every partial-update object, specify absent, present, null, empty, invalid, and atomic-failure outcomes.","principle":"PATCH must define merge, replacement, and removal semantics before implementation.","root_cause":"The plan says to re-expand `values` without stating how they combine with persisted `template_values`.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"secret-reference-input-ambiguity","description":"A raw credential such as a token-shaped alphanumeric string can be persisted inside `template_values` as `$secret:<credential>` and later exposed in missing-secret reports.","finding_id":"F47","fix":"At restraint rung 2, reuse the existing `$secret:<name>` syntax: bare secret names are accepted only when they already exist, while forward references require the explicit prefix. Update every surface, example, normalization rule, and negative test.","location":"§3.1 secret parameter grammar across YAML, MCP, CLI, and HTTP inputs","prevention":"Use tagged reference syntax at secret-bearing inputs and test credential-shaped alphanumeric/underscore strings.","principle":"A trust boundary cannot claim raw-secret rejection when reference identity is inferred from an ambiguous bare string.","root_cause":"Many real credentials satisfy `[A-Za-z_][A-Za-z0-9_]*`, so the name grammar alone cannot distinguish a secret name from a raw value.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"refresh-missing-secret-disconnect","description":"Deleting a required secret and calling refresh can raise `needs_configuration` while leaving the old credential-bearing transport cached and callable.","finding_id":"F48","fix":"At restraint rung 2, reuse the identity-conditional discard helper under the per-id lock: catch configuration failure, pop/disconnect the exact old connection, invalidate caches, set `needs_configuration` health, and add a test proving later calls cannot reuse it.","location":"§4.1 `refresh_server` steps 3-5","prevention":"For credential removal, assert both the new connection is absent and the prior connection object is disconnected and unreachable.","principle":"When credential refresh fails closed, no transport authenticated with the invalidated old credential may remain reachable.","root_cause":"Secret resolution occurs before the old connection is popped and disconnected; its exception path has no cleanup transition.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"81f232ed-6b51-4b5c-9e62-580ea33f74b1","round":3,"verdict":"needs_review"},"session_id":"997e04ce-bdf0-4be9-aa5f-92f307376b63"}
```

**Round 5** `kind: verification`

- reviewer_run: 583c72d4-d7b1-4550-ab29-a6facac7f0d2
- reviewer_session: 2f6b805c-edd0-44a5-a6a8-af1527ce45c1
- adversary_round: 4 of 4 (review cap)
- verdict: needs_review
- findings:
- F49 / blocking / bad-sequencing / 2.1: hop 408 is taken by `408_structured_session_handoffs.sql` (#21140, now committed together with 409 and 410) while the Target and 2.1.1 hard-code 408 — accepted; the hop becomes 411 under the existing recompute-at-start rule
- F50 / blocking / traceability / 3.1: the parent shared-content contract `src/gobby/install/shared/AGENTS.md` still routes project customisation to `.gobby/install/<kind>/<name>/` — accepted (typed repairs)
- F51 / blocking / unhandled-edge / 4.2: `resolve_request_scope` carries no fallback or registry input, so the all-empty tuple and the unregistered-project refusal are adapter-dependent — accepted (`fallback_project_id` and `project_exists` inputs)
- F52 / blocking / unhandled-edge / 2.2: `protect_mcp_mapping` writes deterministic secret slots before `INSERT … ON CONFLICT DO NOTHING`, so a conflict loser overwrites the winner's managed secrets — accepted (conflict insert first, secret protection only for the returned id)
- F53 / blocking / unhandled-edge / 3.2: the ≥1-file prune guard skips the deletion of the last project template or last global override — accepted (scanned-root tracking)
- F54 / blocking / unhandled-edge / 4.3: the templated PATCH merge is a read-modify-write outside the per-id lock — accepted (one id-keyed manager operation under the lock; DELETE is a non-resurrecting winner)
- F55 / blocking / unhandled-edge / 3.2: join-derived `runtime_hook` is lost when FK detachment nulls `template_id` — accepted (`mcp_servers.runtime_hook` column materialised in the hop)
- F56 / blocking / unhandled-edge / 4.1: cancellation during old-connection teardown orphans the transport after the registry pop — accepted (shielded bounded teardown)
- F57 / blocking / unhandled-edge / 4.1: `refresh_server` has no `enabled` branch, so live-sync reconciliation would connect a disabled instance — accepted
- F58 / blocking / traceability / 2.2: new and deleted `mcp_servers.py` symbols have no file-wide Target — accepted (`src/gobby/storage/mcp_servers.py::*`)
- F59 / blocking / traceability / 4.2: the incremental embedding writer chain is untargeted — accepted (typed repairs)
- F60 / blocking / traceability / 4.3: the deleted `_current_project_id` helper and its caller `_body_project_id` have no Targets — accepted (typed repair plus `_body_project_id`)
- resolution_notes: all twelve findings accepted; typed repairs applied through `apply_plan_review_repairs` (F50, F59, F60), the remaining nine hand-applied as prose fixes on the repaired artifact. The review cap (4) is reached, so no further adversary round runs; see the human-handoff entry below.

```json plan-review-round
{"evidence_id":"fda1fc70-8206-4ecf-b69e-21b9eb043f96","plan_hash":"ff8e319b7df99f353aaa9c2373469f67a8a8f4116b3fc7455184903fbf28a9ad","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"aa860b47f6526ab4d90d72373ec2818b6977435d2859c0b4d9fd22504abc5eef","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":12,"total":14},"evidence_id":"fda1fc70-8206-4ecf-b69e-21b9eb043f96","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"delegated-verified"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":11,"manifest_digest":"8e3c5a63523e860f75a87c1d37612ab0b1168e96ce30488a114104b0ca39cd64","status":"valid"},"source_digest":"2eeb703465146f2a433cf0579a4c84950b7069130e0d845d80dac196219e447c","version":1},"findings":[{"category":"bad-sequencing","check_key":"schema-hop-serialization","description":"Section 2.1 cannot execute as written: `408_structured_session_handoffs.sql` already occupies hop 408, while this plan targets `408_mcp_templates_project_secrets.sql` and leaves the collision to implementation-time renaming.","finding_id":"F49","fix":"At restraint rung 2, serialize §2.1 behind active schema owner #21140, select the next unique hop after that owner lands (currently 409), and replace 408 consistently in Constraints, Targets, body, Acceptance 2.1.1, and all identity-carrier expectations before expansion.","location":"P2 / §2.1 migration Target, body, and Acceptance 2.1.1","prevention":"Before final plan review, compare every migration Target with the live MIGRATIONS registry and active schema-owning tasks, then encode the serialization dependency.","principle":"A schema migration deliverable needs one serialized owner and one stable, unique hop identity before expansion.","root_cause":"The plan defers the hop choice until implementation while hard-coding 408 in the manifest-backed Target; active task #21140 already owns migration 408.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"parent-shared-content-contract-parity","description":"Implementers will receive conflicting canonical instructions for project-local MCP templates and instances because `src/gobby/install/shared/AGENTS.md` remains unchanged.","finding_id":"F50","fix":"At restraint rung 2, keep the selected `.gobby/mcp/` layout and update the existing parent shared-content contract to name the MCP-specific template/server roots and their override semantics.","location":"P3 / §§3.1–3.2 project template and instance paths","prevention":"For every new project override root, trace the path decision to the highest governing AGENTS.md and include that contract in Targets and acceptance.","principle":"A plan that introduces an exception to a canonical repository contract must update that contract at the owning deliverable.","repairs":[{"entries":["`src/gobby/install/shared/AGENTS.md`"],"kind":"add_targets","section_id":"3.1"},{"items":[{"artifact":"file: `src/gobby/install/shared/AGENTS.md`","prose":"The parent shared-content contract documents `.gobby/mcp/templates/` and `.gobby/mcp/servers/` as the MCP-specific project/global override roots and states their override semantics."}],"kind":"add_acceptance","section_id":"3.1"}],"root_cause":"The plan documents `.gobby/mcp/` only in a new nested instruction file, while the governing parent contract still directs project customization to `.gobby/install/<kind>/<name>/`.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"scope-resolver-explicit-fallback","description":"The all-empty tuple must resolve to `get_project_context()` at the MCP front door and `GLOBAL_PROJECT_ID` for sessionless HTTP, yet the shared function has no surface or fallback input. It also cannot reject an unregistered explicit project without undeclared registry access.","finding_id":"F51","fix":"At restraint rung 2, add explicit `fallback_project_id` and project-existence validation inputs to the shared function. Have MCP pass its resolved project-context fallback and HTTP pass the global legacy fallback, then drive both through the same table, including the identical all-empty tuple.","location":"P4 / §§4.2–4.3 `resolve_request_scope` case table","prevention":"Enumerate equal input tuples across adapters and require explicit resolver inputs for every differing outcome.","principle":"A total scope resolver must receive every input that changes its result; identical inputs cannot have adapter-dependent outcomes.","root_cause":"The specified `(session_project_id, project_id, scope)` signature omits both fallback policy and project-registration validation.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"conflict-loser-secret-atomicity","description":"Two concurrent manual adds with different plaintext credentials can produce one server row while the losing request overwrites the winner's managed secret or creates orphan slots, contradicting the plan's untouched-duplicate guarantee.","finding_id":"F52","fix":"At restraint rung 4, make the no-return conflict roll back all pre-insert secret writes before `insert_server` returns `None`—for example, raise a private duplicate sentinel inside the transaction and catch it only after rollback. Add a barrier test with distinct loser values and keys that proves every secret row remains the winner's.","location":"P2 / §2.2 `insert_server`; P4 / §4.2 concurrent add","prevention":"For every conflict-safe create, inventory side effects that occur before conflict resolution and prove the losing branch rolls all of them back.","principle":"A duplicate create outcome must leave the winning aggregate and all of its dependent secret rows unchanged.","root_cause":"`protect_mcp_mapping` writes deterministic managed-secret slots before `INSERT ... DO NOTHING RETURNING`; a no-return conflict is successful SQL, so the transaction commits the loser's secret mutations.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"authoritative-empty-template-scan","description":"Deleting the only project template or the only global user override yields an empty, error-free scan that prunes nothing. The stale row remains, FK detachment never happens, and same-run bundled restoration never starts.","finding_id":"F53","fix":"At restraint rung 2, track successfully scanned authoritative roots independently of file count. Permit an existing readable authoritative root to produce an empty on-disk set and prune its owned rows; keep missing, unreadable, partial, and erroring roots non-pruning. Add last-project-template and last-global-override transition tests.","location":"P3 / §3.2 template orphan pruning","prevention":"Test zero-to-one and one-to-zero transitions for every authoritative filesystem registry, separately from missing-root and error cases.","principle":"An authoritative file-backed registry must represent an empty authoritative source as deletion of its final owned definition.","root_cause":"The prune guard requires at least one visited file, conflating a successful empty scan with a missing, partial, or failed scan.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-values-patch-linearizability","description":"Two concurrent partial `values` PATCHes can read the same base and let the last full replacement erase the first. A PATCH racing DELETE can likewise validate stale state and attempt a post-delete transition.","finding_id":"F54","fix":"At restraint rung 2, pass the partial patch into one id-keyed mutation operation. Hold the per-id runtime lock and a database row lock or compare-and-swap across reread, merge, expansion, persistence, and refresh; define DELETE as a non-resurrecting winner when it removes the row. Add barrier tests for disjoint PATCHes, null-removal versus PATCH, and PATCH versus DELETE.","location":"P4 / §4.3 templated PATCH merge and exact-scope mutations","prevention":"For every partial-update API, inject barriers around read and write and test disjoint updates, removal, and deletion races.","principle":"A read-modify-write mutation must serialize the read, merge, validation, persistence, and conflicting deletion as one transition.","root_cause":"The route reads and expands a complete replacement before entering the manager's per-id mutation lock.","section_id":"4.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"template-detach-runtime-hook-preservation","description":"A detached `chrome-devtools` instance loses `chrome_executable_path` dispatch on its next connection even though the plan promises that hard deletion leaves the instance as an unchanged materialized manual server.","finding_id":"F55","fix":"At restraint rung 4, add `mcp_servers.runtime_hook` to the existing schema hop, materialize it during expansion/refresh, read it from the instance row, and retain it on FK detachment. Add a hook-bearing prune-then-reconnect test that proves behavior is unchanged.","location":"P2 / §2.2 read-derived fields; P3 / §3.2 hard-delete detachment","prevention":"Before detaching a provenance row, enumerate every runtime field and prove each survives a delete-then-reconnect transition.","principle":"Converting a managed instance to a manual row must preserve every runtime behavior the plan calls materialized.","root_cause":"`runtime_hook` is derived only through the template join and is never stored on `mcp_servers`, while template deletion sets `template_id` to NULL.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"refresh-disconnect-cancellation-safety","description":"Cancellation while refresh awaits old-connection teardown can release the per-id lock with the old subprocess or transport still alive and no registry reference available for later cleanup.","finding_id":"F56","fix":"At restraint rung 2, retain the exact old object and shield its already-bounded disconnect cleanup; finish or force that bounded teardown before re-raising cancellation. Add a cancellation-injection test immediately after identity-conditional removal.","location":"P4 / §4.1 refresh step 4 and identity-conditional discard","prevention":"Inject cancellation after every registry-pop/await seam and assert resource termination, registry consistency, health, and lock release.","principle":"Cancellation must not orphan a resource after its registry ownership has been removed.","root_cause":"The refresh ordering pops the old connection before awaiting disconnect, and the existing cleanup path re-raises cancellation without preserving a registry owner.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"refresh-disabled-instance","description":"An instance YAML with `enabled: false` can be persisted and then connected by §4.3 reconciliation, because `refresh_server` installs it and unconditionally reaches `create_connection`.","finding_id":"F57","fix":"At restraint rung 2, after swapping the unresolved config and tearing down any old connection, branch on `enabled`. Keep disabled rows disconnected, clear their connection/cache state, unregister lazy connect, and return without resolving secrets or creating a transport. Add direct-refresh and live-sync disabled tests.","location":"P4 / §4.1 refresh ordering; §4.3 live sync reconciliation","prevention":"For each connect-capable transition, test enabled and disabled rows from startup, direct refresh, PATCH, and live sync.","principle":"A disabled server row must never start or retain a transport through refresh.","root_cause":"The ordered refresh path proceeds from config swap to secret resolution and connection creation without an `enabled` branch, while live sync refreshes every affected id including disabled rows.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"new-storage-symbol-target-scope","description":"`insert_server`, `get_server_by_id`, `resolve_server`, and `refresh_template_instances` have no exact Target because they do not exist yet, and the listed exact symbols do not authorize their insertion or the neighboring method removals.","finding_id":"F58","fix":"At restraint rung 2, replace §2.2's mixed exact `mcp_servers.py` entries with one `src/gobby/storage/mcp_servers.py::*` Target whose scope reason names the storage API rewrite, all four new methods, and the removed bundled-canonicalization methods.","location":"P2 / §2.2 `src/gobby/storage/mcp_servers.py` Targets","prevention":"For each planned new or deleted symbol in an existing file, choose one justified file-wide Target before manifest derivation.","principle":"Every changed region in a symbol-bearing existing file must be owned by an exact symbol Target or one justified file-wide Target.","root_cause":"The section lists selected existing methods while adding four new methods and deleting canonicalization methods outside those exact symbol scopes.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"embedding-payload-writer-targets","description":"Updating `_embed_all_tools_admitted` cannot make refresh-route points carry `server_id`: payload construction flows through `store_embedding`, `_store_embedding_admitted`, `embed_tool`, and `_embed_tool_admitted`, none of which §4.2 owns.","finding_id":"F59","fix":"At restraint rung 2, target and thread `server_id` through the existing four-method writer chain and pin the refresh route's payload shape alongside the bulk backfill.","location":"P4 / §§4.2–4.3 semantic embedding Targets and refresh pipeline","prevention":"Run callers from each payload-construction symbol and map bulk, incremental, retry, and refresh writers into Targets and acceptance.","principle":"A payload contract must target every writer, including incremental writers outside the bulk migration path.","repairs":[{"entries":["`src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.store_embedding`","`src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch._store_embedding_admitted`","`src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch.embed_tool`","`src/gobby/mcp_proxy/semantic_search.py::SemanticToolSearch._embed_tool_admitted`"],"kind":"add_targets","section_id":"4.2"},{"items":[{"artifact":"test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_refresh_embeddings_carry_scoped_server_identity`","prose":"Refresh-route embedding writes carry the resolved server id, server name, and owning project id and remain visible to scoped semantic search."}],"kind":"add_acceptance","section_id":"4.3"}],"root_cause":"The plan targets only bulk embedding and search while refresh calls the omitted per-tool writer chain.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"scope-fallback-helper-deletion-target","description":"The daemon-checkout fallback cannot be removed within the expanded task's declared scope because `src/gobby/servers/routes/mcp/endpoints/server.py::_current_project_id` is omitted.","finding_id":"F60","fix":"At restraint rung 2, add the existing helper as an exact deletion Target; Acceptance 4.3.9 already pins the resulting scope behavior.","location":"P4 / §4.3 HTTP endpoint Targets","prevention":"For every prose instruction containing delete/remove/retire, resolve the named existing symbol and add its exact Target.","principle":"Deleting an existing symbol is a code change and requires an exact manifest-backed Target.","repairs":[{"entries":["`src/gobby/servers/routes/mcp/endpoints/server.py::_current_project_id`"],"kind":"add_targets","section_id":"4.3"}],"root_cause":"The body explicitly deletes `_current_project_id`, while the Targets enumerate only route handlers in the same file.","section_id":"4.3","severity":"blocking"}],"reviewer_session":"2f6b805c-edd0-44a5-a6a8-af1527ce45c1","round":4,"verdict":"needs_review"},"session_id":"997e04ce-bdf0-4be9-aa5f-92f307376b63"}
```

**Round 6** `kind: verification`

- entry: human-handoff (review cap reached: adversary round 4 of 4)
- source_round: Round 5 (reviewer_run 583c72d4-d7b1-4550-ab29-a6facac7f0d2, verdict needs_review)
- repairs_applied: F49–F60 — typed repairs F50, F59, F60 through `apply_plan_review_repairs`; prose repairs F49, F51–F58 hand-applied; plus the `consumer-coverage` residue the repaired 4.2 surfaced (`EmbeddingSwitchRunner._project_tool_change` re-embeds `tools` rows and now carries `server_id`)
- validation: `uv run gobby plans validate .gobby/plans/bundled-mcp-templates.md -p .` and `--mode expansion` both clean (5 phases, zero residue)
- resolution_notes: four adversary rounds ran (15 / 14 / 19 / 12 findings, all accepted); no further adversary round launches under the cap. The human coordinator chooses between continued interactive revision and build handoff; handoff runs `derive_plan_handoff_manifest` → `apply_plan_handoff_manifest` → expansion validation → `gobby build .gobby/plans/bundled-mcp-templates.md --planning-seed-state approved --completed-plan-review-rounds 4`.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add the AWS OpenAPI MCP negotiation smoke test
  category: test
  task_type: task
  depends_on: []
  validation_criteria: "1.1.1: A skip-by-default integration test launches the pinned\
    \ OpenAPI server over stdio and completes initialize \u2192 list_tools \u2192\
    \ call_tool through gobby's real `StdioTransportConnection`. test: `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::test_openapi_server_negotiates_and_serves_tools`.\n\
    1.1.2: The petstore fixture is self-contained OpenAPI 3.0.x with no external references.\
    \ file: `tests/fixtures/openapi/petstore.json`.\n1.1.3: Cleanup terminates the\
    \ `uvx` child and joins the fixture HTTP thread on the success path and on a forced\
    \ failure after connect; the test asserts no orphaned process or thread from both\
    \ paths. test: `tests/mcp_proxy/integration/test_openapi_mcp_negotiation.py::test_openapi_server_disconnect_kills_child`."
  labels:
  - covers:bundled-mcp-templates:1.1:1.1.1
  - covers:bundled-mcp-templates:1.1:1.1.2
  - covers:bundled-mcp-templates:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  assigned_agent: backend-developer
- title: Add the migration hop for template registry, instance provenance, and project-scoped
    secrets
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '2.1.1: The hop creates `mcp_server_templates`, adds `mcp_servers.template_id`/`template_values`/`runtime_hook`,
    and scopes `secrets` by `(name, project_id)` with the global sentinel default.
    file: `crates/gcore/assets/schema/migrations/411_mcp_templates_project_secrets.sql`.

    2.1.2: The hop is registered as an `EmbeddedMigration` and every identity carrier
    (catalog manifest, expected identity JSON, schema/cli contract tests, grant identity
    and golden vectors) reflects the new head. file: `crates/gcore/src/schema/assets.rs`.

    2.1.3: `cargo nextest run -p gobby-core --features postgres -E ''test(schema)''`
    and `cargo nextest run -p gobby-daemon -E ''test(cli_contract)''` pass against
    the applied hop. behavior: "schema identity tracks catalog head" in `crates/gcore/tests/schema_contract.rs`.'
  labels:
  - covers:bundled-mcp-templates:2.1:2.1.1
  - covers:bundled-mcp-templates:2.1:2.1.2
  - covers:bundled-mcp-templates:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Add template registry storage and instance provenance to MCP storage
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: `MCPTemplateStorageMixin` persists templates keyed
    by `(name, project_id)`, refreshes Gobby-owned rows on hash drift while preserving
    `enabled`, and resolves project rows before global rows. symbol: `MCPTemplateStorageMixin.get_template`.

    2.2.2: `mcp_servers` rows carry `template_id`/`template_values` through model,
    persist, upsert, and update paths, and every bundled-name canonicalisation path
    is deleted. symbol: `MCPServerStorageMixin.upsert`.

    2.2.3: `refresh_template_instances` re-expands template-owned fields and never
    rewrites `enabled`, `description`, or `template_values`. test: `tests/storage/test_storage_mcp.py::test_refresh_template_instances_preserves_instance_fields`.

    2.2.4: Tool cache reads and writes are keyed by server id. symbol: `MCPToolStorageMixin.cache_tools`.

    2.2.5: `refresh_template_instances` leaves an instance whose expansion fails untouched,
    refreshes every other instance, and reports the failure keyed by server id with
    name and scope and no secret value. test: `tests/storage/test_storage_mcp.py::test_refresh_template_instances_isolates_expansion_failures`.

    2.2.6: Server reads join the template row so `MCPServer.template` rehydrates from
    `template_id` after a restart, `runtime_hook` is read from the instance row, and
    an instance of a project override template that declares no hook materialises
    `runtime_hook` as `None`. test: `tests/storage/test_storage_mcp.py::test_server_reads_rehydrate_template_name_and_runtime_hook`.

    2.2.7: `MCPServerConfig` carries `id`, `template_id`, `template`, `runtime_hook`,
    and `template_values` with defaults, and `validate()` rejects an empty id. symbol:
    `MCPServerConfig.validate`.

    2.2.8: `insert_server` on an existing `(name, project_id)` row returns `None`
    and writes neither a row nor a secret slot: after a winner with credential `A`
    and a loser with credential `B` on the same key, the managed secret still decrypts
    to `A`. test: `tests/storage/test_storage_mcp.py::test_insert_server_conflict_writes_no_secret_slot`.'
  labels:
  - covers:bundled-mcp-templates:2.2:2.2.1
  - covers:bundled-mcp-templates:2.2:2.2.2
  - covers:bundled-mcp-templates:2.2:2.2.3
  - covers:bundled-mcp-templates:2.2:2.2.4
  - covers:bundled-mcp-templates:2.2:2.2.5
  - covers:bundled-mcp-templates:2.2:2.2.6
  - covers:bundled-mcp-templates:2.2:2.2.7
  - covers:bundled-mcp-templates:2.2:2.2.8
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Scope the secret store by project
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.3.1: Secrets are stored and looked up by `(name, project_id)`;
    project-scoped reads fall back to the global row and existing callers with no
    scope keep global behaviour. symbol: `SecretStore.get`.

    2.3.2: Managed MCP secrets created by `protect_mcp_mapping` are written in the
    owning instance''s scope and cleaned up in that scope. symbol: `protect_mcp_mapping`.

    2.3.3: `resolve_secrets_in_config` resolves `$secret:` references with the server
    config''s project scope. test: `tests/mcp_proxy/test_manager_coverage.py::test_resolve_secrets_uses_config_project_scope`.

    2.3.4: `gobby secrets set/get/list/delete` accept `--global`/`--project`, default
    to the current registered project, and print the scope. test: `tests/cli/test_cli_secrets.py::test_set_secret_defaults_to_current_project_scope`.

    2.3.5: An unresolved `$secret:` reference raises an `MCPError` that names secret
    names only, and no strip-and-proceed path remains. test: `tests/mcp_proxy/test_manager_coverage.py::test_resolve_secrets_in_config_fails_closed_naming_secret_names`.

    2.3.6: Deleting a project-scoped secret removes only that row and reveals the
    same-named global fallback, while deleting global never removes another project''s
    row. test: `tests/storage/test_secrets_store.py::test_delete_is_exact_scope_and_reveals_global_fallback`.

    2.3.7: Every direct SQL read of `secrets` outside `SecretStore` selects an explicit
    scope: managed-secret cleanup reads its instance scope and remote preflight reads
    the global row only, and a same-named row in another project is untouched by both.
    test: `tests/storage/test_revisioned_config_store.py::test_mcp_cleanup_ignores_same_named_secret_in_other_project`.'
  labels:
  - covers:bundled-mcp-templates:2.3:2.3.1
  - covers:bundled-mcp-templates:2.3:2.3.2
  - covers:bundled-mcp-templates:2.3:2.3.3
  - covers:bundled-mcp-templates:2.3:2.3.4
  - covers:bundled-mcp-templates:2.3:2.3.5
  - covers:bundled-mcp-templates:2.3:2.3.6
  - covers:bundled-mcp-templates:2.3:2.3.7
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Add the MCP server template model, loader, expansion, and bundled template
    YAMLs
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '1.1'
  validation_criteria: '3.1.1: Seven bundled templates load and validate, and `openapi`
    declares the pinned command, `--log-level ERROR`, `connect_timeout: 120`, and
    the parameter contract in Constraints. file: `src/gobby/install/shared/mcp/templates/openapi.yaml`.

    3.1.2: `expand_template` materialises env/args, rejects unknown params, raw secret
    values, missing required/conditional params, and reports missing secrets by name.
    symbol: `expand_template`.

    3.1.3: `bundled.py` exposes only template-keyed runtime hooks; every bundled-name
    constant and canonicalisation helper is deleted. file: `src/gobby/mcp_proxy/bundled.py`.

    3.1.4: The `mcp/` tree is a protected, manifest-covered bundled content type.
    test: `tests/sync/test_integrity.py::test_every_synced_content_type_maps_a_protected_path`.

    3.1.5: Secret parameters normalize to $secret references in template_values and
    no supplied credential value is retained. test: `tests/mcp_proxy/test_templates.py::test_expand_template_normalizes_secret_references`.

    3.1.6: Stdio runtime hooks dispatch on `config.runtime_hook`: a same-named instance
    whose template declares no hook receives no injection, and every `npx` command
    prefers offline. test: `tests/mcp_proxy/transports/test_stdio_transport.py::test_runtime_hook_dispatches_on_config_not_name`.

    3.1.7: `expand_template` materialises a required secret param as its `$secret:`
    reference when the secret is absent and omits an absent optional secret param
    from `env`/`args` while listing it in `optional_missing_secrets`. test: `tests/mcp_proxy/test_templates.py::test_expand_template_required_and_optional_missing_secrets`.

    3.1.8: All six converted legacy templates preserve their exact command, arguments,
    secret mappings, optional behavior, and runtime hook contracts. test: `tests/mcp_proxy/test_templates.py::test_bundled_template_definitions_match_legacy_contracts`.

    3.1.9: Template YAML `enabled` loads into `MCPServerTemplate.enabled`, defaults
    to true, and round-trips through `to_definition`/`from_definition`. test: `tests/mcp_proxy/test_templates.py::test_template_enabled_defaults_true_and_round_trips`.

    3.1.10: A credential-shaped bare value (`ghp_abc123`) naming no stored secret
    is rejected without being echoed, a bare name of an existing secret normalises
    to its `$secret:` reference, and `$secret:<name>` is accepted before the secret
    exists. test: `tests/mcp_proxy/test_templates.py::test_secret_params_require_reference_or_existing_name`.

    3.1.11: The parent shared-content contract documents `.gobby/mcp/templates/` and
    `.gobby/mcp/servers/` as the MCP-specific project/global override roots and states
    their override semantics. file: `src/gobby/install/shared/AGENTS.md`.'
  labels:
  - covers:bundled-mcp-templates:3.1:3.1.1
  - covers:bundled-mcp-templates:3.1:3.1.2
  - covers:bundled-mcp-templates:3.1:3.1.3
  - covers:bundled-mcp-templates:3.1:3.1.4
  - covers:bundled-mcp-templates:3.1:3.1.5
  - covers:bundled-mcp-templates:3.1:3.1.6
  - covers:bundled-mcp-templates:3.1:3.1.7
  - covers:bundled-mcp-templates:3.1:3.1.8
  - covers:bundled-mcp-templates:3.1:3.1.9
  - covers:bundled-mcp-templates:3.1:3.1.10
  - covers:bundled-mcp-templates:3.1:3.1.11
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Add template and instance sync targets for bundled, global, and project YAML
  category: code
  task_type: feature
  depends_on:
  - '3.1'
  - '2.3'
  validation_criteria: '3.2.1: Bundled, global, and project template YAML sync to
    `mcp_server_templates` rows with the owner/scope rules and the `override: true`
    collision guard. symbol: `sync_bundled_mcp_templates`.

    3.2.2: Instance YAML under `.gobby/mcp/servers/` and `~/.gobby/mcp/servers/` becomes
    project- or global-scoped `mcp_servers` rows with provenance, an `affected_ids`
    list of the created or updated row ids, and a `needs_configuration` report naming
    missing secrets. symbol: `sync_mcp_server_files`.

    3.2.3: The first sync adopts a pre-existing global row for a bundled name only
    when its expanded template config matches the row exactly, writes `template_id`/`template_values`
    in one update, changes no runtime field, and skips customised rows (extra env,
    extra args, differing secret references) with an `adoption_skipped` reason. test:
    `tests/mcp_proxy/test_sync_templates.py::test_sync_adopts_only_exact_legacy_bundled_rows`.

    3.2.4: Daemon start refreshes template instances instead of normalising bundled
    servers, from the new `mcp_stack` module. test: `tests/runner_init/test_services_mcp_stack.py::test_init_mcp_stack_refreshes_template_instances`.

    3.2.5: MCP stack initialisation lives in `src/gobby/runner_init/mcp_stack.py`
    and `services.py` shrinks. file: `src/gobby/runner_init/mcp_stack.py`.

    3.2.6: Daemon start logs each template-instance expansion failure by name and
    scope with its fix command and still completes MCP stack initialisation. test:
    `tests/runner_init/test_services_mcp_stack.py::test_init_mcp_stack_reports_stale_instance_without_failing`.

    3.2.7: Removing an instance YAML file leaves its persisted row enabled and unchanged
    until explicit removal. test: `tests/mcp_proxy/test_sync_servers.py::test_removed_instance_file_does_not_delete_row`.

    3.2.8: Instance YAML naming a disabled template is recorded as a `template_disabled`
    error naming the template and scope, and no row is written. test: `tests/mcp_proxy/test_sync_servers.py::test_disabled_template_blocks_instance_sync`.

    3.2.9: An instance file whose stem, `name`, and `template` all differ syncs a
    row named by `name` expanded from the template named by `template`. test: `tests/mcp_proxy/test_sync_servers.py::test_instance_name_and_template_name_are_independent`.

    3.2.10: Removing a template YAML file prunes its row on the next authoritative
    error-free scan and detaches its instances as manual rows, a removed global user
    override is replaced by the bundled definition in the same `gobby sync` run, and
    an erroring scan prunes nothing. test: `tests/mcp_proxy/test_sync_templates.py::test_removed_template_file_prunes_row_and_restores_bundled_definition`.

    3.2.11: Deleting the last template file in an existing project or global root
    prunes that scope''s rows on the next scan, a scope whose root directory does
    not exist is never pruned, and an unreadable root prunes nothing across the scan.
    test: `tests/mcp_proxy/test_sync_templates.py::test_last_template_deletion_prunes_and_missing_root_does_not`.

    3.2.12: An instance detached by template pruning keeps its materialised `runtime_hook`
    and dispatches the same stdio hook on its next connection. test: `tests/mcp_proxy/test_sync_templates.py::test_detached_instance_keeps_runtime_hook_after_reconnect`.'
  labels:
  - covers:bundled-mcp-templates:3.2:3.2.1
  - covers:bundled-mcp-templates:3.2:3.2.2
  - covers:bundled-mcp-templates:3.2:3.2.3
  - covers:bundled-mcp-templates:3.2:3.2.4
  - covers:bundled-mcp-templates:3.2:3.2.5
  - covers:bundled-mcp-templates:3.2:3.2.6
  - covers:bundled-mcp-templates:3.2:3.2.7
  - covers:bundled-mcp-templates:3.2:3.2.8
  - covers:bundled-mcp-templates:3.2:3.2.9
  - covers:bundled-mcp-templates:3.2:3.2.10
  - covers:bundled-mcp-templates:3.2:3.2.11
  - covers:bundled-mcp-templates:3.2:3.2.12
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Key the client manager, connections, health, and tool cache by server id
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '2.3'
  - '3.1'
  validation_criteria: '4.1.1: Configs loaded at startup and rows persisted by `add_server`
    carry the DB row id as `MCPServerConfig.id`, and every manager map is keyed by
    that id. symbol: `load_initial_configs`.

    4.1.2: Manager configs, connections, schema cache, health, and lazy-connector
    state are keyed by server id, and two configs sharing a name in different projects
    coexist as independent connections. test: `tests/mcp_proxy/test_mcp_manager.py::test_same_name_in_two_projects_are_independent_servers`.

    4.1.3: No name-based lookup remains in `manager.py` or `client_manager/`; every
    facade method and registry helper takes a server id, and the project-scoped `get_available_servers(project_id=)`
    / `list_tools(project_id=)` keep their name-keyed inventory shapes for the workflow
    consumers. behavior: "`gcode grep -F ''find_config_ids'' -g ''src/gobby/mcp_proxy/client_manager/**''
    -g ''src/gobby/mcp_proxy/manager.py''` returns no hits" in `src/gobby/mcp_proxy/manager.py`.

    4.1.4: Discovered tools persist and load by server id. symbol: `cache_discovered_tools`.

    4.1.5: `refresh_server(server_id)` rebuilds only the selected instance''s config
    with current scoped secrets, invalidates its caches, and reconnects it; with two
    same-named instances in different projects, rotating one project''s secret changes
    only that instance''s next call. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_rotates_secret_for_selected_instance_only`.

    4.1.6: `refresh_server`, `reconnect`, `remove_server`, `update_server`, and `set_server_enabled`
    share the per-id connection lock, stale-session discard pops a connection only
    when it is the one the failed call used, and under a barrier-controlled concurrent
    `call_tool` every call that starts after `refresh_server` returns uses the new
    secret while an in-flight call either completes on the old connection or retries
    on the new one; a concurrently deleted row yields the unknown-server error. test:
    `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_is_linearizable_against_concurrent_calls`.

    4.1.7: An instance whose required secret is missing at startup load, lazy connect,
    or refresh never starts a transport, reports `needs_configuration` with secret
    names only, and connects after the secret is set and the instance refreshed. test:
    `tests/mcp_proxy/test_mcp_manager.py::test_missing_required_secret_fails_closed_on_every_connection_path`.

    4.1.8: `get_available_servers(project_id=)` and `list_tools(project_id=)` return
    only the caller-visible set, so two same-named instances with different tool schemas
    in two projects never appear in each other''s workflow inventory. test: `tests/workflows/test_dry_run_tool_gates.py::test_workflow_inventory_is_scoped_to_workflow_project`.

    4.1.9: Template-owned configs re-expand before startup load, lazy connect, and
    refresh so an optional secret''s appearance or deletion materializes or removes
    its env/arg without entering needs_configuration. test: `tests/mcp_proxy/test_mcp_manager.py::test_optional_secret_reexpands_on_all_connection_paths`.

    4.1.10: `_configs` retains `$secret:` references after startup load, lazy connect,
    and refresh while only the connection object receives resolved values. test: `tests/mcp_proxy/test_mcp_manager.py::test_registry_config_keeps_secret_references_after_refresh`.

    4.1.11: A refresh whose template re-expansion fails leaves the live connection
    and registry state untouched, reports `stale_template` health naming parameters
    only, and raises; startup load registers the same instance as stale without blocking
    the others. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_keeps_last_known_good_on_expansion_error`.

    4.1.12: Refreshing an instance whose required secret was deleted disconnects and
    discards the old transport before reporting `needs_configuration`, and a later
    `call_tool` cannot reuse it. test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_with_deleted_secret_disconnects_old_transport`.

    4.1.13: Cancelling `refresh_server` while it awaits the old connection''s disconnect
    still completes the bounded teardown before the cancellation propagates, leaving
    no live transport outside the registry and the per-id lock released. test: `tests/mcp_proxy/test_manager_disconnect_cancellation.py::test_refresh_cancelled_during_old_disconnect_finishes_teardown`.

    4.1.14: `refresh_server` on a disabled instance, reached directly or through the
    live-sync refresh route, installs the config, clears connection and cache state,
    reports `disabled` health, and never resolves secrets or creates a transport.
    test: `tests/mcp_proxy/test_mcp_manager.py::test_refresh_server_never_connects_disabled_instance`.'
  labels:
  - covers:bundled-mcp-templates:4.1:4.1.1
  - covers:bundled-mcp-templates:4.1:4.1.2
  - covers:bundled-mcp-templates:4.1:4.1.3
  - covers:bundled-mcp-templates:4.1:4.1.4
  - covers:bundled-mcp-templates:4.1:4.1.5
  - covers:bundled-mcp-templates:4.1:4.1.6
  - covers:bundled-mcp-templates:4.1:4.1.7
  - covers:bundled-mcp-templates:4.1:4.1.8
  - covers:bundled-mcp-templates:4.1:4.1.9
  - covers:bundled-mcp-templates:4.1:4.1.10
  - covers:bundled-mcp-templates:4.1:4.1.11
  - covers:bundled-mcp-templates:4.1:4.1.12
  - covers:bundled-mcp-templates:4.1:4.1.13
  - covers:bundled-mcp-templates:4.1:4.1.14
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Resolve server names by project scope in the proxy front door
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  - '3.1'
  - '2.3'
  - '3.2'
  validation_criteria: '4.2.1: `call_tool`, `list_tools`, `get_tool_schema`, and `read_resource`
    resolve an external server name within the caller''s project first and the global
    scope second, and never reach another project''s instance. test: `tests/mcp_proxy/services/test_tool_proxy_coverage.py::test_call_tool_resolves_project_instance_before_global`.

    4.2.2: `add_mcp_server` with `template`/`values` persists an expanded instance
    in the requested scope and returns `missing_secrets` plus the exact configure
    commands without connecting. symbol: `ServerManagementService.add_server`.

    4.2.3: `list_mcp_servers` reports only the caller''s visible instances with `scope`
    and `template`, plus the available templates and their params. symbol: `GobbyDaemonTools.list_mcp_servers`.

    4.2.4: Tool embeddings carry the server id and scope and semantic search filters
    to visible servers. test: `tests/mcp_proxy/test_semantic_search.py::test_search_tools_filters_to_visible_servers`.

    4.2.5: The scope-resolution matrix (enabled project row, global fallback, disabled
    project shadow, foreign-project id, project-row removal) passes for `call_tool`,
    `list_tools`, and `get_tool_schema`. test: `tests/mcp_proxy/services/test_scope_resolution_matrix.py::test_scope_resolution_matrix`.

    4.2.6: Every non-HTTP direct manager consumer (task delivery, GitHub triage, GitHub/Linear
    sync, integrations, websocket tool calls) resolves its server through `resolve_server`
    with its own project scope and passes only the resolved id across the manager
    boundary; with a same-named project and global instance the project instance is
    used. test: `tests/mcp_proxy/services/test_scope_resolution_consumers.py::test_consumers_resolve_project_instance_by_id`.

    4.2.7: Adding from a disabled project or global template returns template_disabled
    and persists no mcp_servers row. test: `tests/mcp_proxy/test_server_mgmt.py::test_add_disabled_template_returns_template_disabled_without_persisting`.

    4.2.8: Two concurrent `add_server` calls for the same name and scope produce one
    persisted row, one manager registration, and one duplicate envelope, with the
    winning row and its managed secret rows unmodified even when the loser carries
    distinct credential values and keys. test: `tests/mcp_proxy/test_server_mgmt.py::test_concurrent_add_same_name_and_scope_has_one_winner`.

    4.2.9: The first daemon start after the change rewrites every legacy external
    tool point with `server_id`, `server_name`, and owning `project_id`, removes the
    superseded legacy points, records the backfill version, and a second start embeds
    nothing. test: `tests/mcp_proxy/test_semantic_search.py::test_scoped_payload_backfill_rewrites_legacy_points_once`.

    4.2.10: `resolve_request_scope` is total over its explicit inputs: the all-empty
    tuple returns `fallback_project_id`, an explicit project is refused when `project_exists`
    is false, and the MCP front door and the HTTP routes drive the same function with
    only their fallback differing. test: `tests/mcp_proxy/services/test_scope_resolution_matrix.py::test_resolve_request_scope_is_total_over_explicit_inputs`.'
  labels:
  - covers:bundled-mcp-templates:4.2:4.2.1
  - covers:bundled-mcp-templates:4.2:4.2.2
  - covers:bundled-mcp-templates:4.2:4.2.3
  - covers:bundled-mcp-templates:4.2:4.2.4
  - covers:bundled-mcp-templates:4.2:4.2.5
  - covers:bundled-mcp-templates:4.2:4.2.6
  - covers:bundled-mcp-templates:4.2:4.2.7
  - covers:bundled-mcp-templates:4.2:4.2.8
  - covers:bundled-mcp-templates:4.2:4.2.9
  - covers:bundled-mcp-templates:4.2:4.2.10
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Expose templates and scope through HTTP routes, the CLI, and the stdio proxy
  category: code
  task_type: feature
  depends_on:
  - '4.2'
  - '3.2'
  validation_criteria: "4.3.1: `POST /api/mcp/servers` accepts `template`/`values`/`scope`,\
    \ keeps the manual payload the web MCP tab sends working, and `GET /api/mcp/servers`\
    \ lists the caller's visible instances with `scope`, `template`, and `missing_secrets`.\
    \ symbol: `add_mcp_server`.\n4.3.2: `GET /api/mcp/templates` lists templates visible\
    \ to the resolved project with their parameter contracts. file: `src/gobby/servers/routes/mcp/endpoints/templates.py`.\n\
    4.3.3: `gobby mcp-proxy add-server --template \u2026 --set \u2026 [--global]`\
    \ instantiates, prompts for missing secrets interactively, and prints configure\
    \ commands non-interactively; `list-templates`/`show-template` exist. test: `tests/cli/test_cli_mcp_proxy.py::test_add_server_from_template_prompts_for_missing_secrets`.\n\
    4.3.4: The stdio proxy and `add_mcp_server`/`remove_mcp_server`/`list_mcp_servers`\
    \ MCP tools carry `template`, `values`, and `scope` end to end. test: `tests/mcp_proxy/test_stdio_proxy.py::test_add_mcp_server_forwards_template_fields`.\n\
    4.3.5: `docs/guides/http-endpoints.md` documents the scoped `/api/mcp/servers`\
    \ contract and `/api/mcp/templates`. file: `docs/guides/http-endpoints.md`.\n\
    4.3.6: HTTP execution routes resolve `server_name` and `server_id` through the\
    \ shared scope resolver and pass the 4.2 scope matrix, and `POST /api/mcp/refresh`\
    \ rotates only the resolved instance via `refresh_server`. test: `tests/servers/routes/mcp_endpoints/test_scope_resolution_matrix.py::test_http_scope_resolution_matrix`.\n\
    4.3.7: HTTP import resolves project and global scope through the shared management\
    \ path. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_import_mcp_server_respects_project_and_global_scope`.\n\
    4.3.8: `PATCH /api/mcp/servers/{name}` preserves the resolved row's id, scope,\
    \ and template provenance, rejects template-owned runtime-field edits on a templated\
    \ instance with `400 template_owned_fields`, re-expands `values` through the template,\
    \ and a following startup refresh changes nothing. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_update_mcp_server_preserves_identity_and_rejects_template_owned_fields`.\n\
    4.3.9: Scope resolution is the single `resolve_request_scope` case table: explicit\
    \ `scope: \"global\"` wins from any caller including a session-bound one, a session-bound\
    \ request otherwise uses its effective project, the CLI sends the registered project\
    \ of its cwd, the stdio proxy sends its effective project, an unregistered or\
    \ missing project under `scope: \"project\"` is refused with `400 project_scope_unresolved`,\
    \ the unchanged sessionless web-tab payload (`project_id: \"\"`, no `scope`) lands\
    \ in the global scope, and a request from a second project never touches the daemon\
    \ checkout's project. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_project_scope_precedence_and_web_legacy_payload`.\n\
    4.3.10: Instance YAML synced while the daemon runs is reconciled into the live\
    \ manager through `POST /api/mcp/refresh` carrying each affected row's id and\
    \ its own `project_id` or `scope: \"global\"`, and becomes callable without a\
    \ restart; with no daemon reachable the sync still succeeds and says the step\
    \ was skipped. test: `tests/workflows/test_user_template_sync.py::test_synced_instance_is_reconciled_into_live_manager`.\n\
    4.3.11: `POST /api/mcp/refresh` on a resolved instance re-hashes changed tool\
    \ schemas, removes stale hashes and embeddings for tools the server no longer\
    \ serves, regenerates embeddings for new and changed tools, honours `force`, and\
    \ reports per-instance statistics carrying name and scope. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_refresh_preserves_schema_hash_and_embedding_pipeline`.\n\
    4.3.12: The source-control GitHub routes resolve the `github` instance for the\
    \ request's project and dispatch by id from `source_control_github.py`. test:\
    \ `tests/servers/routes/test_source_control_routes.py::test_github_routes_resolve_project_instance`.\n\
    4.3.13: HTTP, CLI, stdio, and MCP template-instantiation adapters preserve the\
    \ shared template_disabled result and create no instance. behavior: \"disabled-template\
    \ instantiation parity across adapters\" in `tests/servers/routes/mcp_endpoints/test_template_routes.py`.\n\
    4.3.14: Project-scoped PATCH, DELETE, and enable requests for a name that exists\
    \ only in the global scope return unknown-server and leave the global row untouched;\
    \ the same requests with `scope: \"global\"` mutate it. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_project_scoped_mutations_never_fall_back_to_global`.\n\
    4.3.15: A templated PATCH merges `values` over the stored `template_values`, keeps\
    \ absent keys, removes `null` keys, rejects an invalid merge with `400 template_values_invalid`\
    \ and no write, and persists the normalised map with the materialised fields atomically.\
    \ test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_update_mcp_server_merges_values_and_null_removes_parameter`.\n\
    4.3.16: Refresh-route embedding writes carry the resolved server id, server name,\
    \ and owning project id and remain visible to scoped semantic search. test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_refresh_embeddings_carry_scoped_server_identity`.\n\
    4.3.17: Under barrier control, two disjoint concurrent `values` PATCHes both survive,\
    \ a `null`-removal PATCH racing another PATCH serialises, and a PATCH racing `DELETE`\
    \ either applies before the delete or returns `404` without re-creating the row.\
    \ test: `tests/servers/routes/mcp_endpoints/test_registry_routes.py::test_concurrent_patches_and_delete_serialize_under_per_id_lock`."
  labels:
  - covers:bundled-mcp-templates:4.3:4.3.1
  - covers:bundled-mcp-templates:4.3:4.3.2
  - covers:bundled-mcp-templates:4.3:4.3.3
  - covers:bundled-mcp-templates:4.3:4.3.4
  - covers:bundled-mcp-templates:4.3:4.3.5
  - covers:bundled-mcp-templates:4.3:4.3.6
  - covers:bundled-mcp-templates:4.3:4.3.7
  - covers:bundled-mcp-templates:4.3:4.3.8
  - covers:bundled-mcp-templates:4.3:4.3.9
  - covers:bundled-mcp-templates:4.3:4.3.10
  - covers:bundled-mcp-templates:4.3:4.3.11
  - covers:bundled-mcp-templates:4.3:4.3.12
  - covers:bundled-mcp-templates:4.3:4.3.13
  - covers:bundled-mcp-templates:4.3:4.3.14
  - covers:bundled-mcp-templates:4.3:4.3.15
  - covers:bundled-mcp-templates:4.3:4.3.16
  - covers:bundled-mcp-templates:4.3:4.3.17
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Retire the legacy bundled-install path and machine-local MCP registry
  category: code
  task_type: chore
  depends_on:
  - '3.2'
  - '4.3'
  validation_criteria: '5.1.1: `install_default_mcp_servers`, `MCPConfigManager`,
    `import_from_mcp_json`, `_API_KEY_PROMPTS`, and `.mcp-template.json` are deleted
    and no production import references them. behavior: "`gcode grep -F ''install_default_mcp_servers''
    -g ''src/**''` returns no hits" in `src/gobby/cli/install_setup.py`.

    5.1.2: `gobby install` instantiates no MCP servers and prints the bundled template
    list with the add-server command. test: `tests/cli/test_install_setup.py::test_daemon_setup_lists_mcp_templates_without_installing`.

    5.1.3: `docs/guides/configuration.md` describes templates, instance YAML, scopes,
    and project-scoped secrets and no longer references `~/.gobby/mcp-servers.json`
    as live config. file: `docs/guides/configuration.md`.

    5.1.4: When the retired mcp-servers.json file exists, install emits the exact
    retirement warning and neither reads nor modifies the file. test: `tests/cli/test_install_setup.py::test_daemon_setup_warns_about_retired_mcp_servers_file`.'
  labels:
  - covers:bundled-mcp-templates:5.1:5.1.1
  - covers:bundled-mcp-templates:5.1:5.1.2
  - covers:bundled-mcp-templates:5.1:5.1.3
  - covers:bundled-mcp-templates:5.1:5.1.4
  tdd: true
  source_section: '5.1'
  implementation_domain: backend
- title: Add the mcp-servers skill and rewrite the MCP tool docs
  category: docs
  task_type: task
  depends_on:
  - '4.3'
  - '5.1'
  validation_criteria: '5.2.1: A bundled `mcp-servers` skill teaches template discovery,
    instantiation, secret configuration, declarative instance YAML, and the OpenAPI
    caveats. file: `src/gobby/install/shared/skills/mcp-servers/SKILL.md`.

    5.2.2: `docs/guides/mcp-tools.md` and `browser-testing/SKILL.md` describe external
    servers as template instances with scope. file: `docs/guides/mcp-tools.md`.

    5.2.3: The regenerated bundled content manifest contains the new mcp-servers skill
    and passes its integrity check. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.'
  labels:
  - covers:bundled-mcp-templates:5.2:5.2.1
  - covers:bundled-mcp-templates:5.2:5.2.2
  - covers:bundled-mcp-templates:5.2:5.2.3
  tdd: false
  source_section: '5.2'
  assigned_agent: tech-writer
```
