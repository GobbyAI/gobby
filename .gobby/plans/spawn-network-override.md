Plan artifact: `.gobby/plans/spawn-network-override.md`

# Per-spawn SRT network profile

**Plan ID:** spawn-network-override

## Context
`kind: framing`

An authorized caller may choose `network: none|trusted` for one managed agent launch. An
omitted or null value inherits the resolved agent definition's profile, including a
fallback definition. `none` uses the existing restricted SRT allowlist; `trusted` adds
the vendored Trusted domains, Git access, and package registries. Both remain
allowlist-based SRT policies. Placement in a tab or pane does not change the choice.
Checkout `isolation: none|worktree|clone` keeps its present meaning.

The work starts after reboot. The installed `merge-orchestrator` definition was still
enabled when this draft was written; retire batch spawning only after its separate
removal is installed and confirmed in the DB registry. Do not migrate its orchestration.
The checkout-field rename and removal of `herdr` code references belong to follow-up
task #23437, **Rename checkout isolation to checkout_mode and remove herdr code references**.

## P1: Launch configuration
`kind: framing`

### 1.1 Resolve and persist the effective SRT profile [category: code]
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_network.py::*` — scope-reason: resolve inherited and explicit profiles in the existing sandbox policy function
- `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_gate.py::*` — scope-reason: thread the choice through the existing SRT gate
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::*` — scope-reason: validate and pass the launch choice before allocating resources
- `src/gobby/mcp_proxy/tools/spawn_agent/_network_preflight.py`
- `tests/agents/test_sandbox_network.py::*` — scope-reason: cover profile resolution, write grants, and Trusted-seed failure
- `tests/agents/test_resume_sandbox_gate.py::*` — scope-reason: cover persisted effective policy on resume

Move the network and sandbox preflight from
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` into the new
`src/gobby/mcp_proxy/tools/spawn_agent/_network_preflight.py`; the current
implementation file has 912 lines and must stay below the production ceiling.
Accept only `none`, `trusted`, or null at the core boundary. Resolve null after
agent fallback chooses the final definition. Apply the explicit profile with the
existing write-path grant, require SRT, and build the effective config before
placement, checkout, session, or run allocation. Persist that effective config in
the existing resume metadata path so resume does not recalculate a different
policy. An unreadable Trusted seed refuses a trusted launch before allocation.

**Research context:** `definition_sandbox_config` already adds the Trusted seed,
Git, and registry groups while keeping `allow_network=false`; `resolve_spawn_sandbox`
combines that config with `apply_write_grant` and `require_managed_srt`.
`spawn_agent_impl` calls this gate before `preflight_placement` and checkout
creation, then passes the config to `build_spawn_context` for resume metadata.
Reuse those paths. Observed: the installed profile is definition-only and the
current implementation has 912 lines. Planned: focused sandbox and resume tests,
including placed and unplaced launches, then format, lint, and type checks.

**Acceptance:**

- 1.1.1 - Null inherits the final definition's profile; explicit `none` and `trusted` take precedence for one launch. behavior: "effective per-spawn SRT network profile".
- 1.1.2 - Both profiles require SRT and preserve the write grant; Trusted-seed failure allocates no launch resources. behavior: "SRT profile and write-grant preflight".
- 1.1.3 - Resume uses the saved effective sandbox config. behavior: "resume retains selected network profile".

### 1.2 Expose the choice on single-spawn surfaces (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: add the MCP single-spawn parameter and forward it after fallback resolution
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: validate and forward the HTTP single-spawn field
- `src/gobby/cli/agents.py::*` — scope-reason: parse and forward the CLI flag
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: test MCP parsing and fallback forwarding
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: test HTTP parsing, web-chat rejection, and credentials
- `tests/cli/test_cli_agents.py::*` — scope-reason: test CLI flag parsing and forwarding

Add optional `network: Literal["none", "trusted"] | None` to MCP `spawn_agent`
and the single-agent HTTP request, and `--network none|trusted` to `gobby agents
spawn`. Forward the value without replacing null with the first definition's
default: MCP provider fallback may choose another definition. Reject an explicit
network value for HTTP `web_chat`, which does not launch an SRT agent. Keep direct
HTTP spawn restricted to operator credentials; managed agent credentials remain
refused. The CLI sends the flag only when present.

**Research context:** MCP `create_spawn_agent_registry.spawn_agent` chooses its
fallback body before calling `spawn_agent_impl`; HTTP `_do_spawn` calls the same
core function. CLI `spawn_agent_cmd` posts to the MCP single-spawn endpoint.
`AgentSpawnRequest` has a `web_chat` branch before managed launch. Observed:
none of these three surfaces has a network parameter. Planned: focused MCP,
HTTP, and Click tests for valid/invalid values, null, fallback, `web_chat`, and
the existing HTTP credential boundary.

**Acceptance:**

- 1.2.1 - MCP, HTTP, and CLI expose only `none|trusted`, with omission/null inheriting the final definition. behavior: "single-spawn network input".
- 1.2.2 - HTTP `web_chat` rejects explicit network and managed credentials cannot call the direct HTTP spawn route. behavior: "HTTP spawn boundary".

## P2: Caller authority and definitions
`kind: framing`

### 2.1 Deny unauthorized overrides and prepare definitions (depends: 1.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: verify the caller's session and agent-run identity for override authority
- `src/gobby/install/shared/workflows/rules/tool-hygiene/limit-spawn-network-override.yaml`
- `src/gobby/install/shared/workflows/agents/default.yaml::*` — scope-reason: allow the default spawned definition to spawn children
- `src/gobby/install/shared/workflows/agents/orchestrator.yaml`
- `tests/workflows/test_spawn_scope_rules.py::*` — scope-reason: cover operator, allowed child, denied child, and forged-parent cases
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: verify the MCP boundary with an explicit network value

Add a default-deny `before_tool` rule for explicit MCP `spawn_agent.network`.
The helper resolves the verified `_platform_session_id`, checks the session's
recorded agent run and its `agent_name`, and fails closed on missing or
inconsistent identity. A root operator session may override; spawned callers
named `default` or `orchestrator` may choose either profile. Other spawned
callers may still spawn with no override. Never use caller-supplied
`parent_session_id` to grant authority. Set bundled `default` to
`spawnable_agents: ["*"]`. Add a disabled `orchestrator` definition based on
the current coordination role instructions, with child spawning configured for
future activation. Activation and the existing seat-spawn policy are separate.

**Research context:** `spawn_target_allowed` in
`condition_helpers_sessions.py` resolves a caller through `SessionManager` and
its recorded agent run; `limit-spawnable-agents.yaml` uses the verified platform
session metadata. Reuse that trust boundary. Observed: bundled `default` has no
`spawnable_agents`; the installed `merge-orchestrator` is still enabled. Planned:
isolated rule tests covering root, `default`, `orchestrator`, other spawned
definitions, missing run identity, and forged parent IDs; inspect the installed
rule and live MCP schema after an authorized coordinated restart.

**Acceptance:**

- 2.1.1 - Operator and named spawned callers may select either profile; all other spawned callers may spawn only without an override. behavior: "network override authority".
- 2.1.2 - A forged parent session ID never changes override authority. behavior: "verified caller identity".
- 2.1.3 - Bundled default permits child spawning and bundled orchestrator is disabled with child-spawn configuration. file: `src/gobby/install/shared/workflows/agents/orchestrator.yaml`.

## P3: Batch retirement
`kind: framing`

### 3.1 Remove batch APIs and active references (depends: 1.2, 2.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: remove the batch tool and its batch-only helpers
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: remove the HTTP batch route and request and response models
- `src/gobby/mcp_proxy/services/tool_execution.py::*` — scope-reason: remove the batch parent-session tool classification
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: remove batch target expansion while keeping single-spawn scope checks
- `src/gobby/install/shared/workflows/rules/restraint/require-restraint-skill.yaml::*` — scope-reason: remove the batch tool condition
- `src/gobby/install/shared/workflows/rules/roles/seat-spawn-policy.yaml::*` — scope-reason: remove batch entries from seat restrictions
- `src/gobby/install/shared/workflows/rules/tool-hygiene/limit-spawnable-agents.yaml::*` — scope-reason: keep only single-spawn targeting
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: remove obsolete batch permission entries
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: remove obsolete batch permission entries
- `src/gobby/install/shared/workflows/agents/plan-writer.yaml::*` — scope-reason: remove obsolete batch permission entries
- `src/gobby/install/shared/skills/gobby/references/agents/spawning.md`
- `docs/guides/agents.md`
- `docs/guides/workflows-overview.md`
- `docs/guides/http-endpoints.md`
- `docs/guides/sandboxing.md`
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: assert batch tool absence and retain single-spawn coverage
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: remove batch-only cases
- `tests/mcp_proxy/tools/spawn_agent/test_worktree_reference_resolution.py::*` — scope-reason: remove batch-only cases
- `tests/mcp_proxy/tools/test_parallel_dispatch.py::*` — scope-reason: remove batch-only cases
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: assert HTTP batch route absence
- `tests/workflows/test_developer_guidance_rules.py::*` — scope-reason: remove batch permission cases
- `tests/workflows/test_seat_rules.py::*` — scope-reason: remove batch permission cases
- `tests/workflows/test_spawn_scope_rules.py::*` — scope-reason: retain single-spawn scope tests

First verify the separate merge-orchestrator removal is installed in the DB
registry. Then remove MCP `dispatch_batch` and HTTP `/api/agents/spawn/batch`,
their batch-only helpers/models, references in permissions and rules, and
active guidance and tests. Historical completed plans and upstream attribution
remain historical records. Do not move merge orchestration to another path.

**Granularity:** This section has many target files because one API retirement
must remove its executable surface and every active reference in the same
change; the tests and guidance cannot be left advertising a removed tool.

**Research context:** `create_spawn_agent_registry` defines `dispatch_batch`
beside `spawn_agent`; `create_agent_spawn_router` defines the HTTP batch route.
`PARENT_SESSION_TOOLS`, spawn scope helpers, bundled rules, three planning
definitions, and active guidance name the batch tool. Observed: the
`merge-orchestrator` template and installed definition still exist, so this
section's precondition is unmet at draft time. Planned: confirm installed
removal, run focused route/registry/rule tests, grep active code and guidance
for both retired names, then format, lint, type checks, and a coordinated live
schema check.

**Acceptance:**

- 3.1.1 - The MCP registry has no `dispatch_batch` tool and HTTP batch requests do not route. behavior: "batch spawn APIs absent".
- 3.1.2 - Permissions, rules, active guidance, and tests no longer refer to either retired API. behavior: "active batch references removed".
- 3.1.3 - Installed merge-orchestrator removal is confirmed before retirement, with no replacement merge orchestration. behavior: "batch retirement prerequisite".

## V1: Verification
`kind: verification`

Run `uv run gobby plans validate .gobby/plans/spawn-network-override.md -p
/Users/josh/Projects/gobby` on this draft. Implementation verification uses
focused API, CLI, sandbox, resume, and authorization tests with isolated test
state; repository format, lint, type, test-quality, and test-type checks; and
installed-rule plus live-schema checks after a coordinated restart. Do not run
the full pytest suite. `gclient` ordinary shell tabs and panes remain unchanged.
