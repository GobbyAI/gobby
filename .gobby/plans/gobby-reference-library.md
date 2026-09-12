# Gobby: thin router and verified reference library

**Plan ID:** gobby-reference-library

## Overview
`kind: framing`

Make gobby the single entrypoint for Gobby capabilities and installed standalone skills.
Fold the 30 named Gobby skills into an explicitly loaded reference library. Retain
standalone methodology and dynamically discover installed skills. Audit and correct the
human-facing guides against current implementations, schemas, tests and installed configuration.

This materializes the user-approved conversational direction for planning task #22167.
The original draft has not yet passed file validation. Implementation is authorized by
the user's instruction to implement and subsequent instruction to proceed.

## Constraints
`kind: framing`

No new product operations or authority. Menus never execute operations. Reference loading
keeps the existing get_skill_file schema gate and complete cursor pagination. Tool schemas
are authoritative for parameters. Guides remain detailed human documentation; references
are concise agent instructions with loading conditions and verified guide links.

Capability identifiers take precedence. Singular skill explicitly dispatches a standalone
skill; plural skills is the skill-management capability. Preserve trailing arguments and
standalone levels, project overrides, and internal skills' default visibility. Connected
MCP servers are never inferred to be installed skills.

Retain: `brevity`, `restraint`, `proportionality`, `elicit`, `ideate`, `research`, `architecture`, `prd`, `code-review`, `decompose-monolith`, `repository-maintenance`, `test-driven-development`, `triage-judgment`, `impeccable`, `tech-writer`, `bridge`, `browser-testing`, `coderabbit`, `context7`, `gusto`, `bash`, `c`, `cpp`, `csharp`, `dart`, `elixir`, `go`, `java`, `javascript`, `json`, `kotlin`, `lua`, `objc`, `php`, `python`, `ruby`, `rust`, `scala`, `swift`, `typescript`, `yaml`.
The current bundle additionally contains annotate; preserve it as a standalone skill.
Gusto is absent from the checked-out bundle; preserve it when installed, without inventing
a bundled implementation. These observations adjust inventory counts, not the 30-name
retirement scope. No retired-name compatibility wrappers.

Use the existing session variable store for completed reference identities; no SQL schema
migration or second durable skill-usage table is needed. Standalone usage and level
tracking continue unchanged. A reference identity is exactly skill-name:references/path.md.
Reject absolute paths, traversal, backslashes, empty components, and non-reference paths.

Inspection evidence: get_skill.py records completed entrypoint loads through
record_completed_load, while get_skill_file_tool currently returns pages without recording
loads. The observer separately updates loaded_skills. Reset sites include in-place compact,
session lifecycle, terminal session reset, and bundled reset rules. The shared requirement
resolver must cover both producers and consumers so a router-only load cannot pass a
reference requirement.

Inspection evidence: AgentEventHandlerMixin._intercept_skill_command currently treats
skill and skills as equivalent prefixes. Its help uses discover_core_skills and filters
the active skill set. Preserve those resolution and visibility semantics. sync_bundled_skills
already soft-deletes absent Gobby-owned rows only after successful bundle loading; reuse it.

Consumer discovery used gcode grep over loaded_skills, skill_fetch_directive and folded
skill names in production/templates/tests; the inventories below include discovered
instruction consumers. File-wide scopes below cover cohesive cross-function instruction
resolution or mechanical fixture migrations; they are not permission for unrelated edits.
No production file may reach 1,000 lines. Focused validation uses the isolated test hub:
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
and GOBBY_TEST_PROTECT=1. No full pytest suite. No mutating examples against live state.

Each capability audit records exact guide anchors, tool/CLI names, implementation symbols,
test or isolated-probe evidence, installed configuration observations where relevant,
discrepancies and their correction. Never claim a template is active from its YAML alone.
An implementation/contract conflict is a defect to resolve, not permission to rewrite the
normative contract. Missing coverage fails integrated verification.

## P1: Reference identity and catalog contracts
`kind: framing`

### 1.1 Resolve and enforce exact instruction identities [category: code]
`kind: deliverable`

Targets:
- `src/gobby/skills/instruction_requirements.py`
- `src/gobby/skills/formatting.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `src/gobby/workflows/claimed_task_extra_skills.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `src/gobby/workflows/engine/skill_load_guidance.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `src/gobby/workflows/safe_evaluator.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/skills/test_skills_formatting.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/workflows/test_claimed_task_extra_skills.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/workflows/engine/test_skill_load_guidance.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/workflows/test_safe_evaluator.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: update instruction routing and its consumers as one contract
- `tests/skills/test_instruction_requirements.py`

**Research context:** formatting.py owns skill_fetch_directive and batch directives;
claimed_task_extra_skills.missing_claimed_task_extra_skills compares names to loaded_skills;
skill_load_guidance._skill_load_targets does the same for workflow steps; safe_evaluator
exposes skill_loaded; spawn_agent/_step_state checks required skills when starting steps.
Introduce new parse_instruction_requirement, instruction_is_loaded and
instruction_fetch_directive helpers in the new module, reused by these entrypoints.

Accept existing plain names and exact reference identifiers in existing requirement lists.
Keep plain-name behavior and level variables unchanged. Reference requirements compare only
to loaded_skill_references, a set-like list of exact canonical strings. Do not accept a
loaded gobby entrypoint, a different topic or a malformed identity. Render reference
directives with explicit schema discovery and paginated get_skill_file loading; do not
inline bodies. Existing skill_loaded accepts either identity through the shared resolver.

**Granularity:** This leaf changes one requirement interpretation contract across existing
callers. Load delivery is independently testable and belongs to 1.2.

**Acceptance:**
- 1.1.1 - Shared parser validates names and reference paths and rejects traversal and malformed identities. file: `src/gobby/skills/instruction_requirements.py`.
- 1.1.2 - Requirement consumers accept exact completed references and reject router-only, sibling-topic and absent loads. test: `tests/skills/test_instruction_requirements.py`.
- 1.1.3 - Plain skills, level preservation and schema-first directives retain their existing behavior. test: `tests/skills/test_skills_formatting.py`.

### 1.2 Track complete reference delivery and reset it with context [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/skills/get_skill.py::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/workflows/observer_mcp.py::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/hooks/event_handlers/_session_start/in_place_compact.py::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/servers/routes/sessions/lifecycle.py::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/storage/sessions/_terminal.py::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/install/shared/workflows/rules/skill-discovery/reset-skill-injection.yaml::*` — scope-reason: record or reset the same completed-reference state
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: record or reset the same completed-reference state
- `tests/mcp_proxy/tools/skills/test_get_skill.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/mcp_proxy/tools/skills/test_skill_delivery.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/workflows/test_skill_loaded_call_tool_path.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/workflows/test_language_skill_reset_rules.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/storage/sessions/test_session_tmux_runtime.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/servers/routes/test_session_variables.py::*` — scope-reason: record or reset the same completed-reference state
- `tests/skills/test_reference_load_tracking.py`

**Research context:** get_skill_file_tool already checks content hashes, cursor kind and file
existence and builds lossless brief/full pages. record_completed_load resolves the ambient
session and uses SessionVariableManager.append_to_set_variable for skill entrypoints.
observer_mcp independently sees successful skill calls. Reuse the session-resolution and
set-append patterns, while keeping reference tracking separate from session_skills usage.

Record a reference only after a successful final content page, including cursor continuation.
Do not count get_skill_files manifests, help, errors or partial pages. Ensure the observer
handles direct and wrapped results without double-counting or confusing a file with SKILL.md.
Reset the new state at every context reset that clears loaded_skills, preserving standalone
level behavior. Pagination errors and stale cursors retain existing typed recovery.

**Granularity:** Delivery, observer reconciliation and context reset jointly own one
context-scoped completion lifecycle; requirement interpretation is already provided by 1.1.

**Acceptance:**
- 1.2.1 - Complete direct and wrapped reference retrieval records only the exact identity. test: `tests/skills/test_reference_load_tracking.py`.
- 1.2.2 - Partial, failed, stale, menu and listing responses do not satisfy reference requirements. test: `tests/mcp_proxy/tools/skills/test_skill_delivery.py`.
- 1.2.3 - Context resets clear completed references alongside skills while preserving existing level rules. test: `tests/workflows/test_language_skill_reset_rules.py`.

### 1.3 Define the bundled capability catalog and validator [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/skills/capability_catalog.py`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `tests/skills/test_capability_catalog.py`

**Research context:** Existing routing embeds only skill selection in _agent.py; skill
files already resolve through project-aware storage. A bundled JSON catalog is sufficient;
no new registry table or MCP service is needed. New load_capability_catalog and validation
helpers expose names, descriptions, overview paths, topics and loading conditions.

Use version 1 with capabilities as a list of records containing name, description,
overview, when and topics; topics contain name, description, path and when.
Paths are relative to the gobby skill. Validate duplicate names/topic identifiers,
missing files, invalid paths and duplicate reference identities. Start with an empty valid
catalog and populate it atomically with each capability's files in P2. Validate with
fixture catalogs before documentation exists. The catalog contains metadata, never bodies.

**Acceptance:**
- 1.3.1 - One catalog contract carries capability and topic metadata and validates all reference paths. file: `src/gobby/skills/capability_catalog.py`.
- 1.3.2 - Duplicate identifiers, missing files and unsafe paths fail with actionable diagnostics. test: `tests/skills/test_capability_catalog.py`.

## P2: Verified capability library
`kind: framing`

Each leaf owns its complete capability audit. Sequential dependencies explicitly order the
shared catalog, shared guide indexes and coverage evidence. Each topic contains: when to
load, schema discovery entrypoints, normal workflow, constraints, failures/recovery and
verified guide links. Preserve existing normative obligations from absorbed source skills.
Retain source entrypoints until the final cutover.

### 2.1 Author and verify tasks guidance [category: docs] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/tasks/overview.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/creation.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/dependencies.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/implementation.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/reviews.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/artifacts.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/backups.md`
- `src/gobby/install/shared/skills/gobby/references/tasks/live-work.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/tasks.json`
- `docs/guides/tasks.md`
- `docs/guides/tdd-enforcement.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/tasks/`, `src/gobby/mcp_proxy/tools/task_dependencies.py`, `src/gobby/mcp_proxy/tools/task_commits.py`, `src/gobby/mcp_proxy/tools/task_readiness.py`.
Source skills are `tasks`, `live-session` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are tasks, tdd-enforcement. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Discovery, creation, claiming, dependencies, labels, implementation, found work, validation, closing, reviews, artifacts, backups/imports and live work.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.1.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/tasks/overview.md`.
- 2.1.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/tasks.json`.
- 2.1.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.2 Author and verify plan guidance [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/plan/overview.md`
- `src/gobby/install/shared/skills/gobby/references/plan/drafting.md`
- `src/gobby/install/shared/skills/gobby/references/plan/coverage.md`
- `src/gobby/install/shared/skills/gobby/references/plan/enhancement.md`
- `src/gobby/install/shared/skills/gobby/references/plan/repair.md`
- `src/gobby/install/shared/skills/gobby/references/plan/review.md`
- `src/gobby/install/shared/skills/gobby/references/plan/approval.md`
- `src/gobby/install/shared/skills/gobby/references/plan/expansion.md`
- `src/gobby/install/shared/skills/gobby/references/plan/lifecycle.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/plan.json`
- `docs/guides/plans-and-plan-mode.md`
- `docs/guides/task-expansion.md`
- `docs/guides/spec-writing.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/plans/`, `src/gobby/mcp_proxy/tools/tasks/_expansion_registry.py`.
Source skills are `plan`, `plan-draft`, `plan-enhance`, `plan-mechanic`, `plan-review`, `expand`, `expansion-agent-selection` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are plans-and-plan-mode, task-expansion, spec-writing. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Drafting, coverage grammar, enhancement, repair, review evidence, approval, expansion, registration and archival.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.2.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/plan/overview.md`.
- 2.2.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/plan.json`.
- 2.2.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.3 Author and verify build guidance [category: docs] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/build/overview.md`
- `src/gobby/install/shared/skills/gobby/references/build/starting.md`
- `src/gobby/install/shared/skills/gobby/references/build/profiles.md`
- `src/gobby/install/shared/skills/gobby/references/build/stages.md`
- `src/gobby/install/shared/skills/gobby/references/build/coordination.md`
- `src/gobby/install/shared/skills/gobby/references/build/monitoring.md`
- `src/gobby/install/shared/skills/gobby/references/build/recovery.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/build.json`
- `docs/guides/dispatch.md`
- `docs/guides/orchestration.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/build.py`, `src/gobby/mcp_proxy/tools/profiles.py`, `src/gobby/mcp_proxy/tools/tasks/_stage_ops.py`.
Source skills are `build`, `build-coordinator` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are dispatch, orchestration. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Starting builds, profiles, stages, coordination, monitoring, cancellation and recovery.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.3.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/build/overview.md`.
- 2.3.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/build.json`.
- 2.3.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.4 Author and verify sessions guidance [category: docs] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/sessions/overview.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/discovery.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/transcripts.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/handoffs.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/context.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/relationships.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/terminals.md`
- `src/gobby/install/shared/skills/gobby/references/sessions/waits.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/sessions.json`
- `docs/guides/sessions.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/sessions/`.
Source skills are `handoff-discipline` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are sessions. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Discovery, transcripts, handoffs, context resets, relationships, terminal control and event-driven waits.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.4.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/sessions/overview.md`.
- 2.4.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/sessions.json`.
- 2.4.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.5 Author and verify agents guidance [category: docs] (depends: 2.4)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/agents/overview.md`
- `src/gobby/install/shared/skills/gobby/references/agents/definitions.md`
- `src/gobby/install/shared/skills/gobby/references/agents/spawning.md`
- `src/gobby/install/shared/skills/gobby/references/agents/providers.md`
- `src/gobby/install/shared/skills/gobby/references/agents/isolation.md`
- `src/gobby/install/shared/skills/gobby/references/agents/personas.md`
- `src/gobby/install/shared/skills/gobby/references/agents/messaging.md`
- `src/gobby/install/shared/skills/gobby/references/agents/checkpoints.md`
- `src/gobby/install/shared/skills/gobby/references/agents/lifecycle.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/agents.json`
- `docs/guides/agents.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/sandboxing.md`
- `docs/guides/sandbox-compatibility.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/agents_registry.py`, `src/gobby/mcp_proxy/tools/agents_spawn_tools.py`, `src/gobby/mcp_proxy/tools/agents_query_tools.py`, `src/gobby/mcp_proxy/tools/agents_checkpoint_tools.py`, `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`, `src/gobby/mcp_proxy/tools/agent_messaging.py`, `src/gobby/mcp_proxy/tools/apply_persona.py`.
Source skills are `persona` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are agents, providers-and-models, sandboxing, sandbox-compatibility. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Definitions, spawning, provider/model selection, isolation, personas, messaging, checkpoints and lifecycle.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.5.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/agents/overview.md`.
- 2.5.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/agents.json`.
- 2.5.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.6 Author and verify memory guidance [category: docs] (depends: 2.5)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/memory/overview.md`
- `src/gobby/install/shared/skills/gobby/references/memory/search.md`
- `src/gobby/install/shared/skills/gobby/references/memory/capture.md`
- `src/gobby/install/shared/skills/gobby/references/memory/scope.md`
- `src/gobby/install/shared/skills/gobby/references/memory/maintenance.md`
- `src/gobby/install/shared/skills/gobby/references/memory/review-lessons.md`
- `src/gobby/install/shared/skills/gobby/references/memory/post-task.md`
- `src/gobby/install/shared/skills/gobby/references/memory/dream.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/memory.json`
- `docs/guides/memory.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/memory.py`, `src/gobby/mcp_proxy/tools/memory_write.py`, `src/gobby/mcp_proxy/tools/memory_scope.py`, `src/gobby/mcp_proxy/tools/memory_review.py`, `src/gobby/mcp_proxy/tools/memory_dream.py`, `src/gobby/mcp_proxy/tools/review_learning.py`.
Source skills are `memory`, `review-learning` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are memory. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Search, capture, scope, maintenance, review lessons, post-task review and dream operations.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.6.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/memory/overview.md`.
- 2.6.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/memory.json`.
- 2.6.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.7 Author and verify code-index guidance [category: docs] (depends: 2.6)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/code-index/overview.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/search.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/retrieval.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/navigation.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/impact.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/recovery.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/code-index.json`
- `docs/guides/code-index.md`
- `docs/guides/gcode-user-guide.md`
- `docs/guides/gcode-graph-core.md`
- `docs/guides/search.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/internal.py`.
Source skills are `code-index` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are code-index, gcode-user-guide, gcode-graph-core, search. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Search, retrieval, navigation, impact analysis, graphs, freshness and repair.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.7.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/code-index/overview.md`.
- 2.7.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/code-index.json`.
- 2.7.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.8 Author and verify skills guidance [category: docs] (depends: 2.7)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/skills/overview.md`
- `src/gobby/install/shared/skills/gobby/references/skills/discovery.md`
- `src/gobby/install/shared/skills/gobby/references/skills/loading.md`
- `src/gobby/install/shared/skills/gobby/references/skills/levels.md`
- `src/gobby/install/shared/skills/gobby/references/skills/references.md`
- `src/gobby/install/shared/skills/gobby/references/skills/hubs.md`
- `src/gobby/install/shared/skills/gobby/references/skills/installation.md`
- `src/gobby/install/shared/skills/gobby/references/skills/authoring.md`
- `src/gobby/install/shared/skills/gobby/references/skills/scripts.md`
- `src/gobby/install/shared/skills/gobby/references/skills/lifecycle.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/skills.json`
- `docs/guides/skills.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/skills/`.
Source skills are `loading-skills`, `writing-skills` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are skills. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Discovery, loading, levels, references, hubs, installation, authoring, scripts and lifecycle.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.8.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/skills/overview.md`.
- 2.8.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/skills.json`.
- 2.8.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.9 Author and verify rules guidance [category: docs] (depends: 2.8)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/rules/overview.md`
- `src/gobby/install/shared/skills/gobby/references/rules/authoring.md`
- `src/gobby/install/shared/skills/gobby/references/rules/events.md`
- `src/gobby/install/shared/skills/gobby/references/rules/conditions.md`
- `src/gobby/install/shared/skills/gobby/references/rules/effects.md`
- `src/gobby/install/shared/skills/gobby/references/rules/enforcement.md`
- `src/gobby/install/shared/skills/gobby/references/rules/overrides.md`
- `src/gobby/install/shared/skills/gobby/references/rules/diagnostics.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/rules.json`
- `docs/guides/rules.md`
- `docs/guides/workflow-rules.md`
- `docs/guides/workflows-overview.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/workflows/_rules.py`.
Source skills are `build-rule` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are rules, workflow-rules, workflows-overview. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Authoring, events, conditions, effects, enforcement, overrides, enabled state and diagnostics.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.9.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/rules/overview.md`.
- 2.9.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/rules.json`.
- 2.9.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.10 Author and verify mcp-servers guidance [category: docs] (depends: 2.9)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/overview.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/discovery.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/templates.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/instances.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/authentication.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/oauth.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/diagnostics.md`
- `src/gobby/install/shared/skills/gobby/references/mcp-servers/results.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/mcp-servers.json`
- `docs/guides/mcp-tools.md`
- `docs/guides/mcp-oauth.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/internal.py`, `src/gobby/mcp_proxy/tools/results.py`.
Source skills are `mcp-servers` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are mcp-tools, mcp-oauth. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Discovery, templates, instances, scope, authentication, OAuth, diagnostics and oversized results.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.10.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/mcp-servers/overview.md`.
- 2.10.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/mcp-servers.json`.
- 2.10.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.11 Author and verify pipelines guidance [category: docs] (depends: 2.10)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/pipelines/overview.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/authoring.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/validation.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/execution.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/approvals.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/recovery.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/history.md`
- `src/gobby/install/shared/skills/gobby/references/pipelines/scheduling.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/pipelines.json`
- `docs/guides/pipelines.md`
- `docs/guides/cron-scheduler.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/workflows/_pipelines.py`, `src/gobby/mcp_proxy/tools/workflows/_pipeline_execution.py`, `src/gobby/mcp_proxy/tools/cron.py`.
Source skills are `pipelines-and-cron` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are pipelines, cron-scheduler. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Authoring, validation, execution, approvals, recovery, history and scheduling.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.11.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/pipelines/overview.md`.
- 2.11.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/pipelines.json`.
- 2.11.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.12 Author and verify source-control guidance [category: docs] (depends: 2.11)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/source-control/overview.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/commits.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/worktrees.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/clones.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/synchronization.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/merge-campaigns.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/pr-delivery.md`
- `src/gobby/install/shared/skills/gobby/references/source-control/cleanup.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/source-control.json`
- `docs/guides/worktrees.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/worktrees/`, `src/gobby/mcp_proxy/tools/clones.py`, `src/gobby/mcp_proxy/tools/merge.py`.
Source skills are `source-control`, `clones`, `merge`, `merge-expert` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are worktrees. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Commits, worktrees, clones, synchronization, merge campaigns, PR delivery and cleanup.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.12.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/source-control/overview.md`.
- 2.12.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/source-control.json`.
- 2.12.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.13 Author and verify review guidance [category: docs] (depends: 2.12)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/review/overview.md`
- `src/gobby/install/shared/skills/gobby/references/review/epic.md`
- `src/gobby/install/shared/skills/gobby/references/review/evidence.md`
- `src/gobby/install/shared/skills/gobby/references/review/feedback.md`
- `src/gobby/install/shared/skills/gobby/references/review/outcomes.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/review.json`
- `docs/guides/test-quality.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/feedback.py`, `src/gobby/mcp_proxy/tools/tasks/_stage_review.py`.
Source skills are `review`, `epic-review` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are test-quality. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Epic review, task evidence, feedback batches and outcomes.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.13.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/review/overview.md`.
- 2.13.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/review.json`.
- 2.13.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.14 Author and verify intro guidance [category: docs] (depends: 2.13)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/intro/overview.md`
- `src/gobby/install/shared/skills/gobby/references/intro/profile.md`
- `src/gobby/install/shared/skills/gobby/references/intro/onboarding.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/intro.json`
- `docs/guides/README.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/profiles.py`.
Source skills are `intro` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are README. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Working profile and onboarding.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.14.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/intro/overview.md`.
- 2.14.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/intro.json`.
- 2.14.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.15 Author and verify development guidance [category: docs] (depends: 2.14)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/development/overview.md`
- `src/gobby/install/shared/skills/gobby/references/development/obligations.md`
- `src/gobby/install/shared/skills/gobby/references/development/channels.md`
- `src/gobby/install/shared/skills/gobby/references/development/native-components.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/development.json`
- `docs/guides/testing.md`
- `docs/guides/gcode-development-guide.md`
- `docs/guides/gcore-development-guide.md`
- `docs/guides/ghook-development-guide.md`
- `docs/guides/gterminal-development-guide.md`
- `docs/guides/adapter-fidelity.md`
- `docs/guides/one-surface-tab-recipe.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/internal.py`.
Source skills are `development-discipline`, `channel-parity` under the bundled skills directory; their entrypoints and existing topic files supply obligations, not assumed current truth.
The relevant guides are testing, gcode-development-guide, gcore-development-guide, ghook-development-guide, gterminal-development-guide, adapter-fidelity, one-surface-tab-recipe. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Developer-agent obligations, channel development and native-component guide navigation.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.15.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/development/overview.md`.
- 2.15.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/development.json`.
- 2.15.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.16 Author and verify variables guidance [category: docs] (depends: 2.15)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/variables/overview.md`
- `src/gobby/install/shared/skills/gobby/references/variables/definitions.md`
- `src/gobby/install/shared/skills/gobby/references/variables/values.md`
- `src/gobby/install/shared/skills/gobby/references/variables/defaults.md`
- `src/gobby/install/shared/skills/gobby/references/variables/scope.md`
- `src/gobby/install/shared/skills/gobby/references/variables/overrides.md`
- `src/gobby/install/shared/skills/gobby/references/variables/resets.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/variables.json`
- `docs/guides/variables.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/workflows/_variables.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are variables. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Definitions, session values, defaults, scope, overrides and resets.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.16.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/variables/overview.md`.
- 2.16.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/variables.json`.
- 2.16.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.17 Author and verify config guidance [category: docs] (depends: 2.16)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/config/overview.md`
- `src/gobby/install/shared/skills/gobby/references/config/schema.md`
- `src/gobby/install/shared/skills/gobby/references/config/values.md`
- `src/gobby/install/shared/skills/gobby/references/config/updates.md`
- `src/gobby/install/shared/skills/gobby/references/config/models.md`
- `src/gobby/install/shared/skills/gobby/references/config/restarts.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/config.json`
- `docs/guides/configuration.md`
- `docs/guides/ai-configuration.md`
- `docs/guides/ai-daemon-contract.md`
- `docs/guides/llm-features.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/config.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are configuration, ai-configuration, ai-daemon-contract, llm-features. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Schema, desired/active values, revision-checked updates, model configuration and restart requirements.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.17.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/config/overview.md`.
- 2.17.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/config.json`.
- 2.17.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.18 Author and verify hub guidance [category: docs] (depends: 2.17)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/hub/overview.md`
- `src/gobby/install/shared/skills/gobby/references/hub/identity.md`
- `src/gobby/install/shared/skills/gobby/references/hub/projects.md`
- `src/gobby/install/shared/skills/gobby/references/hub/queries.md`
- `src/gobby/install/shared/skills/gobby/references/hub/aggregates.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/hub.json`
- `docs/guides/shared-stack.md`
- `docs/guides/hub-install-contract.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/hub.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are shared-stack, hub-install-contract. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Machine/project identity, cross-project queries and aggregate information.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.18.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/hub/overview.md`.
- 2.18.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/hub.json`.
- 2.18.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.19 Author and verify communications guidance [category: docs] (depends: 2.18)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/communications/overview.md`
- `src/gobby/install/shared/skills/gobby/references/communications/channels.md`
- `src/gobby/install/shared/skills/gobby/references/communications/messages.md`
- `src/gobby/install/shared/skills/gobby/references/communications/attachments.md`
- `src/gobby/install/shared/skills/gobby/references/communications/identities.md`
- `src/gobby/install/shared/skills/gobby/references/communications/routing.md`
- `src/gobby/install/shared/skills/gobby/references/communications/subscriptions.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/communications.json`
- `docs/guides/comm-integrations.md`
- `docs/guides/telegram.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/communications.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are comm-integrations, telegram. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Channels, messages, attachments, identities, project routing and subscriptions.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.19.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/communications/overview.md`.
- 2.19.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/communications.json`.
- 2.19.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.20 Author and verify voice guidance [category: docs] (depends: 2.19)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/voice/overview.md`
- `src/gobby/install/shared/skills/gobby/references/voice/vocabulary.md`
- `src/gobby/install/shared/skills/gobby/references/voice/configuration.md`
- `src/gobby/install/shared/skills/gobby/references/voice/stt.md`
- `src/gobby/install/shared/skills/gobby/references/voice/tts.md`
- `src/gobby/install/shared/skills/gobby/references/voice/diagnostics.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/voice.json`
- `docs/guides/voice.md`
- `docs/guides/crane-tts.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/voice.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are voice, crane-tts. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Vocabulary, voice configuration, STT/TTS setup and diagnostics.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.20.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/voice/overview.md`.
- 2.20.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/voice.json`.
- 2.20.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.21 Author and verify observability guidance [category: docs] (depends: 2.20)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/observability/overview.md`
- `src/gobby/install/shared/skills/gobby/references/observability/metrics.md`
- `src/gobby/install/shared/skills/gobby/references/observability/capacity.md`
- `src/gobby/install/shared/skills/gobby/references/observability/usage.md`
- `src/gobby/install/shared/skills/gobby/references/observability/retention.md`
- `src/gobby/install/shared/skills/gobby/references/observability/diagnostics.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/observability.json`
- `docs/guides/observability.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/metrics.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are observability. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Metrics, capacity, usage, retention and diagnostics.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.21.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/observability/overview.md`.
- 2.21.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/observability.json`.
- 2.21.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.22 Author and verify admin guidance [category: docs] (depends: 2.21)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/admin/overview.md`
- `src/gobby/install/shared/skills/gobby/references/admin/installation.md`
- `src/gobby/install/shared/skills/gobby/references/admin/daemon.md`
- `src/gobby/install/shared/skills/gobby/references/admin/health.md`
- `src/gobby/install/shared/skills/gobby/references/admin/authentication.md`
- `src/gobby/install/shared/skills/gobby/references/admin/secrets.md`
- `src/gobby/install/shared/skills/gobby/references/admin/backups.md`
- `src/gobby/install/shared/skills/gobby/references/admin/recovery.md`
- `src/gobby/install/shared/skills/gobby/references/admin/portability.md`
- `src/gobby/install/shared/skills/gobby/references/admin/clients.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/admin.json`
- `docs/guides/admin-operations.md`
- `docs/guides/system-requirements.md`
- `docs/guides/release-guide.md`
- `docs/guides/remote-docker-acceptance.md`
- `docs/guides/gclient-user-guide.md`
- `docs/guides/web-ui.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/internal.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are admin-operations, system-requirements, release-guide, remote-docker-acceptance, gclient-user-guide, web-ui. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Installation, daemon lifecycle, health, authentication, secrets, backups, recovery, portability and client access.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.22.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/admin/overview.md`.
- 2.22.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/admin.json`.
- 2.22.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

### 2.23 Author and verify integrations guidance [category: docs] (depends: 2.22)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/integrations/overview.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/hooks.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/webhooks.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/plugins.md`
- `src/gobby/install/shared/skills/gobby/references/integrations/external.md`
- `src/gobby/install/shared/skills/gobby/catalog.json`
- `docs/reference-audit/integrations.json`
- `docs/guides/integrations.md`
- `docs/guides/hook-schemas.md`
- `docs/guides/ghook-user-guide.md`
- `docs/guides/webhooks-and-plugins.md`
- `docs/guides/webhook-action-schema.md`
- `docs/guides/github-issue-triage.md`
- `docs/guides/cli-commands.md`
- `docs/guides/http-endpoints.md`

**Research context:** The current capability entrypoints are under the MCP tools package:
`src/gobby/mcp_proxy/tools/internal.py`.
This capability has no folded standalone entrypoint; existing guides and registered operations supply its initial inventory.
The relevant guides are integrations, hook-schemas, ghook-user-guide, webhooks-and-plugins, webhook-action-schema, github-issue-triage. The catalog contract from 1.3
uses exact relative file paths and supplies all menu metadata; each topic is a separate
load and never satisfies a sibling reference requirement.

Required coverage: Hooks, webhooks, plugins and external integration setup.
Author the named overview and topic files. Read each relevant guide section and check its
commands, arguments, defaults, state transitions, ownership and recovery against current
implementation, registered schemas and focused tests. Audit shared CLI and HTTP guide
sections only for this capability; order all changes through the dependency above.
Mark operator-only operations explicitly. For active rules/profiles/workflows, inspect
installed rows and record provenance rather than inferring enabled state from templates.

Record in the capability's audit JSON: capability, source skills, guide path/anchor,
operation surface (MCP service/tool or CLI command), reference path, implementation
file/symbol, verification command/result, and discrepancies with resolution. Include every
public operation belonging to this capability; shared-service tools are assigned by actual
behavior. Preserve normative conflicts as explicit defect findings and resolve through the
found-work workflow. Mutating examples use isolated fixtures or temporary test daemons.
Correct affected guide prose and examples in this same leaf, update links, and avoid
copying entire guides or tool schemas. Register the capability and all topics in the catalog.

Verification: focused library/coverage checks introduced in P3, Markdown link/anchor
validation, and operation-specific test or isolated-probe evidence recorded in the audit.
Before that checker exists, parse JSON and validate this catalog entry through 1.3's
validator; keep executed evidence separate from planned checks.

**Acceptance:**
- 2.23.1 - The overview and named operation topics preserve all listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/integrations/overview.md`.
- 2.23.2 - Relevant guide sections and public operations have concrete implementation/schema/test evidence, with stale instructions corrected. file: `docs/reference-audit/integrations.json`.
- 2.23.3 - Catalog topics resolve to the authored files and examples distinguish agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.

## P3: Catalog routing and executable coverage
`kind: framing`

### 3.1 Route capability and standalone invocations consistently [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/skills/capability_routing.py`
- `src/gobby/hooks/event_handlers/_agent.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/hooks/event_handlers/_tool.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/skills/gobby/SKILL.md`
- `src/gobby/install/shared/prompts/agent/help-content.md`
- `src/gobby/cli/installers/skill_install.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_gobby_skill_router.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_capability_routing.py`

**Research context:** AgentEventHandlerMixin._intercept_skill_command and
_generate_help_content own parsing and dynamic help. ToolEventHandlerMixin resolves
native Skill calls separately. install_router_skills_as_commands and
install_router_skills_as_cli_skills carry the bundled router into providers.
Introduce a shared route_gobby_request helper returning menu text or explicit load
directives; hook and installation surfaces consume the same catalog contract.

Bare gobby/help renders capability descriptions plus currently installed standalone skills.
Capability alone loads its overview. Capability references renders topic descriptions and
exact provider-correct invocation examples without loading bodies. Capability references
topic loads exactly that topic. Capability request loads its overview and condition-matched
guidance and continues the original request; preserve the original trailing request rather
than copying it into injected context. Skill name/arguments and explicit skill name/arguments
resolve installed standalone skills, preserving level selection. Unknown names return
available choices and do no work. Capability collisions resolve to capability; explicit
singular skill escapes to standalone. Plural skills is always the capability.

Preserve project override resolution, active-skill filtering and internal default visibility.
MCP membership cannot contribute a standalone skill. Menus remain instruction-neutral.
The on-disk router explains loading and dynamic discovery, never embeds the library.

**Granularity:** The route contract spans provider interception and carriers atomically;
catalog validation and library content already close independently in prior phases.
Focused validation: routing tests plus existing hook and installer tests, covering both
dollar and supported slash/colon syntax.

**Acceptance:**
- 3.1.1 - Menus, overviews, topics, requests, collisions and unknown names follow the routing matrix. test: `tests/skills/test_capability_routing.py`.
- 3.1.2 - Standalone arguments, levels, visibility and project overrides are preserved. test: `tests/skills/test_gobby_skill_router.py`.
- 3.1.3 - Prompt interception and provider carriers derive from the same catalog and never inline unrequested bodies. file: `src/gobby/install/shared/skills/gobby/SKILL.md`.

### 3.2 Enforce public-operation and verified-guide coverage [category: test] (depends: 3.1)
`kind: deliverable`

Targets:
- `tests/skills/test_reference_library.py`
- `tests/skills/reference_library_helpers.py`
- `docs/reference-audit/coverage-policy.md`

**Research context:** The capability audit files added in P2 associate public tool and CLI
operations with exact topics. Existing registry factories expose tool inventories, and
Click commands expose supported CLI surfaces. Use isolated registry fixtures and
non-mutating CLI introspection to derive the complete current inventory; do not hardcode
the conversational draft's count of 21 services if registered membership differs.

Define audit JSON validation and reference/guide-anchor checking in the new helpers.
Require every public tool in every internal service to appear in at least one capability
audit, with an exact reference path. Require supported CLI-only commands, including
operator-only actions, to be represented. Exclude private helpers and hidden implementation
commands with an explicit documented policy, never by silently dropping failures.
Verify every referenced guide section has audit evidence. Check local Markdown links and
anchors, topic catalog completeness, absorbed-skill obligations and schema-first loading.
Representative scenarios cover tasks; planning/build; agent/handoff; pipelines; workspaces;
config revision conflicts; oversized paginated results. Run mutations only in isolated
fixtures. This is an independently useful regression contract for future public operations.

**Acceptance:**
- 3.2.1 - Coverage checks fail for unmapped public tools, supported CLI-only operations or unaudited guide links. test: `tests/skills/test_reference_library.py`.
- 3.2.2 - Representative workflows and catalog/reference/anchor checks pass using isolated fixtures. test: `tests/skills/test_reference_library.py`.

## P4: Enforcement and workflow migration
`kind: framing`

### 4.1 Require operation-specific references at existing enforcement points [category: config] (depends: P3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/AGENTS.md`
- `src/gobby/install/shared/workflows/rules/build-coordinator/require-build-coordinator-for-gobby-build.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/context-handoff/block-tools-after-handoff-compact.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/context-handoff/nudge-compact-on-context-pressure.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/context-handoff/require-handoff-discipline.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/development-discipline/require-development-discipline-skill.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/guard-plan-memory-writes.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/layered-memory-guidance.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-enhancer-lessons.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/review-learning/inject-plan-reviewer-lessons.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/review-learning/inject-planner-lessons.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/review-learning/inject-qa-reviewer-lessons.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/review-learning/inject-review-lessons-for-touched-files.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/reviewer-lifecycle/terminal-verdict-after-validation.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/skill-discovery/bootstrap-default-agent-core-skills.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/skill-discovery/require-plan-skill.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/rules/task-enforcement/require-tasks-skill-for-mutations.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/engine/test_skill_load_guidance.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_developer_guidance_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_handoff_discipline_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_skill_discovery_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_skill_loaded_call_tool_path.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/mcp_proxy/tools/tasks/_live_session_label.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/mcp_proxy/tools/test_live_session_label.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/hooks/dispatchers/mcp.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/workflows/engine/templating.py::*` — scope-reason: migrate the instruction contract throughout this file

**Research context:** Bundled first-use and bootstrap rules currently call skill_loaded
and skill_fetch_directive for retired names. The identity helpers from 1.1 accept exact
reference requirements; complete tracking from 1.2 prevents menus from satisfying them.
_live_session_label currently directly checks loaded_skills for live-session.

Replace loading-skills bootstrap with gobby loading guidance in the skills/loading reference,
retain memory bootstrap through memory/overview, and retain standalone brevity/restraint.
Map handoff gates to sessions/handoffs, task mutations to tasks/overview and closing-specific
instructions to tasks/closing; map remaining gates to the operation topic that preserves
their existing obligations. Change reasons and Jinja helper use together. Preserve event
timing, failure-open behavior of failed discovery where already defined, and authorization
semantics. Do not add blanket gates for new capabilities. Installed state is checked at
cutover, never declared from these templates.

**Granularity:** This leaf is a mechanical migration of the existing first-use enforcement
contract, with no new enforcement lifecycle. Workflow definitions and persisted rows remain
separate leaves. Run the targeted rule and live-session tests after final edits.

**Acceptance:**
- 4.1.1 - Existing bootstrap and first-use triggers request exact references with unchanged timing and failure behavior. test: `tests/workflows/test_skill_discovery_rules.py`.
- 4.1.2 - Reference gates reject router/menu-only loads and accept completed operation topics. test: `tests/workflows/test_task_enforcement_rules.py`.
- 4.1.3 - Live-session labeling consumes the tasks/live-work requirement. test: `tests/mcp_proxy/tools/test_live_session_label.py`.

### 4.2 Migrate agent and workflow instruction requirements [category: config] (depends: 4.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/prompts/expansion/system.md`
- `src/gobby/install/shared/prompts/expansion/user.md`
- `src/gobby/install/shared/registry/stages.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/backend-developer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/default.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/feedback-reviewer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/frontend-developer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/fullstack-developer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/planner.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/workflows/review.yaml::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_agents_sync.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_epic_reviewer_definition.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_plan_adversary_loads_plan_review.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_plan_enhancer_agents.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_planner_loads_plan_draft.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/agents/test_qa_reviewer_definition.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/hooks/grok_pending_context.py::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/workflows/engine/delivery_formatting.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/hooks/test_grok_pending_context.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: migrate the instruction contract throughout this file

**Research context:** Agent definitions and stage prompts carry additional_skills and
required skill-load instructions; pipeline and recovery delivery also name folded skills.
Existing requirement lists now support exact references. No replacement metadata field is
needed. The stage registry remains the authority for build stage order.

Convert Gobby-owned definitions, workflow requirements, recovery directives and hook
injections to operation-specific references after P2 provides them. Preserve all existing
planning/review/expansion, development and handoff obligations. Keep standalone methodology
requirements and levels. Read the dispatch instructions before changing stage-related
templates; do not alter stage semantics or grant new execution authority.

**Granularity:** One mechanical instruction-reference migration across bundled autonomous
consumers; rule enforcement and persisted migration are distinct deliverables. Focused
agent-definition, dispatch prompt and workflow tests verify each changed carrier.

**Acceptance:**
- 4.2.1 - Agent definitions and workflow consumers resolve only current standalone names or exact reference identities. test: `tests/workflows/test_workflows_agent_definitions.py`.
- 4.2.2 - Planning, build, review and recovery prompts retain their obligations with valid topic loads. test: `tests/workflows/test_planner_grammar_prompt.py`.

### 4.3 Update retained methods and documentation consumers [category: docs] (depends: 4.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/bridge/SKILL.md`
- `src/gobby/install/shared/skills/code-review/SKILL.md`
- `src/gobby/install/shared/skills/coderabbit/SKILL.md`
- `src/gobby/install/shared/skills/impeccable/references/live-contract.md`
- `src/gobby/install/shared/skills/impeccable/scripts/live-resume.mjs::*` — scope-reason: migrate the instruction contract throughout this file
- `src/gobby/install/shared/skills/proportionality/SKILL.md`
- `AGENTS.md`
- `docs/contracts/plan-coverage.md`
- `docs/contracts/session-boundary.md`
- `tests/docs/test_claude_md_contract_section.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_coderabbit_skill.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_repository_maintenance_skill.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_code_review_skill.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_bridge_skill.py::*` — scope-reason: migrate the instruction contract throughout this file

**Research context:** Retained standalone methods link to folded procedures. The root agent
instructions and plan/session normative contracts also name existing skill entrypoints.
Replace Gobby-specific operational directions with exact capability reference loads while
retaining reusable methodology, internal visibility, provider-specific behavior and levels.
Do not rewrite normative semantics to match defects. Update current documentation links;
historical completed plans and commit records remain historical evidence.

For retained impeccable resources, load the design skill before editing and preserve its
design contract. Change reference routing only; this task is not a UI redesign.
Verify no current retained consumer depends on a retired entrypoint.

**Acceptance:**
- 4.3.1 - Retained methods keep reusable guidance and resolve Gobby-specific procedures through references. test: `tests/skills/test_coderabbit_skill.py`.
- 4.3.2 - Current root instructions and normative plan/session contracts name valid loading paths without changing obligations. file: `AGENTS.md`.

## P5: Installation and cutover
`kind: framing`

### 5.1 Migrate persisted Gobby-owned requirements safely [category: code] (depends: P4)
`kind: deliverable`

Targets:
- `src/gobby/skills/reference_migration.py`
- `src/gobby/skills/sync.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/storage/test_skill_sync.py::*` — scope-reason: migrate the instruction contract throughout this file
- `tests/skills/test_reference_migration.py`

**Research context:** sync_bundled_skills already refreshes installed content, preserves
non-Gobby-owned skills and soft-deletes absent bundled rows after complete successful loading.
Reuse normal synchronization for retirement. Add a bounded idempotent requirement conversion
step for Gobby-owned persisted instruction lists; do not mutate unrelated or user-owned
rows. Use the catalog's explicit folded-name mapping to exact operation references.

Inspect ownership and serialization at each persisted consumer before conversion.
Convert only fields whose semantics are instruction requirements; never replace arbitrary
strings, task titles or historical prose. For user/project-owned requirements that still
name retired bundled skills, return an actionable diagnostic naming the row, field and
exact replacement; preserve that row. Preserve custom skills and explicit overrides.
Failure leaves diagnosable state and rerun completes safely without duplicate changes.
No raw SQL or REST task lifecycle changes; task requirement updates use lifecycle services.

**Granularity:** One idempotent upgrade contract. Physical entrypoint removal and live
cutover belong to 5.2 after isolated migration verification.
Focused tests cover fresh empty state, installed bundled state, user overrides, malformed
requirements, partial failure/retry and repeated synchronization.

**Acceptance:**
- 5.1.1 - Gobby-owned persisted instruction requirements convert idempotently to exact references. test: `tests/skills/test_reference_migration.py`.
- 5.1.2 - Custom skills and user-owned requirements remain intact and incompatible requirements receive actionable replacements. test: `tests/skills/test_reference_migration.py`.

### 5.2 Retire folded entrypoints and verify fresh install and upgrade [category: code] (depends: 5.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/build-coordinator/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/build-coordinator/references/terminal-sandbox-evidence.md` — operation: delete
- `src/gobby/install/shared/skills/build-rule/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/build-rule/references/common-patterns.md` — operation: delete
- `src/gobby/install/shared/skills/build-rule/references/effects.md` — operation: delete
- `src/gobby/install/shared/skills/build-rule/references/events-and-conditions.md` — operation: delete
- `src/gobby/install/shared/skills/build/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/channel-parity/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/clones/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/code-index/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/development-discipline/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/epic-review/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/expand/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/expansion-agent-selection/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/handoff-discipline/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/intro/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/live-session/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/loading-skills/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/mcp-servers/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/memory/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/merge-expert/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/merge/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/persona/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/pipelines-and-cron/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan-draft/references/plan-coverage-grammar.md` — operation: delete
- `src/gobby/install/shared/skills/plan-draft/references/targets-and-consumers.md` — operation: delete
- `src/gobby/install/shared/skills/plan-draft/references/task-structure.md` — operation: delete
- `src/gobby/install/shared/skills/plan-draft/references/verification-and-revision.md` — operation: delete
- `src/gobby/install/shared/skills/plan-enhance/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan-mechanic/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan-review/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan-review/references/approval-and-handoff.md` — operation: delete
- `src/gobby/install/shared/skills/plan-review/references/deterministic-gate.md` — operation: delete
- `src/gobby/install/shared/skills/plan-review/references/findings-and-repairs.md` — operation: delete
- `src/gobby/install/shared/skills/plan-review/references/traceability-and-coverage.md` — operation: delete
- `src/gobby/install/shared/skills/plan/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/plan/references/adversarial-review.md` — operation: delete
- `src/gobby/install/shared/skills/plan/references/build-handoff.md` — operation: delete
- `src/gobby/install/shared/skills/plan/references/drafting-and-staging.md` — operation: delete
- `src/gobby/install/shared/skills/plan/references/evidence-and-recovery.md` — operation: delete
- `src/gobby/install/shared/skills/plan/references/work-routing.md` — operation: delete
- `src/gobby/install/shared/skills/review-learning/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/review/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/source-control/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/tasks/SKILL.md` — operation: delete
- `src/gobby/install/shared/skills/tasks/references/creation.md` — operation: delete
- `src/gobby/install/shared/skills/tasks/references/no-work-closures.md` — operation: delete
- `src/gobby/install/shared/skills/tasks/references/review-flows.md` — operation: delete
- `src/gobby/install/shared/skills/writing-skills/SKILL.md` — operation: delete
- `tests/agents/test_agents_sync.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_epic_reviewer_definition.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_merge_orchestrator_contract.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_plan_adversary_internal_research_definition.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_plan_adversary_loads_plan_review.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_plan_adversary_taskless_definition.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_plan_enhancer_agents.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_planner_loads_plan_draft.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/agents/test_qa_reviewer_definition.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/build/test_child_merge_repair.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/build_pipeline/test_build_pipeline_build_profiles.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/cli/test_cli_build.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_bundled_agent_contract.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_delivery_chain.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_dispatch_prompts.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_planning_enhancement.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_rules_stage_native.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/dispatch/test_spawn_isolation.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/docs/test_claude_md_contract_section.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/e2e/test_build_dispatcher_autonomy.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/e2e/test_review_learning_e2e.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/fixtures/provider_contracts/agy/daemon-receipts.jsonl`
- `tests/framing_corpus.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_grok_pending_context.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_inline_mcp_dispatcher.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_session_lookup_metadata.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_staged_effects_receipt_route.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/hooks/test_tool_handlers.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/services/test_call_tool_session_id_context.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/test_mcp_proxy_stdio.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/test_registries.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/test_live_session_label.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/test_plan_review_approval.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/test_plan_review_backfill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/mcp_proxy/tools/test_review_learning.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/review_evidence_helpers.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_covers.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_plans_parser.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_review_evidence_io.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_review_repairs_apply.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_review_repairs_service.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/plans/test_review_repairs_validation.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_coverage_helpers.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_class_recall.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_feedback_loop_e2e.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_file_paths.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_lessons.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_retirement.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/review_learning/test_storage_contract.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/servers/websocket/chat/test_plan_skill_delivery.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/servers/websocket/chat/test_runtime_manager.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/build-coordinator/unattended-build-coordination.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/development-discipline/no-python-suppressions.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/development-discipline/research-moved-symbol.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/development-discipline/research-unchanged.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/merge-expert/page-terminal-capture.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/pipelines-and-cron/select-automation-path.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/plan-draft/preserve-research.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/plan-mechanic/bounded-repair.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/review-learning/record-confirmed-lessons.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/writing-skills/compact-decomposition.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/scenarios/writing-skills/create-discipline-skill.yaml::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_bridge_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_build_coordinator_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_build_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_code_review_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_coderabbit_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_epic_review_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_live_session_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_loading_skills_content.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_pipelines_and_cron_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_adversary_rejection.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_draft_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_draft_stage_list.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_enhance_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_mechanic_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_research_context.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_review_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_skill_delegated_mode.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_plan_skill_grammar.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_python_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_removed_wait_tool_guidance.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_repository_maintenance_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_review_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_skill_tdd_harness.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_skills_formatting.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_tasks_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_writing_skills_skill.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/tasks/test_epic_workspace_bound.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/tasks/test_lifecycle_events.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/tasks/test_live_session_recovery.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/tasks/test_stage_states.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/tasks/test_sweep_stale_claims.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/test_skill_sync.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/test_stage_review_findings.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/storage/test_storage_build_profiles.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/tasks/test_acceptance_artifacts.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/tasks/test_plan_gate.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/engine/test_evaluation.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/engine/test_skill_load_guidance.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_agent_models.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_agent_monitoring_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_agent_workflow_completion.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_condition_helpers.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_delivery_disposition.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_delivery_pipeline.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_developer_guidance_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_handoff_discipline_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_hooks.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_pipeline_renderer.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_plan_mode_delivery.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_plan_mode_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_planner_grammar_prompt.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_retired_bundled_definitions.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_review_learning_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_review_workflow.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_reviewer_terminal_verdict_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_rewrite_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_rule_engine.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_rule_engine_task_helper_wiring.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_skill_discovery_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_skill_loaded_call_tool_path.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_step_enforcement.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/workflows/test_workflows_agent_definitions.py::*` — scope-reason: switch existing skill fixtures and assertions to exact migrated references
- `tests/skills/test_reference_installation.py`
- `tests/install/test_bundled_content_manifest.py::*` — scope-reason: verify derived integrity metadata for the new bundle
- `docs/reference-audit/cutover.md`

**Research context:** Existing sync already removes orphaned Gobby-owned entrypoints.
The current checkout derives bundled integrity metadata through install/manifest.py; the
previously tracked bundled_content_manifest.json is no longer a tracked source artifact.
Use the current generation/build flow and verify its output rather than reinstating a
retired tracked manifest. Provider carriers were migrated in 3.1.

Remove exactly the 30 folded bundled skill directories after all references and consumers
exist. No compatibility wrappers. Adapt existing behavioral tests and scenario fixtures to
load the new references while retaining their assertions about operating obligations.
Preserve retained skills, annotate and all custom/override material.
Rehearse fresh install and upgrade against the isolated test hub and temporary provider
homes. Validate full reference file installation, source ownership, retired row cleanup,
user-owned requirement diagnostics and repeat synchronization.

Coordinate shared live cutover with active sessions using project messaging and an
event-driven quiet window. Inspect current uncommitted templates before synchronization.
Use main-checkout daemon lifecycle commands only, and verify installed catalog files,
retired rows, preserved custom skills and the actual active bootstrap rules after cutover.
Record commands and observed installed-row evidence in cutover.md. A pending foreign edit
requires owner coordination; never publish that edit unknowingly.

**Granularity:** Physical removal, integrity generation and adapted historical fixtures
are one atomic cutover of the entrypoint set. The persistence algorithm was separately
verified in 5.1. Fresh/upgrade tests belong here because they validate this exact bundle.

**Acceptance:**
- 5.2.1 - Exactly the folded source entrypoints are retired with no wrappers and all retained/custom skills preserved. test: `tests/skills/test_reference_installation.py`.
- 5.2.2 - Fresh installation, upgrade, repeated sync and derived integrity metadata pass using isolated state. test: `tests/skills/test_reference_installation.py`.
- 5.2.3 - Existing migrated behavior/scenario checks pass with current reference paths and obligations intact. test: `tests/skills/test_reference_library.py`.
- 5.2.4 - Coordinated live cutover verifies installed ownership, retirement, bootstrap and catalog rows. file: `docs/reference-audit/cutover.md`.

## V1 Verification and completion
`kind: verification`

Run uv run gobby plans validate .gobby/plans/gobby-reference-library.md before deriving
the approved manifest. The user's implement/proceed instructions authorize execution;
use the normal human-handoff derive/apply path and expansion-mode validation, without
fabricating adversarial approval. Register against the real implementation root after
expansion; #22167 owns the validated planning artifact, not the implementation epic.

Focused verification includes routing, complete and paginated loading, stale cursors,
independent skill/reference tracking, context resets, exact gates, complete public-tool
and CLI coverage, audited guide links, fresh install and upgrade, and representative
task/planning/build/agent/handoff/pipeline/workspace/config/oversized-result flows.
Use the isolated test hub and temporary daemon/provider state for mutations.
Run category-appropriate lint/type checks scoped to changed files and focused tests.
Commit task-attributed changes and close each deliverable through the normal close gate.
No full pytest suite without an explicit user request.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Resolve and enforce exact instruction identities
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Shared parser validates names and reference paths and
    rejects traversal and malformed identities. file: `src/gobby/skills/instruction_requirements.py`.

    1.1.2: Requirement consumers accept exact completed references and reject router-only,
    sibling-topic and absent loads. test: `tests/skills/test_instruction_requirements.py`.

    1.1.3: Plain skills, level preservation and schema-first directives retain their
    existing behavior. test: `tests/skills/test_skills_formatting.py`.'
  labels:
  - covers:gobby-reference-library:1.1:1.1.1
  - covers:gobby-reference-library:1.1:1.1.2
  - covers:gobby-reference-library:1.1:1.1.3
  tdd: false
  source_section: '1.1'
  implementation_domain: backend
- title: Track complete reference delivery and reset it with context
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: Complete direct and wrapped reference retrieval records
    only the exact identity. test: `tests/skills/test_reference_load_tracking.py`.

    1.2.2: Partial, failed, stale, menu and listing responses do not satisfy reference
    requirements. test: `tests/mcp_proxy/tools/skills/test_skill_delivery.py`.

    1.2.3: Context resets clear completed references alongside skills while preserving
    existing level rules. test: `tests/workflows/test_language_skill_reset_rules.py`.'
  labels:
  - covers:gobby-reference-library:1.2:1.2.1
  - covers:gobby-reference-library:1.2:1.2.2
  - covers:gobby-reference-library:1.2:1.2.3
  tdd: false
  source_section: '1.2'
  implementation_domain: backend
- title: Define the bundled capability catalog and validator
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: One catalog contract carries capability and topic metadata
    and validates all reference paths. file: `src/gobby/skills/capability_catalog.py`.

    1.3.2: Duplicate identifiers, missing files and unsafe paths fail with actionable
    diagnostics. test: `tests/skills/test_capability_catalog.py`.'
  labels:
  - covers:gobby-reference-library:1.3:1.3.1
  - covers:gobby-reference-library:1.3:1.3.2
  tdd: false
  source_section: '1.3'
  implementation_domain: backend
- title: Author and verify tasks guidance
  category: docs
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.1.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/tasks/overview.md`.

    2.1.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/tasks.json`.

    2.1.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.1:2.1.1
  - covers:gobby-reference-library:2.1:2.1.2
  - covers:gobby-reference-library:2.1:2.1.3
  tdd: false
  source_section: '2.1'
  assigned_agent: tech-writer
- title: Author and verify plan guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: '2.2.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/plan/overview.md`.

    2.2.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/plan.json`.

    2.2.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.2:2.2.1
  - covers:gobby-reference-library:2.2:2.2.2
  - covers:gobby-reference-library:2.2:2.2.3
  tdd: false
  source_section: '2.2'
  assigned_agent: tech-writer
- title: Author and verify build guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.3.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/build/overview.md`.

    2.3.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/build.json`.

    2.3.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.3:2.3.1
  - covers:gobby-reference-library:2.3:2.3.2
  - covers:gobby-reference-library:2.3:2.3.3
  tdd: false
  source_section: '2.3'
  assigned_agent: tech-writer
- title: Author and verify sessions guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: '2.4.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/sessions/overview.md`.

    2.4.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/sessions.json`.

    2.4.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.4:2.4.1
  - covers:gobby-reference-library:2.4:2.4.2
  - covers:gobby-reference-library:2.4:2.4.3
  tdd: false
  source_section: '2.4'
  assigned_agent: tech-writer
- title: Author and verify agents guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.4'
  validation_criteria: '2.5.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/agents/overview.md`.

    2.5.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/agents.json`.

    2.5.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.5:2.5.1
  - covers:gobby-reference-library:2.5:2.5.2
  - covers:gobby-reference-library:2.5:2.5.3
  tdd: false
  source_section: '2.5'
  assigned_agent: tech-writer
- title: Author and verify memory guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.5'
  validation_criteria: '2.6.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/memory/overview.md`.

    2.6.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/memory.json`.

    2.6.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.6:2.6.1
  - covers:gobby-reference-library:2.6:2.6.2
  - covers:gobby-reference-library:2.6:2.6.3
  tdd: false
  source_section: '2.6'
  assigned_agent: tech-writer
- title: Author and verify code-index guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.6'
  validation_criteria: '2.7.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/code-index/overview.md`.

    2.7.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/code-index.json`.

    2.7.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.7:2.7.1
  - covers:gobby-reference-library:2.7:2.7.2
  - covers:gobby-reference-library:2.7:2.7.3
  tdd: false
  source_section: '2.7'
  assigned_agent: tech-writer
- title: Author and verify skills guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.7'
  validation_criteria: '2.8.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/skills/overview.md`.

    2.8.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/skills.json`.

    2.8.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.8:2.8.1
  - covers:gobby-reference-library:2.8:2.8.2
  - covers:gobby-reference-library:2.8:2.8.3
  tdd: false
  source_section: '2.8'
  assigned_agent: tech-writer
- title: Author and verify rules guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.8'
  validation_criteria: '2.9.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/rules/overview.md`.

    2.9.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/rules.json`.

    2.9.3: Catalog topics resolve to the authored files and examples distinguish agent-supported
    and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.9:2.9.1
  - covers:gobby-reference-library:2.9:2.9.2
  - covers:gobby-reference-library:2.9:2.9.3
  tdd: false
  source_section: '2.9'
  assigned_agent: tech-writer
- title: Author and verify mcp-servers guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.9'
  validation_criteria: '2.10.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/mcp-servers/overview.md`.

    2.10.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/mcp-servers.json`.

    2.10.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.10:2.10.1
  - covers:gobby-reference-library:2.10:2.10.2
  - covers:gobby-reference-library:2.10:2.10.3
  tdd: false
  source_section: '2.10'
  assigned_agent: tech-writer
- title: Author and verify pipelines guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.10'
  validation_criteria: '2.11.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/pipelines/overview.md`.

    2.11.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/pipelines.json`.

    2.11.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.11:2.11.1
  - covers:gobby-reference-library:2.11:2.11.2
  - covers:gobby-reference-library:2.11:2.11.3
  tdd: false
  source_section: '2.11'
  assigned_agent: tech-writer
- title: Author and verify source-control guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.11'
  validation_criteria: '2.12.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/source-control/overview.md`.

    2.12.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/source-control.json`.

    2.12.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.12:2.12.1
  - covers:gobby-reference-library:2.12:2.12.2
  - covers:gobby-reference-library:2.12:2.12.3
  tdd: false
  source_section: '2.12'
  assigned_agent: tech-writer
- title: Author and verify review guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.12'
  validation_criteria: '2.13.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/review/overview.md`.

    2.13.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/review.json`.

    2.13.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.13:2.13.1
  - covers:gobby-reference-library:2.13:2.13.2
  - covers:gobby-reference-library:2.13:2.13.3
  tdd: false
  source_section: '2.13'
  assigned_agent: tech-writer
- title: Author and verify intro guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.13'
  validation_criteria: '2.14.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/intro/overview.md`.

    2.14.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/intro.json`.

    2.14.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.14:2.14.1
  - covers:gobby-reference-library:2.14:2.14.2
  - covers:gobby-reference-library:2.14:2.14.3
  tdd: false
  source_section: '2.14'
  assigned_agent: tech-writer
- title: Author and verify development guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.14'
  validation_criteria: '2.15.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/development/overview.md`.

    2.15.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/development.json`.

    2.15.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.15:2.15.1
  - covers:gobby-reference-library:2.15:2.15.2
  - covers:gobby-reference-library:2.15:2.15.3
  tdd: false
  source_section: '2.15'
  assigned_agent: tech-writer
- title: Author and verify variables guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.15'
  validation_criteria: '2.16.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/variables/overview.md`.

    2.16.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/variables.json`.

    2.16.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.16:2.16.1
  - covers:gobby-reference-library:2.16:2.16.2
  - covers:gobby-reference-library:2.16:2.16.3
  tdd: false
  source_section: '2.16'
  assigned_agent: tech-writer
- title: Author and verify config guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.16'
  validation_criteria: '2.17.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/config/overview.md`.

    2.17.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/config.json`.

    2.17.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.17:2.17.1
  - covers:gobby-reference-library:2.17:2.17.2
  - covers:gobby-reference-library:2.17:2.17.3
  tdd: false
  source_section: '2.17'
  assigned_agent: tech-writer
- title: Author and verify hub guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.17'
  validation_criteria: '2.18.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/hub/overview.md`.

    2.18.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/hub.json`.

    2.18.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.18:2.18.1
  - covers:gobby-reference-library:2.18:2.18.2
  - covers:gobby-reference-library:2.18:2.18.3
  tdd: false
  source_section: '2.18'
  assigned_agent: tech-writer
- title: Author and verify communications guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.18'
  validation_criteria: '2.19.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/communications/overview.md`.

    2.19.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/communications.json`.

    2.19.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.19:2.19.1
  - covers:gobby-reference-library:2.19:2.19.2
  - covers:gobby-reference-library:2.19:2.19.3
  tdd: false
  source_section: '2.19'
  assigned_agent: tech-writer
- title: Author and verify voice guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.19'
  validation_criteria: '2.20.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/voice/overview.md`.

    2.20.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/voice.json`.

    2.20.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.20:2.20.1
  - covers:gobby-reference-library:2.20:2.20.2
  - covers:gobby-reference-library:2.20:2.20.3
  tdd: false
  source_section: '2.20'
  assigned_agent: tech-writer
- title: Author and verify observability guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.20'
  validation_criteria: '2.21.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/observability/overview.md`.

    2.21.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/observability.json`.

    2.21.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.21:2.21.1
  - covers:gobby-reference-library:2.21:2.21.2
  - covers:gobby-reference-library:2.21:2.21.3
  tdd: false
  source_section: '2.21'
  assigned_agent: tech-writer
- title: Author and verify admin guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.21'
  validation_criteria: '2.22.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/admin/overview.md`.

    2.22.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/admin.json`.

    2.22.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.22:2.22.1
  - covers:gobby-reference-library:2.22:2.22.2
  - covers:gobby-reference-library:2.22:2.22.3
  tdd: false
  source_section: '2.22'
  assigned_agent: tech-writer
- title: Author and verify integrations guidance
  category: docs
  task_type: feature
  depends_on:
  - '2.22'
  validation_criteria: '2.23.1: The overview and named operation topics preserve all
    listed obligations and loading/recovery instructions. file: `src/gobby/install/shared/skills/gobby/references/integrations/overview.md`.

    2.23.2: Relevant guide sections and public operations have concrete implementation/schema/test
    evidence, with stale instructions corrected. file: `docs/reference-audit/integrations.json`.

    2.23.3: Catalog topics resolve to the authored files and examples distinguish
    agent-supported and operator-only actions. file: `src/gobby/install/shared/skills/gobby/catalog.json`.'
  labels:
  - covers:gobby-reference-library:2.23:2.23.1
  - covers:gobby-reference-library:2.23:2.23.2
  - covers:gobby-reference-library:2.23:2.23.3
  tdd: false
  source_section: '2.23'
  assigned_agent: tech-writer
- title: Route capability and standalone invocations consistently
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  - '2.2'
  - '2.3'
  - '2.4'
  - '2.5'
  - '2.6'
  - '2.7'
  - '2.8'
  - '2.9'
  - '2.10'
  - '2.11'
  - '2.12'
  - '2.13'
  - '2.14'
  - '2.15'
  - '2.16'
  - '2.17'
  - '2.18'
  - '2.19'
  - '2.20'
  - '2.21'
  - '2.22'
  - '2.23'
  validation_criteria: '3.1.1: Menus, overviews, topics, requests, collisions and
    unknown names follow the routing matrix. test: `tests/skills/test_capability_routing.py`.

    3.1.2: Standalone arguments, levels, visibility and project overrides are preserved.
    test: `tests/skills/test_gobby_skill_router.py`.

    3.1.3: Prompt interception and provider carriers derive from the same catalog
    and never inline unrequested bodies. file: `src/gobby/install/shared/skills/gobby/SKILL.md`.'
  labels:
  - covers:gobby-reference-library:3.1:3.1.1
  - covers:gobby-reference-library:3.1:3.1.2
  - covers:gobby-reference-library:3.1:3.1.3
  tdd: false
  source_section: '3.1'
  implementation_domain: backend
- title: Enforce public-operation and verified-guide coverage
  category: test
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: '3.2.1: Coverage checks fail for unmapped public tools, supported
    CLI-only operations or unaudited guide links. test: `tests/skills/test_reference_library.py`.

    3.2.2: Representative workflows and catalog/reference/anchor checks pass using
    isolated fixtures. test: `tests/skills/test_reference_library.py`.'
  labels:
  - covers:gobby-reference-library:3.2:3.2.1
  - covers:gobby-reference-library:3.2:3.2.2
  tdd: false
  source_section: '3.2'
  assigned_agent: backend-developer
- title: Require operation-specific references at existing enforcement points
  category: config
  task_type: feature
  depends_on:
  - '3.1'
  - '3.2'
  validation_criteria: '4.1.1: Existing bootstrap and first-use triggers request exact
    references with unchanged timing and failure behavior. test: `tests/workflows/test_skill_discovery_rules.py`.

    4.1.2: Reference gates reject router/menu-only loads and accept completed operation
    topics. test: `tests/workflows/test_task_enforcement_rules.py`.

    4.1.3: Live-session labeling consumes the tasks/live-work requirement. test: `tests/mcp_proxy/tools/test_live_session_label.py`.'
  labels:
  - covers:gobby-reference-library:4.1:4.1.1
  - covers:gobby-reference-library:4.1:4.1.2
  - covers:gobby-reference-library:4.1:4.1.3
  tdd: false
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Migrate agent and workflow instruction requirements
  category: config
  task_type: feature
  depends_on:
  - '4.1'
  validation_criteria: '4.2.1: Agent definitions and workflow consumers resolve only
    current standalone names or exact reference identities. test: `tests/workflows/test_workflows_agent_definitions.py`.

    4.2.2: Planning, build, review and recovery prompts retain their obligations with
    valid topic loads. test: `tests/workflows/test_planner_grammar_prompt.py`.'
  labels:
  - covers:gobby-reference-library:4.2:4.2.1
  - covers:gobby-reference-library:4.2:4.2.2
  tdd: false
  source_section: '4.2'
  assigned_agent: backend-developer
- title: Update retained methods and documentation consumers
  category: docs
  task_type: feature
  depends_on:
  - '4.2'
  validation_criteria: '4.3.1: Retained methods keep reusable guidance and resolve
    Gobby-specific procedures through references. test: `tests/skills/test_coderabbit_skill.py`.

    4.3.2: Current root instructions and normative plan/session contracts name valid
    loading paths without changing obligations. file: `AGENTS.md`.'
  labels:
  - covers:gobby-reference-library:4.3:4.3.1
  - covers:gobby-reference-library:4.3:4.3.2
  tdd: false
  source_section: '4.3'
  assigned_agent: tech-writer
- title: Migrate persisted Gobby-owned requirements safely
  category: code
  task_type: feature
  depends_on:
  - '4.1'
  - '4.2'
  - '4.3'
  validation_criteria: '5.1.1: Gobby-owned persisted instruction requirements convert
    idempotently to exact references. test: `tests/skills/test_reference_migration.py`.

    5.1.2: Custom skills and user-owned requirements remain intact and incompatible
    requirements receive actionable replacements. test: `tests/skills/test_reference_migration.py`.'
  labels:
  - covers:gobby-reference-library:5.1:5.1.1
  - covers:gobby-reference-library:5.1:5.1.2
  tdd: false
  source_section: '5.1'
  implementation_domain: backend
- title: Retire folded entrypoints and verify fresh install and upgrade
  category: code
  task_type: feature
  depends_on:
  - '5.1'
  validation_criteria: '5.2.1: Exactly the folded source entrypoints are retired with
    no wrappers and all retained/custom skills preserved. test: `tests/skills/test_reference_installation.py`.

    5.2.2: Fresh installation, upgrade, repeated sync and derived integrity metadata
    pass using isolated state. test: `tests/skills/test_reference_installation.py`.

    5.2.3: Existing migrated behavior/scenario checks pass with current reference
    paths and obligations intact. test: `tests/skills/test_reference_library.py`.

    5.2.4: Coordinated live cutover verifies installed ownership, retirement, bootstrap
    and catalog rows. file: `docs/reference-audit/cutover.md`.'
  labels:
  - covers:gobby-reference-library:5.2:5.2.1
  - covers:gobby-reference-library:5.2:5.2.2
  - covers:gobby-reference-library:5.2:5.2.3
  - covers:gobby-reference-library:5.2:5.2.4
  tdd: false
  source_section: '5.2'
  implementation_domain: backend
```
