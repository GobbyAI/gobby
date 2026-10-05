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

Relationship to #23003, **Research sandbox profile on researcher**: sequenced, not
folded. #23003 is the D2 deferral of `.gobby/plans/agent-definition-profiles.md`;
its spec, corrected 2026-10-05, sets the shipped definition field `network: trusted`
on the bundled `researcher.yaml` after #22998, **Review and observation seats**,
which also targets that file. The field is sync-owned: definition storage refuses a
non-sync write that widens `network`. That change needs nothing from this plan, and
this plan has no `researcher.yaml` Target, so folding #23003 here would create a
cross-plan shared target for no gain. This plan delivers per-launch selection for
any definition, including `default` seats, and runs after #23003 in lane order. No
gate exists in either direction, so neither plan carries a deferral or dependency
edge for the other.

Authority ships before exposure: 1.2 lands the override-authority predicate and its
`before_tool` rule, and 2.1 adds the `network` parameter together with a factory
guard that calls the same predicate, so no build advertises `network` without
enforcement. Three of the orderings and mechanisms below are Orchestrator rulings
recorded in the V1 Plan Changelog for Josh's review before implementation.

## P1: Effective profile and override authority
`kind: framing`

### 1.1 Resolve the effective SRT profile for one launch [category: code]
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_network.py::*` — scope-reason: add the launch-local override helper beside `definition_sandbox_config`
- `tests/agents/test_sandbox_network.py::*` — scope-reason: cover inheritance, precedence, copy semantics, and the missing-definition refusal
- `tests/agents/test_resume_sandbox_gate.py::*` — scope-reason: cover persisted effective policy on resume

Add one helper in `src/gobby/agents/sandbox_network.py` that applies an explicit
profile to the final agent definition for one launch: null returns the body
unchanged; `none` or `trusted` returns `agent_body.model_copy(update={"network":
value})`; any other value raises; an explicit value with no resolved definition
(`agent_body is None`) raises with a clear refusal. `model_copy` skips Pydantic
validation, so the helper checks the value itself and the surfaces' `Literal`
types remain the outer trust boundary. The caller applies the helper after agent
fallback has chosen the final definition and passes the copy to the unchanged
`spawn_agent_impl`. The existing SRT gate then builds the effective config with
the write-path grant and `require_managed_srt` before placement, checkout,
session, or run allocation, and an unreadable Trusted seed refuses a trusted
launch before allocation. The copy never reaches storage, so the stored definition
and later launches keep the original profile. Resume needs no new code: the
existing resume metadata already stores the effective config, so a regression test
proves an overridden profile survives resume.

**Research context:** `definition_sandbox_config` already adds the Trusted seed,
Git, and registry groups while keeping `allow_network=false`; it is the only reader
of `AgentDefinitionBody.network`. `resolve_spawn_sandbox` combines that config with
`apply_write_grant` and `require_managed_srt`. `spawn_agent_impl` calls this gate
before `preflight_placement` and checkout creation, then passes the config to
`build_spawn_context`, which stores it as resume metadata `sandbox_config`;
`resolve_resume_sandbox` (`src/gobby/agents/sandbox_gate.py`) gates that snapshot on
resume and refuses a run without one. Reuse those paths unchanged:
`_implementation.py` (913 lines) and `_sandbox_gate.py` need no edits. Observed:
the installed profile is definition-only, no bundled definition sets
`network: trusted`, and the MCP factory passes `agent_body=None` when `default`
is missing. Planned: focused sandbox and resume tests, then format, lint, and
type checks.

**Acceptance:**

- 1.1.1 - Null inherits the final definition's profile; explicit `none` and `trusted` take precedence for one launch, and an explicit value without a resolved definition refuses. behavior: "effective per-spawn SRT network profile".
- 1.1.2 - The override is a launch-local copy: the loaded body and stored definition keep their profile. behavior: "one-launch override lifetime".
- 1.1.3 - Resume reuses the saved effective sandbox config, so an overridden profile survives resume without new resume code. behavior: "resume retains selected network profile".

### 1.2 Deny unauthorized overrides (depends: 1.1) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/condition_helpers_sessions.py::*` — scope-reason: add the override-authority predicate on verified caller identity
- `src/gobby/install/shared/workflows/rules/tool-hygiene/limit-spawn-network-override.yaml`
- `src/gobby/install/shared/workflows/agents/default.yaml::*` — scope-reason: allow the default spawned definition to spawn children
- `tests/workflows/test_spawn_scope_rules.py::*` — scope-reason: cover root, allowed child, denied child, run-less child, and forged-parent cases

Add a predicate beside `spawn_target_allowed` that decides whether a caller
session may pass an explicit `network`. It reuses that helper's identity
classification: a root session (`agent_run_id is None` and `agent_depth == 0`)
may override; a run-less session at depth above 0 fails closed; a spawned
session resolves its recorded agent run's `agent_name`, and only `default` and
`orchestrator` may choose either profile. Missing or inconsistent identity
raises, and a raising block condition fails closed. Other spawned callers may
still spawn with no override. Never use caller-supplied `parent_session_id` to
grant authority. Add a default-deny `before_tool` rule for explicit MCP
`spawn_agent.network` that calls the predicate with the verified
`_platform_session_id`. Set bundled `default` to `spawnable_agents: ["*"]`.
`orchestrator` is an authority name only: this plan creates no orchestrator
definition, because #22996, **Coordination seats: assistant, orchestrator,
lane-manager**, owns `orchestrator.yaml`. Override authority never bypasses
seat-spawn policy; `seat-no-spawn` still blocks seat callers from spawning.

**Research context:** `spawn_target_allowed` in
`condition_helpers_sessions.py` resolves a caller through `SessionManager` and
its recorded agent run, treats only a run-less depth-0 session as root, and
raises on unresolvable identity; `limit-spawnable-agents.yaml` passes it the
verified platform session metadata. Reuse that trust boundary. Observed: bundled
`default` has no `spawnable_agents`; `seat-spawn-policy.yaml` blocks seat
callers from spawning. Planned: isolated rule tests covering root, `default`,
`orchestrator`, other spawned definitions, a run-less depth-1 session, missing
run identity, and forged parent IDs; inspect the installed rule after an
authorized coordinated restart.

**Acceptance:**

- 1.2.1 - Root and named spawned callers may select either profile; all other spawned callers may spawn only without an override. behavior: "network override authority".
- 1.2.2 - A forged parent session ID or a run-less non-root session never gains override authority. behavior: "verified caller identity".
- 1.2.3 - Bundled default permits child spawning, and override authority leaves seat-spawn policy in force. behavior: "definition and seat spawn policy".

## P2: Single-spawn surfaces
`kind: framing`

### 2.1 Expose the choice on single-spawn surfaces (depends: 1.2) [category: code]
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: add the MCP parameter, the authority guard, and the override after fallback resolution
- `src/gobby/servers/routes/agent_spawn.py::*` — scope-reason: validate, guard, and apply the HTTP single-spawn field
- `src/gobby/cli/agents.py::*` — scope-reason: parse and forward the CLI flag
- `src/gobby/install/shared/skills/gobby/references/agents/spawning.md`
- `docs/guides/agents.md`
- `docs/guides/sandboxing.md`
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: test MCP parsing, fallback, guard, and refusal before allocation
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: test HTTP parsing, web-chat rejection, and credentials
- `tests/cli/test_cli_agents.py::*` — scope-reason: test CLI flag parsing and forwarding
- `tests/workflows/test_mcp_step.py::*` — scope-reason: prove a pipeline MCP step cannot bypass override authority

Add optional `network: Literal["none", "trusted"] | None` to MCP `spawn_agent`
and the single-agent HTTP request, and `--network none|trusted` to `gobby agents
spawn`. The CLI sends the flag only when present. Reject an explicit network value
for HTTP `web_chat`, which does not launch an SRT agent. Keep direct HTTP spawn
restricted to operator credentials; managed agent credentials remain refused.

When `network` is explicit, the MCP factory and the HTTP route run one authority
guard before applying the 1.1 helper and before any launch allocation. The guard
resolves the caller from the request principal (`get_request_principal`, the
pattern in `_terminal_termination.py`) and the current session context, never
from `parent_session_id`:

- principal `None` (local operator credentials) with no session context: allowed;
- principal `None` with a session context: the 1.2 predicate on that session;
- managed agent token claims: the 1.2 predicate on the token's session;
- no request principal (`LookupError`, an internal caller such as a pipeline
  step) with a seeded session: the 1.2 predicate on that session;
- no request principal and no session: refused.

The guard keeps the 1.2 `before_tool` rule in place and also covers pipeline MCP
steps, which call tools with `enforce_workflow=False` under the pipeline's seeded
session. After the guard passes and fallback has chosen the final definition,
the surface applies the 1.1 helper and calls `spawn_agent_impl` with the copy.
HTTP creates or reuses its per-project `web_launcher` parent session before
resolving the body; that reused row is not a launch allocation, so a refusal
after it is acceptable. Update the existing spawning reference, agents guide,
and sandboxing guide with the flag and parameter, null and fallback
inheritance, one-launch lifetime, authority limits, and the fact that `trusted`
remains an SRT allowlist. Batch documentation stays until D1 retires batch
spawning.

**Research context:** MCP `create_spawn_agent_registry.spawn_agent` chooses its
fallback body before calling `spawn_agent_impl`; HTTP `_do_spawn` resolves its
body after `aget_or_create_launcher_session` and calls the same core function.
CLI `spawn_agent_cmd` posts to the MCP single-spawn endpoint.
`AgentSpawnRequest` has a `web_chat` branch before managed launch. The agent
capability matrix in `src/gobby/servers/auth_service.py` rejects managed agent
tokens on `POST /api/agents/spawn` and `POST /api/pipelines/run`, pinned by
`tests/servers/test_auth_service.py::test_agent_capability_matrix`; the CLI posts
to `/api/mcp/gobby-agents/tools/spawn_agent` with `daemon_auth_headers`, which
prefers a managed run token, and such calls get daemon-side `before_tool`
enforcement (`_enforce_workflow_for_request`). Pipeline MCP steps
(`src/gobby/workflows/pipeline/handlers.py`) seed the pipeline's `session_id`
with `resolve_and_seed_contexts` and call `tool_proxy.call_tool(...,
enforce_workflow=False)`, so `before_tool` rules do not run there.
`get_request_principal` raises `LookupError` for internal callers, returns `None`
for local operator requests, and returns token claims for managed agents.
Observed: none of these three surfaces has a network parameter; `_factory.py`
has 844 lines and stays under the production ceiling after this change. Planned:
focused MCP, HTTP, Click, and pipeline-step tests for valid and invalid values,
null, fallback in both directions, explicit `none` on a trusted final
definition, a later omitted launch inheriting the original profile, `web_chat`,
the HTTP credential boundary, and denied-authority and unreadable-Trusted-seed
refusals that never reach placement, checkout creation, child-session creation,
or runner launch.

**Acceptance:**

- 2.1.1 - MCP, HTTP, and CLI expose only `none|trusted`, with omission/null inheriting the final definition, including after fallback in both directions. behavior: "single-spawn network input".
- 2.1.2 - HTTP `web_chat` rejects explicit network and managed credentials cannot call the direct HTTP spawn route. behavior: "HTTP spawn boundary".
- 2.1.3 - An explicit override from an unauthorized caller, including through a pipeline MCP step, refuses before placement, checkout, child session, or launch. behavior: "override authority at the spawn boundary".
- 2.1.4 - The spawning reference, agents guide, and sandboxing guide document the parameter, flag, inheritance, lifetime, and authority limits. file: `docs/guides/sandboxing.md`.

## D1 Batch spawn retirement (depends: 1.2, 2.1)
`kind: deferred`

Batch retirement waits on an external prerequisite: the separate removal of the
`merge-orchestrator` definition must be installed in the DB registry first. On
2026-10-05 `uv run gobby agents show merge-orchestrator` still printed an installed,
enabled global row (`source: installed`), and its prompt dispatches `merge-worker`
through `dispatch_batch`. #23460, **Plan the merge-orchestrator removal with no
replacement merge orchestration**, owns planning that removal and names this D1 as
its downstream dependent; it is the prerequisite, not the owner of D1.1-D1.3. The
contract forbids a prose blocker for that wait, so this work is deferred. Expansion
creates its `planning` task under this plan's epic, blocked by #23460 and by the
removal leaf that #23460's plan expands into, and its spec is written once the
removal is installed. Do not move merge orchestration to another path.

Expected scope for that planning pass, carried from the draft:

Expected targets:
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

Once the removal is installed, remove MCP `dispatch_batch` and HTTP `/api/agents/spawn/batch`,
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

Obligations the planning pass keeps: D1.1, the MCP registry has no
`dispatch_batch` tool and HTTP batch requests do not route; D1.2, permissions,
rules, active guidance, and tests no longer refer to either retired API; D1.3,
installed merge-orchestrator removal is confirmed before retirement, with no
replacement merge orchestration.

```yaml
deferral:
  task_ref: "TBD-batch-retirement"
  reason: "External prerequisite: the merge-orchestrator removal planned under #23460 must be installed in the DB registry first."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
    - D1.2
    - D1.3
```

## 3 End-to-end verification
`kind: verification`

Run `uv run gobby plans validate .gobby/plans/spawn-network-override.md -p
/Users/josh/Projects/gobby` on this draft. Implementation verification uses
focused API, CLI, pipeline-step, sandbox, resume, and authorization tests with
isolated test state; repository format, lint, type, test-quality, and test-type
checks; and, after the coordinated restart that activates 2.1, an installed-rule
check that `limit-spawn-network-override` is present and enabled plus a live MCP
schema check that `spawn_agent` advertises `network`. The factory guard ships in
the same change as the parameter, so enforcement never relies on rule sync
alone. Do not run the full pytest suite. `gclient` ordinary shell tabs and panes
remain unchanged.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05, Lane 7 plan writer 2 (gobby#15413), #23444 writer pass before the
  enhancer: added the #23003 relationship (sequenced, not folded); converted batch
  retirement from deliverable 3.1 to deferred D1 because its merge-orchestrator
  prerequisite is external and unowned; corrected 1.1 research context (913 lines,
  resume already stores `sandbox_config`, so 1.1.3 is a regression test); cited the
  agent capability matrix for the HTTP credential boundary in 1.2; renamed the
  verification section to free the `V1` changelog ID.
- 2026-10-05, Lane 7 plan writer 2 (gobby#15413), #23444 enhancer pass: one
  `plan-enhancer-taskless-old` run (20805752) returned six opportunities, and the
  Orchestrator (gobby#14972) accepted all six as recommended. E01-E03 change the
  user-supplied ordering or mechanism; they are Orchestrator rulings that Josh
  reviews before implementation. Sections were renumbered: old 1.1 stays 1.1, old
  2.1 (authority) is now 1.2, and old 1.2 (exposure) is now 2.1.
  - E01, authority before exposure: resolved by ordering 1.1, then 1.2 authority,
    then 2.1 exposure, with the factory guard shipping alongside the parameter
    and verification checking the installed rule and live schema after restart.
  - E02, orchestrator.yaml collision with #22996: resolved by removing that
    Target and old acceptance 2.1.3; `orchestrator` remains an authority name,
    and seat-spawn policy stays in force.
  - E03, pipeline MCP steps bypass `before_tool` rules: resolved by keeping the
    rule and adding a factory and HTTP guard keyed on the request principal and
    session context, with a pipeline-step regression in `test_mcp_step.py`.
  - E04, least mechanism: resolved by replacing the planned
    `_network_preflight.py` extraction with a launch-local `model_copy` helper
    in `sandbox_network.py`; `_implementation.py` and `_sandbox_gate.py` leave
    the Targets, and an explicit value without a resolved definition refuses.
  - E05, test coverage: resolved by acceptance 1.1.2 and 2.1.3 and the 2.1 test
    plan (one-launch lifetime, fallback both ways, refusal before allocation);
    the reused HTTP launcher session is not a launch allocation.
  - E06, documentation: resolved by adding the spawning reference, agents
    guide, and sandboxing guide to 2.1; batch documentation waits for D1.
  - Also: #23003 note refreshed to its corrected spec; D1 names #23460 as its
    blocking prerequisite and keeps its own placeholder `task_ref`, as the
    Orchestrator confirmed.
