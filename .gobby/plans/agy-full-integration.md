Plan artifact: `.gobby/plans/agy-full-integration.md`

# AGY 1.1.18 Integration and Provider Consistency

**Plan ID:** agy-full-integration

## Status
`kind: framing`

**Resumed 2026-08-20 against installed AGY 1.1.16.** The plan is no longer
parked. Upstream `google-antigravity/antigravity-cli` issue #222 ("Hook
execution not working on Windows (same problem on MacOS too)") is still Open
(`stat:awaiting response`, last maintainer activity 2026-07-22/29), but it is
no longer this plan's gate: **the local probe is.** Gate 0 (§1.1) re-ran in
both print mode and interactive/tmux terminal mode (landing on 1.1.18, below);
approval waits on the adversarial rounds against the revised plan, not on
upstream. Task #19563 carried that run.

**Proven on 1.1.16 (print mode).** A print-mode turn (conversation
`384e2db9-cff0-437f-8170-cd116ad15d5a`) dispatched Gobby's registered hooks:
`PreInvocation`, `PreToolUse`, `PostInvocation`, and `Stop` each invoked the
hook command. AGY exposes exactly five hook events — those four plus
`PostToolUse` — and no `SessionStart`; the synthetic `SESSION_START` derived
from the first `PreInvocation` (§4.1) stands. Two Gobby-side defects that
blocked payload capture are fixed in `488f6c244c` (#20624): `ghook` now emits
AGY-legal skip JSON (`{}` / `{"decision":"allow"}`) instead of the
protojson-illegal `{"continue":true}`, and the project walk-up reads
`workspacePaths` because AGY runs hooks with cwd = the `hooks.json` directory,
not the workspace.

**Gate 0 executed 2026-08-22 (task #19563).** All 24 §1.1 records were
probed in both print mode and interactive/tmux mode; the outcome table lives
in `tests/fixtures/provider_contracts/agy/README.md` and §1.2 Run 2 mirrors
it. **The probed version is 1.1.18, not 1.1.16:** AGY's auto-updater replaced
the binary at 03:18:34 local, after the two `/hooks` captures and before the
first live turn, and there is no pin or downgrade path. By the plan's own rule
the floor moves to 1.1.18 (Constraints). Disproofs that changed downstream
sections: typed `MODEL/RUN_COMMAND` transcript records no longer exist — every
tool result is `MODEL/GENERIC` free text (§4.2); `transcriptPath` names
`transcript_full.jsonl` (§2.2, §4.2, §5.1); `agy models --output-format json`
is a usage error — the flag precedes the subcommand (§6.3);
`permissionOverrides` and `injectSteps.toolCall` are not honored (§4.1);
hook-denied or failed tools never produce `PostToolUse` (§4.1, §6.2); no
compaction or context-pressure signal exists (§5.2, §5.3); the stream-input
transport has no in-flight cancel — SIGINT kills the process (§5.2).

**Outstanding before approval.**
1. ~~Print-mode live probe~~ done: MCP, built-in, and shell tools dispatched
   all five hooks and the real `ghook` delivered every event to the daemon
   (`tests/fixtures/provider_contracts/agy/daemon-receipts.jsonl`: HTTP 200,
   processed marker, and `hooks.log` line per mode × tool class × event). The
   `source=agy` hooks.log line and the AGY session row cannot exist before
   §4.1 lands (today's adapter reads `session_id`/`cwd`/`tool_name`, none of
   which AGY sends); they are §4.1's acceptance evidence, see the Dispatch
   Evidence Gate.
2. ~~Interactive/tmux-mode dispatch~~ done (record 1.1.17: all five events,
   key sets identical to print mode).
3. ~~Gate 0 fixtures~~ committed under `tests/fixtures/provider_contracts/agy/`
   (stale 1.0.x fixtures deleted).
4. Adversarial Round 19 onward, against the revised plan. Round 18 is
   unfenced and stays unfinalized; do not finalize it.

**History (kept for the record).** The first Gate 0 run on 1.1.10 found two
defects. Gobby's `hooks.json` used a Claude-shaped `{"hooks": {...}}` wrapper
that 1.1.10 rejects; fixed in `230cb26ea` (#19566). With the format corrected,
no hook fired on 1.1.10, matching upstream #222; the plan parked on 2026-08-03.
No changelog entry explains why 1.1.16 dispatches (1.1.10's entry — "hooks
defined in hooks.json run before the built-in termination checks … lets Stop
hooks run at all" — is the closest), which is why this plan trusts the local
probe over the issue tracker in both directions: dispatch is accepted only
when observed, and #222's Open state does not veto observed dispatch.

## Overview
`kind: framing`

AGY (Antigravity CLI) is Gobby's only hook-only provider: it has a hook adapter but no
transcript parser, no web-chat backend, no spawn path, and no tool-chat binding.
`docs/research/cli-integration-matrix.md:124` records it as **Blocked** on the premise that
upstream exposes neither parseable transcripts nor a machine transport. **That premise is
now false.** AGY 1.1.16 ships `--output-format stream-json`, `--input-format stream-json`
(1.1.15: NDJSON prompts on stdin, one turn per message in a single conversation),
`--conversation` resume, per-conversation JSONL transcripts, and non-interactive
`-p "/hooks"` / `/usage` / `agy --output-format json models` introspection — and, as of
today, it dispatches Gobby's hooks.

Investigating that unblock exposed a second problem: the reason AGY was easy to leave
behind is that provider integration in Gobby is not uniform. Transcript parsers are
dispatched from five separate places, two of which silently fall back to the Claude parser.
Transcript discovery is hook-reported for three providers and disk-derived for two.
`critical_hooks` differs arbitrarily per CLI. Web chat never received the SRT sandbox
migration that spawn got. This epic therefore does two things: it makes AGY a complete
integration, and it normalizes the seams that made AGY's absence invisible.

**Verified against an installed binary** (not assumed). Items marked **[1.1.10]** were
confirmed by the first Gate 0 run; items marked **[1.1.16]** were observed on 2026-08-20;
unmarked items were observed on 1.1.9. The 2026-08-22 Gate 0 run re-confirmed or
disproved every one of them on **1.1.18** (the floor); where this overview and §1.2
Run 2 disagree, Run 2 and the fixture README are authoritative. In particular the
typed `RUN_COMMAND`/`VIEW_FILE`/`MCP_TOOL` record census below is 1.1.9 history: on
1.1.18 every tool result is `MODEL/GENERIC` (record 1.1.10). §1.2 records the
cumulative state:

- **[1.1.10]** Flags `--conversation`, `--output-format stream-json`, `--disable-slash-commands`,
  `--print-timeout`, `--model`, `--effort`, `--project`, `--add-dir` all exist; **[1.1.16]**
  additionally `--input-format`, `--json-schema`, `--mode accept-edits|plan`, `--log-file`,
  `--agent`, and subcommands `mcp add|remove|list|enable|disable`, `models`, `agents`,
  `changelog`.
- NDJSON constants `step_update`, `agent_response`, `text_delta`, `tool_info`,
  `permission_mode`, `num_turns` are present in the binary's string table.
- **[1.1.10]** Transcripts exist at `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`.
  Across 66 local conversations, `step_index` is unique, dense and monotonic in every one —
  the file is append-only, so incremental parsing is safe. **[1.1.16]** A sibling
  `transcript_full.jsonl` and chunked copies under
  `logs/chunks/{transcript,transcript_full}/00000000.jsonl` now exist beside it; record
  1.1.22 decides which file the parser consumes. Tool-call names at transcript level are
  snake_case (`list_dir`, `find_by_name`, `run_command`) with JSON-encoded string args.
- The real record set is **15** `source/type` combinations, not the 4 the source brief listed.
  Tool records include `VIEW_FILE` (50), `MCP_TOOL` (46), `LIST_DIRECTORY` (39),
  `GREP_SEARCH` (36), `SEARCH_WEB` (5), `CODE_ACTION` (2) and a `GENERIC` fallback (25).
  A parser keyed only on `RUN_COMMAND` would drop roughly 78% of tool records.
- Undocumented fields exist: `truncated_fields` (AGY self-truncates `content` or `tool_calls`),
  a string `error`, and `thinking` on `PLANNER_RESPONSE` only.
- **[1.1.16]** AGY's official hook docs (https://antigravity.google/docs/hooks/) and the
  binary's embedded documentation agree and are authoritative. It supports
  **exactly five** hook events and **no `SessionStart`**. All hook payload keys are
  **camelCase (protojson)**: `conversationId`, `workspacePaths`, `transcriptPath`,
  `artifactDirectoryPath`, `modelName`, `stepIdx`. `SessionStartHookArgs` exists as a
  protobuf type in the binary's string table but is not registrable in `hooks.json` and is
  undocumented — there is no SessionStart.
- `PreToolUse` accepts `decision: allow|deny|ask|force_ask|deny_unless_prior_grant`,
  `reason`, `permissionOverrides`, and an `overwrite` object that rewrites tool arguments.
  `PreInvocation`/`PostInvocation` accept `injectSteps[]` (`toolCall` | `userMessage` |
  `ephemeralMessage`); `PostInvocation` adds `terminationBehavior: force_continue|terminate|""`;
  `Stop` accepts `decision: "continue"` + `reason` to block termination; `PostToolUse`
  accepts `{}`. Unknown fields are protojson-rejected on every event. Record 1.1.24 decides
  which of these AGY honors live.
- **[1.1.16]** Hook cwd is the directory containing `hooks.json` (e.g. `~/.gemini/config`),
  not the workspace; `workspacePaths[]` is the only workspace signal. The binary sets
  `ANTIGRAVITY_CONVERSATION_ID` in the hook environment. Default hook timeout is 30 s;
  Gobby's template sets 45 s.

## Constraints
`kind: framing`

- **AGY floor is 1.1.18.** Older versions stay unavailable with an actionable upgrade
  message. The plan's own rule — the floor is the version the contracts were probed
  against — moves it to 1.1.18: the 2026-08-22 Gate 0 run started on 1.1.16 but AGY's
  auto-updater replaced the binary before the first live turn, so every live record in
  §1.2 Run 2 was observed on 1.1.18 and no earlier version is covered by evidence
  (1.1.12 changed headless `--mode`, 1.1.13 transcript writing, 1.1.15 stream-json
  text encoding, and between 1.1.16 and 1.1.18 the typed tool transcript records
  disappeared — §4.2). The auto-updater is itself a constraint: AGY upgrades in place
  without a pin, so the daemon's version probe (§2.5) must be re-run per session, not
  cached at install, and a future contract drift surfaces first as fixture mismatch.
  AGY becomes the first version-gated provider CLI; reuse `is_at_least_version`
  (`src/gobby/install/bin_freshness_models.py`) for the comparison and the existing
  `get_cli_version` (`src/gobby/servers/provider_model_discovery.py`) for the daemon's
  single async probe (§2.5), rather than inventing a mechanism.
- **`--dangerously-skip-permissions` is the house pattern for spawn.** Claude uses it, Qwen
  `--approval-mode yolo`, Grok `--always-approve`, Codex `--ask-for-approval never`, Droid
  `--auto`. AGY matches them, with SRT as the boundary. The source brief made this flag
  conditional on "Gobby deny/block decisions remain fail-closed" — **that precondition holds
  for no provider today** (`crates/ghook/tests/contract.rs:199-216` asserts `"should fail
  open"`), so it is not an AGY-specific gate. It is addressed in 2.3 instead.
- **No new monoliths.** Touched production files carry a measured line budget
  (counts at HEAD `274481f627`): `agents/sandbox.py` **914** (was 822 when first
  budgeted) is touched by both 3.1 and 3.2, so its decomposition is **mandatory** and
  is scheduled in 3.1 before any AGY code lands; `servers/websocket/chat/_session.py`
  **943** (was 988, then 872) receives 3.1's post-hydration launch seam and 5.3's
  provider-conditional pre-fire, so 3.1 extracts the launch seam into
  `_session_launch.py` before adding; `hooks/hook_manager.py` **864** is touched by 2.1
  and 4.1, so 2.1 extracts its webhook/MCP dispatch helpers into
  `hook_manager_dispatch.py` first; `agents/spawn_executor.py` 782 (was 746) gains AGY
  code in 6.1 and must stay under 1,000; `adapters/acp_client.py` 955 sits 45 lines
  from the ceiling before 3.1's ACP-SRT work, so 3.1 extracts before it adds;
  `hooks/event_handlers/_session_start/flow.py` 934 (was 966, then 844) takes 2.2's
  classifier routing and 4.1's registration idempotency, and 4.1 names its extraction
  target; `servers/routes/mcp/hooks.py` 781 (was 966) has regained headroom and is
  decomposed only if 4.1 projects it at or above 1,000. `runner_init/services.py` 850
  is **not** touched by this plan (2.5 publishes the record before the init seam runs;
  the seam reads nothing). Rust: `crates/ghook/src/action.rs` 639, `dispatch.rs` 678,
  `cli_config.rs` 164 are far from budget; `crates/ghook/tests/contract.rs` 1,668 is a
  test file and exempt. The validator's `production-size-growth` lint fires at 850
  lines for any file-scoped target, so every such target above names its split in the
  owning deliverable. If any production file projects at or above 1,000 lines, load the
  `decompose-monolith` skill and decompose in the same task — 2.1, 3.1 and 4.1 name the
  concrete extraction targets.
- **Schema changes are gcore migrations, registered, numbered 402+.** The Python
  migration directory is gone: `src/gobby/storage/migrations/` is empty and
  `src/gobby/storage/postgres_baseline_schema.sql` no longer exists
  (`tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl`
  forbids DDL in Python). A migration is a file
  `crates/gcore/assets/schema/migrations/<NNN>_<name>.sql` **plus** an
  `EmbeddedMigration { version, filename, checksum, sql: include_str!(…) }` entry
  appended to `MIGRATIONS` in `crates/gcore/src/schema/assets.rs` — the checksum is the
  sha256 of the file (`assets.rs::sha256_hex`) and is verified against
  `schema_migrations` by `crates/gcore/src/schema/verify.rs`; an unregistered file
  never applies. The baseline `crates/gcore/assets/schema/baseline.sql` is **sealed** at
  `BASELINE_VERSION` 375 with pinned checksums (`BASELINE_CHECKSUM`,
  `grant/bundle.rs::GOLDEN_BASELINE_CHECKSUM`, `runner.rs::PREDECESSOR_BASELINE_CHECKSUM`):
  deliverables never edit it — the 398 hop tried an in-place baseline edit and the 399
  hop (`f8c4b926a2`) reverted it because it broke the baseline-lineage tests. New
  columns and tables land only as numbered migrations. Each migration lands through
  the full embedded-asset contract, verified against the 399–401 hops
  (`f8c4b926a2`, `1583401303`, `8eef50ab8c`): the numbered file; its
  `EmbeddedMigration` entry in `assets.rs`; regenerated
  `crates/gcore/assets/schema/catalog.manifest.json`
  (`UPDATE_GCORE_SCHEMA_MANIFEST=1 cargo test -p gobby-core catalog_manifest_is_fresh_for_embedded_assets`)
  whenever the DDL changes the catalog; refreshed
  `src/gobby/storage/schema_expected_identity.json` (`latest_version`, `latest_checksum`,
  `assets_root_hash`); the `latest_version` pins in `crates/gcore/src/grant/bundle.rs`
  and `crates/gcore/src/grant/tests.rs`; the identity assertions in
  `crates/gcore/tests/schema_contract.rs` and `crates/gdaemon/tests/cli_contract.rs`;
  the `MIGRATIONS` enumeration in
  `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`
  and the counts in `crates/gcore/tests/catalog_manifest_freshness.rs`; and the four
  signed golden grant vectors under `tests/runtime_grants/golden/` that embed the
  schema identity. Python carries no DDL. The reserved 371–373 were consumed while the
  plan was parked and 399–401 were consumed between the 2026-08-20 re-baseline draft
  and this revision (latest applied: `401_model_metadata_reasoning.sql`), so the plan's
  reservations are **402** (3.1 workspace identity), **403** (4.1 startup claim
  generation), **404** (4.1 receipt effects), re-checked against
  `ls crates/gcore/assets/schema/migrations | tail -1` at implementation time.
- **No raw local transcripts or account data in fixtures.** Every fixture is scrubbed and
  minimal, derived from verified shapes.
- **Gate 0 blocks everything.** No implementation begins until P1 resolves the open contract
  questions. `--conversation` resume was proven on 1.1.18 (record 1.1.1), so P5 and P6 stand.
  The probe is a **pre-approval prerequisite**, not a manifest leaf: It runs in **both**
  print mode and interactive/tmux terminal mode; a record answered in one mode only is
  partial. §1.1 is executed as a
  standalone task created from its spec, executed, committed, and closed **before this plan
  is submitted for the planning approval that applies its implementation manifest**. The
  boundary is approval, not build handoff, because the machinery enforces nothing later:
  `apply_plan_review_manifest` writes the manifest at approval, and the expansion stage
  auto-advances from an approved planning stage (`_AUTO_ADVANCE_NON_AGENT_STAGES` and
  `expansion_work_rule` in `src/gobby/dispatch/rules.py` gate only on stage state), so a
  probe scheduled after approval could never precede leaf derivation. The branch rule
  is explicit: when a probe disproves a contract a downstream section consumes, the affected
  sections are revised and pass a fresh reviewed round — their acceptance items rewritten to
  the recorded contract, or converted to typed deferrals with open `deferred-from` tasks for
  any surface Gobby abandons — and only then is the plan submitted for the approval that
  derives the manifest, once, from the revised plan. Downstream acceptance is therefore
  always evaluated against the plan state matching the recorded Gate 0 contracts, never
  against a disproven assumption. This ordering is the executable one:
  `reset_expansion_output` (`src/gobby/tasks/expansion/_reset.py`) refuses any run
  containing a claimed, committed, progressed, or closed task, so a probe leaf inside the
  manifest could never be reconciled after a disproof discovered during its own execution —
  no implementation leaf may exist before every contract is recorded. Because approval —
  and the expansion that auto-advances from it — happens only after the probe task closes
  and disproof-driven revisions are re-reviewed, no reset is ever required and the manifest
  cannot leak a disproven contract into execution.
- Web chat currently applies **no** SRT and only Codex threads a provider-native policy, so
  the migration in P3 changes real behavior for Claude, Grok, Qwen and Droid — not just AGY.
- **Shared NDJSON stream limit is reused, not moved.** `ACP_STREAM_READER_LIMIT_BYTES`
  (16 MiB, `src/gobby/adapters/acp_client.py:86`) stays where it is; Droid and AGY import it.
  No constant relocation or rename.
- **Web-chat transport is a Gate 0 decision, not a design default.** 1.1.15 added
  `--input-format stream-json` (one subprocess per session, NDJSON prompts on stdin,
  requires `--output-format stream-json`), an alternative to one-subprocess-per-turn
  with `--conversation` resume. §5.2 embeds whichever transport record 1.1.18 proves;
  it may not assume per-turn result boundaries, cancellation, or id continuity that
  the record did not observe.

## P1: Contract Gate
`kind: framing`

**Goal**: Settle every unverifiable claim against the live binary before any code is written.

### 1.1 Probe the AGY live contract (executed on 1.1.18)
`kind: framing`

This section is the spec for the **pre-approval prerequisite task**: before this plan is
submitted for planning approval it is created as a standalone `category: test` task
referencing `agy-full-integration:1.1`, executed, committed, and closed — and any
disproof-driven revisions pass a fresh reviewed round — ahead of the approval that applies
this plan's implementation manifest and auto-advances into expansion. It emits no
manifest leaf. The numbered records below (1.1.1–1.1.24) are the probe-record IDs
downstream sections cite; each is satisfied by the closed prerequisite task's fixtures.

The probe runs twice per question where a live turn is involved: once in print mode
(`agy -p … --output-format stream-json`) and once in interactive/tmux terminal mode
(§"Terminal-mode probe mechanics" below). Records 1.1.1, 1.1.2, 1.1.6, 1.1.7, 1.1.13
and the zero-exit half of 1.1.10 were answered on 1.1.10 (§1.2) and are **re-confirmed**
on 1.1.18 (the probed floor), not re-derived — re-run the recorded command, diff against
the recorded output, and note "unchanged on 1.1.18" or the delta. Every other record is open.

Fixture artifacts (produced by the prerequisite task; every existing 1.0.x file is
replaced, not appended to):
- `tests/fixtures/provider_contracts/agy/README.md` — capture procedure, per-record
  outcome table (1.1.11), version `1.1.18`, both modes.
- `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl` — **one file for both
  modes**: each line carries `provider`, `event`, `mode` (`print` | `interactive`),
  `cli_version`, `payload` (live camelCase protojson as received on stdin), `env`
  (only `ANTIGRAVITY_CONVERSATION_ID` and `PWD`, scrubbed), `response` (what the
  capture hook answered), `capture_status: "live"`. Minimum ten lines (five events ×
  two modes) plus a `PostToolUse` with `error`, a `Stop` with a non-empty
  `terminationReason`, and a `PreToolUse` for each of an MCP tool, a built-in tool,
  and a shell command. The snake_case `shape_only_not_live_proven` lines are deleted.
  A second file is not introduced: the per-provider fixture loader pattern
  (`tests/adapters/test_provider_contract_fixtures.py`) reads one
  `hook-payloads.jsonl` per provider, and mode is a per-record attribute of one
  contract, not a second contract.
- `tests/fixtures/provider_contracts/agy/transcript-manifest.json` — regenerated
  wholesale from live 1.1.18 probes (layout incl. `transcript_full.jsonl` and
  `chunks/`, record 1.1.22).
- `tests/fixtures/provider_contracts/agy/stream-json-samples.jsonl` — new.
- `tests/fixtures/provider_contracts/agy/command-captures.json` — regenerated: scrubbed
  JSON from `agy --help`, `agy -p "/hooks" --output-format json`, `-p "/usage"`,
  `-p "/quota"`, `-p "/credits"`, `-p "/model"`, `agy --output-format json models`,
  `agy mcp list`, the flag-syntax and auth probes (records 1.1.7, 1.1.13, 1.1.15,
  1.1.19–1.1.21).
- `tests/fixtures/provider_contracts/agy/agy_models_v1.0.10.txt` and
  `model-cache-summary.json` — deleted (superseded by `command-captures.json`; §6.3's
  fixture is re-pointed there).

Run scripted probes in a throwaway workspace and record results in the fixture README.
Twenty questions are open and each one changes downstream design:

1. **Does `--conversation <id>` actually resume?** (Answered: yes, record 1.1.1.) The earlier probe plan
   (task 15038, `task-15038-agy-grok-contract-probe.md:17`) observed it **timing out**
   on 1.0.1. If it still fails, the web-chat backend cannot be resumable and P5 must be
   re-planned around `--continue` or single-turn sessions.
2. **What does `transcriptPath` literally contain?** AGY's embedded docs say
   `<workspace>/.gemini/antigravity-cli/transcript.jsonl` (workspace-local). Only the
   `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` form was
   observed on disk, and no workspace-local file exists in this repo. The parser and the
   discovery fallback both depend on which is real.
3. **cwd behavior.** Confirm the reported bug: `init.cwd` matches the requested cwd, yet a
   tool call lacking an explicit `Cwd` executes under AGY's application-data directory.
   Determine whether `--project`, `--add-dir`, or the `PreToolUse` `overwrite` field is the
   correct fix.
4. **Image input.** AGY has real image plumbing (`image_paths`/`imagePaths` protobuf field,
   `attachments`, png/jpeg/gif/webp mime handling) but `--help` exposes no image flag.
   Test whether print mode can reach it via an `@path` mention or by asking AGY to view an
   image file. This decides whether `VISION_EXTRACT` is enabled or stays unavailable.
5. **Launch-security flags.** Record `--help` evidence and live argv outcomes for AGY's
   `--sandbox` values and `--dangerously-skip-permissions` in both print and terminal modes,
   including the exact accepted value that disables AGY's native sandbox when SRT is the
   enforcing boundary. These flags define the security boundary consumed by 3.2 and 6.1;
   neither section may embed a flag form this probe did not record.
6. **Live cancellation contract.** Interrupt an active AGY turn in print mode and record
   the exact mechanism (signal or API), the process-tree exit behavior, the partial-stream
   outcome on stdout, whether orphan children remain, and whether the conversation id from
   the interrupted turn still resumes afterward. 5.2 cancellation, 5.2 id-preservation and
   5.3 websocket-interrupt acceptance consume this record; none of them may invent
   interruption semantics this probe did not observe.
7. **Network and state footprint.** Record the exact domains AGY contacts during a
   print-mode turn and the filesystem roots it reads and writes under
   `~/.gemini/antigravity-cli/` — credentials, per-conversation brain state, transcripts.
   3.2's sandbox-policy entries embed only values this probe recorded.
8. **Controlled-tool bridge.** Determine whether AGY print mode can act as a tool runtime
   under Gobby's control: the exact transport or configuration (flags, hook `overwrite`,
   MCP registration, or none) by which Gobby can expose a bounded tool set, whether tools
   outside that set are refused, and what the stream records for a denied tool. 6.2's
   `TOOL_CHAT` branch consumes this record: a supported bridge names its transport, an
   unsupported one keeps the binding unavailable with this probe as the narrow reason.
9. **`--print-timeout` semantics.** Characterize the flag as a whole-turn or inactivity
   clock, its accepted syntax and default, **whether a disabled or unbounded form exists**
   (an explicit disable value, or omission yielding no limit), the process exit code and
   stream payload on expiry — including whether expiry can return exit code zero with an
   error payload — and its interaction with an actively streaming turn. 5.2's timeout
   policy embeds only behavior this probe recorded.
10. **Terminal plan-menu contract.** Gobby drives plan-mode approval menus in spawned
    terminal CLIs through per-provider keystroke sequences (`DEFAULT_PLAN_KEYSTROKES`,
    `src/gobby/adapters/plan_keystrokes.py`). Record whether AGY's terminal mode presents
    a plan/approval menu at all, and if so the exact keystroke sequences that select each
    option, including any pane-state dependence. 6.1's plan-keystroke registry entry
    consumes this record: a present menu names its recorded keystrokes, an absent one
    records the refusal evidence for the negative contract.
11. **Authentication footprint.** Launch AGY under a scrubbed environment and record its
    exact auth contract: every environment variable it accepts for credentials, whether
    authentication is file-only (and the exact credential roots), how ambient credential
    env vars are handled when present but unneeded (rejected, ignored, or read), and
    whether any spawn-path caller requires auth-CLI inference for AGY. 3.2's
    credential-env masking and 6.1's auth-inventory rows consume this record; neither
    may guess an env var this probe did not observe.
12. **Compaction signaling.** Record whether AGY's print-mode stream-json emits any
    compaction, context-pressure, or summarization event during a long turn — the exact
    record shape if one exists, or the absence evidence if none does. 5.3's
    `PRE_COMPACT` parity branch consumes this record.
13. **Interactive-mode dispatch.** In a tmux-hosted `agy` terminal session, do all
    five events dispatch with the same payload shape as print mode? Capture one
    payload per event per mode. A hook that fires in print mode only makes 6.1's
    spawned terminal an untracked session.
14. **`--input-format stream-json` session semantics** (1.1.15). With one process per
    session: how is a turn's end delimited on stdout (`result` per turn?); what does
    the process do on stdin EOF (exit code, final record); can an in-flight turn be
    cancelled without killing the process (signal, or a stdin control record), and
    does the next stdin message continue the same `conversation_id`; is
    `--conversation` accepted together with `--input-format`. This decides §5.2's
    transport: persistent process vs per-turn `--conversation` resume.
15. **Usage/quota introspection** (1.1.11). Exact JSON for `agy -p "/usage"
    --output-format json`, `/quota`, `/credits`: field names, units, reset
    timestamps, exit code, and whether a quota-exhausted state is distinguishable.
    Confirm no agent turn or quota spend occurs. Feeds the usage-capacity deliverable
    (#19364 folded into this plan as §6.4).
16. **Model list shape** (1.1.12). `agy models --output-format json` and
    `--output-format stream-json`: exact schema (id, display name, efforts, default
    flag), whether stdout contains only the list, and exit code when unauthenticated.
    Feeds §6.3.
17. **Hook registration introspection** (1.1.12). `agy -p "/hooks" --output-format
    json`: confirm `command.data.hooks[].{name,enabled,source,actions[]}`, how a
    disabled or malformed hook appears, and that no agent turn runs. Feeds §2.6's
    post-install verification.
18. **Transcript file layout** (1.1.13+). Which of `transcript.jsonl`,
    `transcript_full.jsonl`, and `chunks/{transcript,transcript_full}/NNNNNNNN.jsonl`
    is append-only and complete; what `transcript_full` adds; when a new chunk file
    opens; and which file the hook's `transcriptPath` names. The parser (§4.2) and
    the disk-fallback table (§2.2) consume exactly one of these.
19. **`--mode plan|accept-edits` headless vs terminal** (1.1.12). In print mode, does
    `--mode plan` produce a plan without executing tools and how is approval
    expressed (stream record? exit?); in terminal mode, what menu appears and which
    keystrokes select each option (this extends 1.1.14 with the recorded flag).
20. **Response-field live acceptance.** For each documented response field, does
    AGY honor it: `PreToolUse` `deny_unless_prior_grant` and `permissionOverrides`;
    `PostInvocation` `terminationBehavior: force_continue|terminate`; `injectSteps`
    `toolCall` (does the injected tool actually run?); `Stop` `decision:"continue"`
    forced-end after N continuations (1.1.9 changelog — record N); and what AGY does
    with a hook that exits 1 or 2 with legal stdout and a stderr message. 4.1's
    response translation embeds only honored fields.

Capture a scrubbed NDJSON sample covering: `init`, resumed turn, assistant `text_delta`,
tool `ACTIVE`/`DONE`/`ERROR`, malformed line, unsuccessful `result`, and a >64 KiB tool output.
Also capture scrubbed live transcript records for one zero-exit and one nonzero-exit
`RUN_COMMAND`, preserving the exact structured fields (`exit_code`, `status`, `error`) and
provenance — 4.2's validation-evidence parity consumes these as the provider-proven payload
shapes required by #18381.

**Terminal-mode probe mechanics.** The gobby-sessions `send_keys`/`capture_output`
MCP tools cannot drive this probe: they authorize targets against registered Gobby
session rows in the caller's agent tree (`src/gobby/mcp_proxy/tools/sessions/_terminal.py::_authorize_send_keys_target`)
and AGY has no spawn path or session row yet. Use tmux directly, mirroring
`TmuxSessionManager.send_keys`/`capture_pane` (`src/gobby/agents/tmux/session_manager.py`):

1. `tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <throwaway workspace> agy`
   (one variant with `--sandbox=false --dangerously-skip-permissions`, one without, for
   1.1.7).
2. Wait for the prompt: poll `tmux capture-pane -p -t agy-gate0` until the input
   prompt renders (record the exact prompt glyph/text for 6.1's prompt monitor).
3. Send a prompt: `tmux send-keys -t agy-gate0 -l '<fixed probe prompt>'` then
   `tmux send-keys -t agy-gate0 Enter`. Use three prompts: one that invokes a
   built-in tool (`list the files in this directory`), one that runs a shell command
   (`run: ls -la`), one that calls the gobby MCP server (`call the gobby
   list_mcp_servers tool and report the result`).
4. Capture the pane after each turn (`capture-pane -p -S -200`) and keep it as the
   interactive evidence for 1.1.14/1.1.23 menus and 1.1.3 cwd.
5. Interrupt a long turn with `tmux send-keys -t agy-gate0 C-c` for 1.1.8's
   terminal half; `tmux kill-session -t agy-gate0` at the end and check for orphans
   (`pgrep -f antigravity`).

**Capture hook.** Install a second top-level key in `~/.gemini/config/hooks.json`
beside `gobby` for the probe's duration (AGY keys the file by hook name, so both
coexist): `"gate0-capture"` with the five events, `timeout` 45, whose command writes
stdin verbatim to `<scratchpad>/hook-captures/<mode>-<event>-<seq>.json`, appends
`cwd`, `ANTIGRAVITY_CONVERSATION_ID`, and `PWD`, and answers the per-event legal skip
JSON (`{"decision":"allow"}` for `PreToolUse`, `{}` otherwise). Gobby's own `gobby`
hook stays installed so the same turn proves the real route: a daemon-side
receipt for the turn is part of 1.1.5's evidence (recorded per mode × tool class ×
event in `daemon-receipts.jsonl`: the daemon's HTTP response, its processed-envelope
marker, and its `hooks.log` line). The `source=agy` line and the AGY session row are
§4.1's acceptance evidence — the pre-§4.1 adapter cannot produce them. Remove
`gate0-capture` when done; `agy -p "/hooks" --output-format json` before and after is
the proof it is gone.

**Scrubbing rules (apply to every fixture line).** `$HOME` → `~`; the conversation id
→ `<CONVERSATION_ID>` everywhere it appears (payload, paths, env); the throwaway
workspace → `<WORKSPACE>`; `artifactDirectoryPath` keeps its shape with the id
replaced; `modelName` is kept verbatim (product name, not account data); free-text
prompts → `<PROMPT_TEXT>` unless they are run-authored probe prompts enumerated verbatim
in the fixture README's "Probe prompts" list (the three fixed probe prompts are members;
bare slash commands such as `/usage` are commands, not prompts) — the fixture contract
test extracts every prompt position (`-p` arguments, `send-keys -l` arguments,
stream-json stdin lines, interactive pane `>` echoes) and fails on any prompt outside
that list; tool
output over 4 KiB is truncated to the first and last 1 KiB with a
`<TRUNCATED n bytes>` marker (the >64 KiB sample 1.1.6 asked for cannot exist: AGY
itself caps tool output at ~8 KiB with its own `<truncated N bytes>` marker, which
the fixture records instead); anything matching an email,
token, `Authorization`, `api_key`, or OAuth field is replaced with `<REDACTED>` and
the record noted; `/usage` `/quota` `/credits` numbers are kept but account
identifiers are replaced. No record may reference a path under
`~/.gemini/antigravity-cli/` other than the `brain/<CONVERSATION_ID>/…` forms.

**Recorded outcomes** (probe-record IDs; satisfied by the closed prerequisite task):

- 1.1.1 - **[re-confirmed unchanged on 1.1.18; delta: `duration_seconds` is cumulative per conversation]** Resume behavior on 1.1.18 is recorded with the exact command and observed output. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.2 - **[re-confirmed on 1.1.18; layout disproven: the literal value names `transcript_full.jsonl`]** The literal `transcriptPath` value from a live hook invocation is recorded, resolving the workspace-local vs `brain/` ambiguity. Both modes. file: `tests/fixtures/provider_contracts/agy/transcript-manifest.json`.
- 1.1.3 - **[confirmed on 1.1.18; remedy: `--add-dir <cwd>` on every launch]** cwd behavior for a tool call without explicit `Cwd` is characterized, with the chosen remedy named. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.4 - **[negative on 1.1.18: no input attachment; vision only via the model's own `view_file`]** Image-input support is determined by live test, deciding the `VISION_EXTRACT` binding. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.5 - **[confirmed on 1.1.18 in both modes; daemon receipt proven, `source=agy` line and session row deferred to §4.1]** Hook payloads are captured live in camelCase in print and interactive mode, replacing the snake_case `shape_only_not_live_proven` records, and the same turn leaves a `source=agy` line in `~/.gobby/logs/hooks.log` and an AGY session row. file: `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl`.
- 1.1.6 - **[re-confirmed unchanged on 1.1.18; deltas: 57 tools, `CANCELED` status, >64 KiB sample impossible (AGY caps tool output at ~8 KiB)]** A scrubbed stream-json NDJSON sample covers init, resume, text delta, tool lifecycle, malformed line, failure result, and a >64 KiB tool output. file: `tests/fixtures/provider_contracts/agy/stream-json-samples.jsonl`.
- 1.1.7 - **[re-confirmed unchanged on 1.1.18]** The accepted syntax and values for `--sandbox` and `--dangerously-skip-permissions`, including the value that disables AGY's native sandbox, are recorded from live probes in both print and terminal modes. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.8 - **[confirmed on 1.1.18: SIGINT/SIGTERM exit 1 with the timeout payload, shell children orphaned, resume works; terminal `C-c` interrupts without `Stop`]** Active-turn cancellation is probed live, recording the mechanism, process-tree exit, partial-stream outcome, orphan cleanup, and post-interrupt resumability of the conversation id, in print mode and via `C-c` in terminal mode. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.9 - **[confirmed on 1.1.18]** The domains AGY contacts and the `~/.gemini/antigravity-cli/` roots it reads and writes during a live turn are recorded, sourcing 3.2's sandbox-policy entries. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.10 - **[disproven on 1.1.18: no `RUN_COMMAND` record and no structured `exit_code` exist; both exit classes are `MODEL/GENERIC` free text]** Scrubbed live zero-exit and nonzero-exit `RUN_COMMAND` transcript records preserve the exact structured fields and provenance. file: `tests/fixtures/provider_contracts/agy/transcript-manifest.json`.
- 1.1.11 - **[confirmed: table in the fixture README; disproven contracts revised in this plan]** A contract-outcome table in the fixture README maps every probe question to confirmed or disproven. For each disproven contract, the affected downstream sections are revised and pass a fresh reviewed round — or convert to typed deferrals with open `deferred-from` tasks — before this plan is submitted for the planning approval that applies its implementation manifest; expansion, which auto-advances from that approval with no prerequisite-task gate, consumes only the revised plan. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.12 - **[confirmed supported on 1.1.18: `PreToolUse` `decision:"deny"` transport, MCP tools surface as `call_mcp_tool`]** The controlled-tool bridge outcome is recorded: the exact transport or configuration and the denial behavior for a supported bridge, or the observed refusal evidence for an unsupported one — the record 6.2's `TOOL_CHAT` branch consumes. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.13 - **[re-confirmed unchanged on 1.1.18; delta: under `--output-format json|stream-json` expiry is a stdout `result{status:ERROR}` record, exit still 1]** `--print-timeout` is characterized live: clock semantics, accepted syntax and default, whether a disabled or unbounded form exists, expiry exit code and stream payload — including any zero-exit error payload — and behavior against an actively streaming turn. **Recorded (1.1.10): Go duration syntax, default `5m0s`, no disable sentinel, `2562047h` accepted as the effectively-unbounded form, expiry exits 1 on stderr — disproving the committed 1.0.11 fixture's exit-0-on-stdout shape.** file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.14 - **[confirmed on 1.1.18: menu exists, keystrokes recorded]** The terminal plan-menu contract is recorded: the observed menu (if any) and the exact keystroke sequences per option, or the evidence that AGY terminal mode exposes no plan menu — the record 6.1's plan-keystroke registry entry consumes. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.15 - **[confirmed on 1.1.18: Keychain-only credential, no env var accepted, no auth-CLI inference needed]** The authentication footprint is recorded from a scrubbed-environment launch: accepted credential env vars, file-only credential roots, ambient-credential handling, and whether any in-scope caller requires auth-CLI inference — the record 3.2's credential-env masking and 6.1's auth inventories consume. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.16 - **[negative on 1.1.18: no compaction or context-pressure record; `checkpoint` is not a pressure signal]** Compaction signaling is recorded: the exact stream-json compaction or context-pressure record shape, or the absence evidence — the record 5.3's `PRE_COMPACT` parity branch consumes. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.17 - **[confirmed on 1.1.18: all five events fire interactively with key sets identical to print mode; negatives apply to both modes]** Interactive/tmux-mode dispatch is recorded per event with captured payloads, diffed field-by-field against the print-mode payloads; any event that fires in one mode only is recorded as a negative contract that 6.1 consumes. file: `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl`.
- 1.1.18 - **[confirmed on 1.1.18: per-turn `result`, EOF exit 0, per-turn timeout, `--conversation` accepted; no in-flight cancel — SIGINT kills the process]** `--input-format stream-json` persistent-session semantics are recorded: per-turn result delimiter, stdin-EOF behavior and exit code, in-flight-turn cancellation mechanism and whether the process survives it, conversation-id continuity across stdin messages, and `--conversation` compatibility — the record that selects §5.2's transport. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.19 - **[confirmed on 1.1.18; `/quota` aliases `/usage`, `/credits` is negative (exit 1)]** The `-p "/usage"`, `/quota`, and `/credits` JSON shapes under `--output-format json` are recorded with exit codes and the quota-exhausted distinction, and the no-turn/no-spend property is confirmed. file: `tests/fixtures/provider_contracts/agy/command-captures.json`.
- 1.1.20 - **[disproven placement on 1.1.18: `agy --output-format json models`; shape `models[].{id,label}`, default via `-p "/model"`]** The `agy models --output-format json|stream-json` shapes are recorded (fields, default marker, effort vocabulary, unauthenticated exit) — the record §6.3 consumes. file: `tests/fixtures/provider_contracts/agy/command-captures.json`.
- 1.1.21 - **[confirmed on 1.1.18]** The `agy -p "/hooks" --output-format json` shape is recorded, including how disabled and malformed hooks appear and that no agent turn runs — the record §2.6's installer verification consumes. file: `tests/fixtures/provider_contracts/agy/command-captures.json`.
- 1.1.22 - **[confirmed on 1.1.18: parser input is `transcript_full.jsonl`]** The transcript layout is recorded — `transcript.jsonl` vs `transcript_full.jsonl` vs `chunks/` — naming the one append-only complete file the parser consumes and the file `transcriptPath` points at. file: `tests/fixtures/provider_contracts/agy/transcript-manifest.json`.
- 1.1.23 - **[confirmed on 1.1.18]** `--mode plan|accept-edits` behavior is recorded in headless and terminal modes, including how plan approval is expressed and the terminal menu keystrokes — extending 1.1.14 with the recorded flag. file: `tests/fixtures/provider_contracts/agy/README.md`.
- 1.1.24 - **[confirmed on 1.1.18 with negatives: `permissionOverrides` and `injectSteps.toolCall` not honored; hook exit 1/2 blocks the tool fail-closed; `Stop` exit ignored; `continue` honored 10 times]** Live acceptance is recorded per response field — `deny_unless_prior_grant`, `permissionOverrides`, `terminationBehavior`, `injectSteps.toolCall`, `Stop` `decision:"continue"` and its forced-end count — and for hook exit codes 1 and 2 with legal stdout; §4.1 embeds only honored fields. file: `tests/fixtures/provider_contracts/agy/hook-payloads.jsonl`.

### 1.2 Gate 0 execution record
`kind: framing`

This is the cumulative record. Task #19563 ran once against 1.1.10 on 2026-08-03,
observed dispatch on 1.1.16 on 2026-08-20, and completed the full both-mode probe
on 2026-08-22 — on 1.1.18, because the auto-updater replaced the binary before the
first live turn. The fixture set is committed under
`tests/fixtures/provider_contracts/agy/` (README outcome table, `hook-payloads.jsonl`,
`transcript-manifest.json`, `stream-json-samples.jsonl`, `command-captures.json`);
the 1.0.x files are deleted.

**Run 1 — 1.1.10 (2026-08-03).** Four contracts confirmed outright, two confirmed
with corrections that disproved committed plan text, two partial, ten unanswered
because no hook fired:

| Record | Outcome | Observed evidence |
| --- | --- | --- |
| 1.1.1 resume | **confirmed** | `agy --print --conversation <id>` resumes: the same `conversation_id` is echoed, `num_turns` advances 1 → 2, and the model recalls the prior turn. The 1.0.1 timeout recorded in task 15038 no longer reproduces. |
| 1.1.2 transcriptPath | **confirmed, embedded-doc form disproven** | Only `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl` exists. No workspace-local `<workspace>/.gemini/antigravity-cli/transcript.jsonl` is created — the form AGY's own embedded docs name. A **new** sibling file, `transcript_full.jsonl`, is written beside the transcript and is not accounted for anywhere in this plan. |
| 1.1.5 payload keys | **partial** | All ten camelCase keys are present in the 1.1.10 binary's string table. No live payload was captured, because no hook ever fired (see the Upstream Blocker Gate). The `shape_only_not_live_proven` fixture records therefore stand unreplaced. |
| 1.1.6 stream-json | **confirmed, plan shape disproven** | Records are **nested under the event key** — `{"event":"step_update","step_update":{…}}` — rather than the flat form §5.1 encodes. The `step_type` vocabulary is also wider than recorded: `user_input`, `checkpoint`, and `unknown` occur alongside `agent_response` and `tool`. §5.1 is corrected below. |
| 1.1.7 sandbox flags | **confirmed** | `--sandbox` is a boolean flag; `--sandbox=false` is the accepted form that disables AGY's native sandbox. `--dangerously-skip-permissions` yields `permission_mode: always-proceed` in the stream `init` record. |
| 1.1.10 `RUN_COMMAND` | **partial** | A zero-exit record was captured with `exit_code` structured at the record top level. The nonzero-exit record was not captured, so 4.2.9's failure-outcome fixture has no live payload shape. |
| 1.1.13 `--print-timeout` | **confirmed, committed fixture disproven** | Go duration syntax, default `5m0s`. Expiry exits **1** and writes the timeout message to **stderr**. There is **no disable sentinel**; `2562047h` is accepted and is the effectively-unbounded form. This **contradicts the committed 1.0.11 fixture**, which records expiry as exit 0 on stdout — that fixture is stale and its zero-exit branch is dead. §5.2 is corrected below. |
| 1.1.3, 1.1.4, 1.1.8, 1.1.9, 1.1.11, 1.1.12, 1.1.14, 1.1.15, 1.1.16 | **unresolved on 1.1.10** | Each required a dispatched hook payload, an interactive terminal turn, or a long live turn. |

**Run 2 — 1.1.16 → 1.1.18 (2026-08-20 dispatch observation; 2026-08-22 full
both-mode run).** The 2026-08-20 print-mode turn (conversation
`384e2db9-cff0-437f-8170-cd116ad15d5a`) proved dispatch of `PreInvocation`,
`PreToolUse`, `PostInvocation`, `Stop` through the installed `gobby` hook and exposed
the two `ghook` defects fixed in `488f6c244c` (#20624). The 2026-08-22 run captured
every record below in print mode and interactive/tmux mode; 566 hook invocations were
recorded. Exact commands and outputs are in the fixture README's outcome table; this
table is its projection.

| Record | Outcome | Observed evidence |
| --- | --- | --- |
| 1.1.1 resume | **re-confirmed unchanged** (+delta) | `--conversation <id>` resumes with the same id, `num_turns` 1→2, prior turn recalled, also after SIGINT/SIGTERM. Delta: `result.duration_seconds` is measured from conversation creation (213 s for a 5 s turn). |
| 1.1.2 transcriptPath | **re-confirmed; layout disproven** | Literal value `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl` in both modes, on the first `PreInvocation`. No workspace-local file. |
| 1.1.3 cwd | **confirmed, remedy named** | Unregistered cwd → `workspacePaths []`, `default-cli-project`, no `Cwd` on `run_command`. Remedy: `--add-dir <cwd>` on every launch. |
| 1.1.4 image input | **negative** | No image flag; `@path` is plain text; stream-input image blocks rejected. Only the model's own `view_file` on a PNG delivers an image. |
| 1.1.5 payloads | **confirmed; session row deferred** | All five events captured in camelCase in both modes for `list_dir`, `run_command`, `call_mcp_tool`; hook cwd `~/.gemini/config`, env `ANTIGRAVITY_CONVERSATION_ID`. Daemon receipts (HTTP 200 + processed marker + `hooks.log` line) for every event of a built-in, shell, and MCP turn in both modes (`daemon-receipts.jsonl`, 49 deliveries). `source=agy` line / session row: impossible before §4.1 (adapter reads `session_id`/`cwd`/`tool_name`). |
| 1.1.6 stream-json | **re-confirmed unchanged** (+deltas) | Nested shape unchanged; 57 tools; `step_type` adds `system_message`, `error_message`; `result.status` adds `CANCELED`; tool output capped at ~8 KiB by AGY so no >64 KiB sample exists. |
| 1.1.7 sandbox flags | **re-confirmed unchanged** | `--sandbox=false` accepted both modes; skip flag → `always-proceed`; without it headless tools auto-deny (`CANCELED`, exit 0) and hook `allow` does not override. |
| 1.1.8 cancellation | **confirmed** | Print: SIGINT/SIGTERM exit 1 with `result ERROR "timeout waiting for response"`, step left `ACTIVE`, shell child orphaned, MCP child dies, resume works. Terminal: `C-c`/`esc` interrupt without `Stop`; 2nd `C-c` arms exit, 3rd exits without `Stop`. |
| 1.1.9 network/roots | **confirmed** | `daily-cloudcode-pa.googleapis.com`, `oauth2.googleapis.com`, `accounts.google.com`, `play.googleapis.com`, `playwright*.azureedge.net`, `googleusercontent.com`; roots under `~/.gemini/antigravity-cli/` (`brain/`, `conversations/`, `cache/`, `log/`, `crashes/`, `presence/`, `knowledge/`, `mcp/`, `bin/agentapi`), `~/.gemini/config/projects/`, `~/Library/Caches/ms-playwright-go/`, login Keychain. |
| 1.1.10 `RUN_COMMAND` | **disproven** | Zero- and nonzero-exit shell runs both produce `MODEL/GENERIC` free text (`The command exited with code 7.\nOutput:\nboom`); no `RUN_COMMAND` record, no structured `exit_code`; stream step `DONE`, `PostToolUse.error` `""`. |
| 1.1.11 outcome table | **confirmed** | Fixture README table; revisions applied to §2.2, §2.3, §2.5, §4.1, §4.2, §5.1, §5.2, §5.3, §6.1, §6.2, §6.3, §6.4, §7.1. |
| 1.1.12 controlled-tool bridge | **confirmed (supported)** | `PreToolUse` `decision:"deny"` + `reason` → `tool ERROR` `TOOL_ERROR "tool call denied by pre-tool hook: …"`, no `PostToolUse`, `result ERROR`, exit 0. MCP tools are `call_mcp_tool{ServerName,ToolName,Arguments}`. |
| 1.1.13 `--print-timeout` | **re-confirmed unchanged** (+delta) | Go syntax, default `5m0s`, no disable sentinel, expiry exit 1. Delta: under `json|stream-json` the payload is a stdout `result{status:ERROR}`; per turn under stream input. |
| 1.1.14 terminal plan menu | **confirmed** | `shift+tab` cycles modes; `ctrl+r`/`/artifact` review with `y`/`n`/`shift+a`/`p`/`esc`; permission prompt `1`/`2`/`3`/`4`/`esc`. |
| 1.1.15 auth footprint | **confirmed** | Keychain item `svce=gemini acct=antigravity`; env API-key vars ignored; foreign `HOME`: a `-p` turn prints the OAuth URL and exits 1 after the 60 s auth timeout, an interactive launch stops at the `not signed in` login-method menu, `models` exits 1 immediately with `Please sign in` (1.1.20). |
| 1.1.16 compaction | **negative** | No compaction/context-pressure record; `checkpoint` fires at step 1 of every conversation. |
| 1.1.17 interactive dispatch | **confirmed** | All five events; key sets identical to print mode; negatives (no `PostToolUse` on `TOOL_ERROR`, no `Stop` on interrupt/exit) apply to both modes. |
| 1.1.18 `--input-format stream-json` | **confirmed** | Launch without `-p`; one `result` per turn; EOF → exit 0; per-turn timeout; `--conversation` accepted; SIGINT → `result ERROR "context canceled"` exit 1 (no in-flight cancel); malformed line fatal. |
| 1.1.19 usage/quota | **confirmed; `/credits` negative** | `/usage` shape `groups[].buckets[].{id,name,window,remaining_fraction,reset_time}`, `num_turns 0`; `/quota` alias; `/credits` exit 1; exhausted = `remaining_fraction 0` + turn `result ERROR "Individual quota reached"`. |
| 1.1.20 models | **disproven (placement)** | `agy models --output-format json` exit 1; `agy --output-format json models` → `command.data.models[].{id,label}`; no default marker; default via `-p "/model"`; effort is the id suffix; unauthenticated (isolated `HOME`) → immediate exit 1, empty stdout, stderr `Please sign in`, no OAuth prompt. |
| 1.1.21 `/hooks` | **confirmed** | `hooks[].{name,enabled,source,actions[].{event,type,command,timeout_seconds}}`; disabled shows `enabled:false`; malformed shows without warning; unknown events vanish. |
| 1.1.22 transcript layout | **confirmed** | Parser input `transcript_full.jsonl`; `transcript.jsonl` is the truncated twin; `chunks/` byte-identical copies. |
| 1.1.23 `--mode` | **confirmed** | Headless `plan` writes `brain/<id>/<name>.md`, no approval record; `accept-edits` writes without prompting; `bogus` → warning. |
| 1.1.24 response fields | **confirmed with negatives** | Honored: `deny`, `deny_unless_prior_grant`, `overwrite`, `terminationBehavior`, `injectSteps.userMessage`/`ephemeralMessage`, `Stop continue` ×10. Not honored: `permissionOverrides` (headless), `injectSteps.toolCall` (fatal). PreToolUse exit 1/2 blocks the tool; Stop exit 2 ignored. |

Disproven contracts repaired in the plan: §5.1's nested record shape and `step_type`
vocabulary (Run 1); §5.2/1.1.13's timeout contract (Run 1); and from Run 2 the
`GENERIC`-only tool records (§4.2), `transcript_full.jsonl` (§2.2, §5.1), the
honored response-field set (§4.1), the stream-input cancel semantics (§5.2), the
`models` flag placement (§6.3), and the `VISION_EXTRACT`/`PRE_COMPACT` negatives
(§6.2, §5.3). Every row reads confirmed, re-confirmed, negative, or
disproven-and-revised.

## Dispatch Evidence Gate
`kind: framing`

**Upstream status.** `google-antigravity/antigravity-cli` issue #222 — hooks register
but do not dispatch — is still Open (`stat:awaiting response`; maintainer asked on
2026-07-22 whether it persists on 1.1.5, last comment 2026-07-29). Gobby reproduced it
on 1.1.10 on 2026-08-03 after fixing its own `hooks.json` format (`230cb26ea`).

**Local evidence supersedes it.** On 1.1.16, with the same `hooks.json`, a print-mode
turn dispatched four of the five events through the installed Gobby hook (Status).
No changelog entry explains the change, so this plan does not infer a fix from
upstream; it gates on what the installed binary does. The gate is therefore
evidence-driven in both directions: dispatch is accepted only when the Gate 0 run
captures it, and #222's state cannot veto a captured dispatch.

**What each deliverable needs from Gate 0** (replaces the former "cannot be
implemented" table):

| Deliverable | Gate 0 records it embeds |
| --- | --- |
| 2.2 | 1.1.2, 1.1.22 (disk-fallback path and file), 1.1.5 (`transcriptPath` arrives before the file exists) |
| 2.3 | 1.1.24 (hook exit-code handling), 1.1.5 (event set) |
| 2.6 | 1.1.21 (`/hooks` JSON) |
| 3.2 | 1.1.7, 1.1.9, 1.1.11 |
| 4.1 | 1.1.5, 1.1.17, 1.1.24 (honored response fields) |
| 4.2 | 1.1.10 (both exit classes), 1.1.22 |
| 5.1 | 1.1.6, 1.1.16 |
| 5.2 | 1.1.1, 1.1.8, 1.1.13, 1.1.18 (transport choice) |
| 5.3 | 1.1.16, 1.1.18 |
| 6.1 | 1.1.3, 1.1.7, 1.1.11, 1.1.14, 1.1.17, 1.1.23 |
| 6.2 | 1.1.4, 1.1.12 |
| 6.3 | 1.1.20 |
| 6.4 (usage-capacity, folds #19364) | 1.1.19 |
| 7.1 | every row above |

**Pre-approval conditions.** All five must hold before this plan is submitted for
the planning approval that applies its implementation manifest:

1. **Satisfied (2026-08-22).** Gate 0 is complete in **both** print mode and
   interactive/tmux mode: every record 1.1.1–1.1.24 reads confirmed,
   re-confirmed, negative, or disproven (§1.2 Run 2).
2. **Satisfied to the extent the current code allows.** The live route is proven
   to the daemon, not only the capture hook: turns for an MCP tool, a built-in
   tool, and a shell command reached the daemon through the real hook binary
   `ghook` in both modes (`daemon-receipts.jsonl`: HTTP 200, processed marker
   and `hooks.log` line per delivery — the pre-§4.1 adapter rejects the camelCase
   payload at validation). The `source=agy` line and
   the AGY session row are produced by §4.1's synthetic `SESSION_START` and are
   that section's acceptance evidence (4.1.22); they cannot exist earlier.
3. **Satisfied.** The fixture set in §1.1 is committed under
   `tests/fixtures/provider_contracts/agy/`, replacing every 1.0.x file and the
   `shape_only_not_live_proven` records.
4. **Revised, review pending.** Every section consuming a disproven contract is
   revised (§1.2 Run 2 lists them) and must pass a fresh reviewed round
   (Constraints branch rule).
5. **Satisfied as of 2026-08-22.** The floor is 1.1.18, the release the run
   observed; AGY auto-updates in place, so this check repeats before approval
   if a newer release has shipped by then.

Adversarial review resumes at Round 19 against the revised plan; Round 18 stays
unfinalized.

## P2: Provider Consistency Foundation
`kind: framing`

**Goal**: Collapse the divergent seams so AGY is added once, not five times.

### 2.1 Unify transcript parser dispatch to one registry [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/__init__.py::get_parser`
- `src/gobby/sessions/transcript_parsing.py::_get_parser`
- `src/gobby/sessions/transcript_parsing.py::_parse_lines`
- `src/gobby/sessions/transcript_processing.py::TranscriptProcessingMixin._process_session_transcript`
- `src/gobby/sessions/summary_context.py::_build_summary_prompt_context`
- `src/gobby/cli/tokens.py::_load_session_messages`
- `src/gobby/sessions/analyzer.py::TranscriptAnalyzer.__init__`
- `src/gobby/hooks/factory.py::HookManagerFactory.create`
- `src/gobby/hooks/factory.py::HookManagerComponents`
- `src/gobby/hooks/hook_manager.py::*` — scope-reason: stores `components.transcript_processor`; adopts the source-aware parser seam the factory now supplies, and its webhook/MCP dispatch helpers move out to keep it under budget
- `src/gobby/hooks/hook_manager_dispatch.py`
- `src/gobby/sessions/summarize.py::*` — scope-reason: constructs a default `TranscriptAnalyzer()` and must pass the session-source parser
- `src/gobby/sessions/summary_generation.py::*` — scope-reason: constructs a default `TranscriptAnalyzer()` and calls `transcript_processor.extract_turns_since_clear` on the factory-supplied parser
- `src/gobby/cli/sessions.py::*` — scope-reason: constructs a default `TranscriptAnalyzer()`
- `src/gobby/mcp_proxy/tools/sessions/_summary_metadata.py::*` — scope-reason: constructs a default `TranscriptAnalyzer()`
- `tests/sessions/test_sessions_analyzer.py::*` — scope-reason: the analyzer default-parser case flips from asserting the Claude parser to asserting a registry-resolved parser per source
- `src/gobby/sessions/transcript_index.py::*` — scope-reason: four direct _get_parser call sites migrate to the shared registry
- `src/gobby/sessions/transcript_reader.py::*` — scope-reason: three direct _get_parser call sites migrate to the shared registry
- `src/gobby/sessions/transcript_window.py::*` — scope-reason: the direct _get_parser call site migrates to the shared registry
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: registry and unknown-source tests re-anchor from _get_parser to the shared registry entry point, and the frozen registry assertion gains the agy entry in 4.2
- `tests/sessions/transcripts/test_droid_parser.py::*` — scope-reason: droid parser tests import _get_parser and migrate to the registry entry point
- `tests/sessions/test_sessions_lifecycle.py::*` — scope-reason: transcript-processing lifecycle cases patch the module-local ClaudeTranscriptParser constructor and migrate to the shared registry seam
- `tests/cli/test_tokens_cli.py::*` — scope-reason: the tokens CLI parse-error case patches the module-local ClaudeTranscriptParser and migrates to the registry seam
- `tests/sessions/test_summarize.py::*` — scope-reason: summary-path cases patch the Droid, Qwen, and Claude parser constructors and migrate to the registry seam
- `tests/sessions/test_token_tracker_attribution.py::*` — scope-reason: token-attribution cases patch the transcript_processing Codex and Qwen parser aliases and migrate to the registry seam

Seven independent source-to-parser sites exist: `PARSER_REGISTRY` plus `get_parser` in
`transcripts/__init__.py`; a duplicate if/elif `_get_parser` in `transcript_parsing.py`; and
three more inline chains — in `TranscriptProcessingMixin._process_session_transcript`,
`_build_summary_prompt_context`, and `_load_session_messages`. Two of those **default to the
Claude parser for unknown sources**, so a new provider silently mis-parses rather than
failing loudly.

Two more sites hardcode Claude without any map: `TranscriptAnalyzer.__init__`
(`src/gobby/sessions/analyzer.py`) defaults `parser` to `ClaudeTranscriptParser()` and is
constructed with no argument in `summarize.py`, `summary_generation.py`,
`cli/sessions.py`, and `mcp_proxy/tools/sessions/_summary_metadata.py`; and
`HookManagerFactory.create` (`src/gobby/hooks/factory.py`) builds one
`ClaudeTranscriptParser(logger_instance=…)` as `HookManagerComponents.transcript_processor`
(typed as the Claude class) that `HookManager` stores and the handoff/summary paths
use for every session regardless of source. Both sites migrate to the registry:
the analyzer takes a registry-resolved parser for the session's source (no Claude
default), and the factory supplies the registry entry point — the component's type
becomes the `TranscriptParser` protocol — with consumers resolving per session source.
`hooks/hook_manager.py` is 864 lines and is touched here and again in 4.1, so this
refactor deliverable performs its decomposition first: the webhook and MCP dispatch
helpers (`_evaluate_blocking_webhooks`, `_dispatch_webhooks_sync`,
`_dispatch_webhooks_async`, `_dispatch_mcp_calls`, `_run_coro_blocking`) and the
dispatcher-shutdown helpers (`_close_webhook_dispatcher_async`,
`_close_webhook_dispatcher_sync`, `_log_webhook_dispatcher_close_failure`) move to a
mixin in the new module `src/gobby/hooks/hook_manager_dispatch.py` that `HookManager`
inherits, with no behavior change and the existing hook-manager suites passing unchanged.

Collapse to the single `PARSER_REGISTRY` + `get_parser` entry point. Delete `_get_parser` and
the three inline chains, routing all callers through the registry. Preserve the existing
`droid` special case (it alone takes `transcript_path`) by generalizing the signature rather
than keeping a branch. Unknown sources must raise, never fall back to Claude.

Deleting `_get_parser` reaches beyond the five maps: it has direct runtime consumers in
`transcript_index.py` (four call sites), `transcript_reader.py` (three), and
`transcript_window.py` (one), a same-module caller — `_parse_lines`
(`transcript_parsing.py:60`) — and test imports in `test_transcript_parsers.py` and
`test_droid_parser.py`. Every one of those callers, `_parse_lines` included, migrates to
`transcripts.get_parser` in this deliverable — a deletion that leaves any of them on the
removed symbol is an import error, not a refactor.

The registry also becomes the only patchable seam, and the constructor-patch sweep is
repository-wide, not Claude-only. `tests/sessions/test_sessions_lifecycle.py` patches
`transcript_processing.ClaudeTranscriptParser` at eight sites,
`tests/cli/test_tokens_cli.py` patches `tokens_module.ClaudeTranscriptParser`,
`tests/sessions/test_summarize.py` patches the Droid, Qwen, and Claude constructors at
their `transcripts.*` sources, and `tests/sessions/test_token_tracker_attribution.py`
patches the `transcript_processing` Codex and Qwen aliases — module-local patch targets
that registry routing would bypass, leaving those mocks silently inert, and that alias
deletion would break before any behavior is tested. All four suites migrate their
patches to the shared `get_parser`/registry seam in this deliverable.

**Acceptance:**

- 2.1.1 - `_get_parser` is deleted and its callers — including the same-module `_parse_lines` — route through the shared registry. file: `src/gobby/sessions/transcript_parsing.py`.
- 2.1.2 - The inline parser chain is removed from `_process_session_transcript` and its caller routes through the registry. symbol: `TranscriptProcessingMixin._process_session_transcript`. file: `src/gobby/sessions/transcript_processing.py`.
- 2.1.3 - An unknown source raises rather than silently returning the Claude parser. symbol: `get_parser`. file: `src/gobby/sessions/transcripts/__init__.py`.
- 2.1.4 - The inline parser chain is removed from `_build_summary_prompt_context` and an unknown source raises there. symbol: `_build_summary_prompt_context`. file: `src/gobby/sessions/summary_context.py`.
- 2.1.5 - The inline parser chain is removed from `_load_session_messages` and an unknown source raises there. symbol: `_load_session_messages`. file: `src/gobby/cli/tokens.py`.
- 2.1.6 - The direct `_get_parser` call sites in `transcript_index.py`, `transcript_reader.py`, and `transcript_window.py` migrate to the shared registry, and the droid-path and unknown-source regressions are re-anchored to the registry entry point. test: `tests/sessions/test_transcript_parsers.py`.
- 2.1.7 - Every test that patches a module-local or module-aliased parser constructor — Claude, Codex, Droid, or Qwen — migrates to the shared registry seam, covering transcript processing, summary context, summarization, token attribution, message loading, and token CLI behavior. test: `tests/sessions/test_sessions_lifecycle.py`.
- 2.1.8 - `TranscriptAnalyzer` no longer defaults to the Claude parser; every constructor call site passes a registry-resolved parser for the session's source, and an unknown source raises. symbol: `TranscriptAnalyzer.__init__`. file: `src/gobby/sessions/analyzer.py`.
- 2.1.9 - The hook-manager factory constructs no provider-specific parser: `HookManagerComponents.transcript_processor` is typed as the parser protocol and resolved through the shared registry per session source at its consumers, with a test proving a non-Claude session is not parsed by the Claude parser. symbol: `HookManagerFactory.create`. file: `src/gobby/hooks/factory.py`.
- 2.1.10 - `HookManager`'s webhook/MCP dispatch and dispatcher-shutdown helpers live in the `hook_manager_dispatch.py` mixin; `hook_manager.py` stays below 1,000 lines with headroom for 4.1's receipt-staged commit adoption, and the existing hook-manager suites pass unchanged. file: `src/gobby/hooks/hook_manager_dispatch.py`.

### 2.2 Normalize transcript discovery to hook-first with disk fallback [category: refactor] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/transcripts.py::derive_transcript_path`
- `src/gobby/hooks/event_handlers/_session_start/__init__.py::SessionStartMixin._derive_transcript_path`
- `src/gobby/sessions/transcript_paths.py::find_transcript_on_disk`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `src/gobby/agents/watchdog/transcript_resolver.py::*` — scope-reason: late-recovery caller adopts the split discovery contract with explicit caller context
- `src/gobby/tasks/transcript_evidence.py::*` — scope-reason: validation-evidence recovery caller adopts the split discovery contract
- `src/gobby/sessions/transcript_reader.py::*` — scope-reason: the thread-offloaded recovery in TranscriptReader adopts the split late-recovery contract at its real caller
- `tests/hooks/test_transcript_path_derivation.py::*` — scope-reason: derivation tests gain usable/pending/invalid classification, bounded-retry, and fallback cases
- `tests/sessions/test_transcript_reader.py::*` — scope-reason: reader-side discovery tests move to the split contract
- `tests/agents/test_idle_check_transcript_paths.py::*` — scope-reason: watchdog recovery tests patch the resolver-seam discovery and re-anchor to the split contract
- `tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py::*` — scope-reason: the idle-recovery integration case patches resolver-seam discovery and re-anchors to the split caller-context contract
- `tests/tasks/test_transcript_evidence.py::*` — scope-reason: validation-evidence recovery tests adopt the split contract with its caller context
- `src/gobby/sessions/transcript_source.py::_detect_source_from_path`
- `tests/sessions/test_transcript_source.py`

Discovery is inconsistent: claude/codex/droid read `transcript_path` from the hook payload,
qwen/grok derive it on disk, agy has neither. `derive_transcript_path` handles only qwen and
grok and returns `None` for everything else; `find_transcript_on_disk` carries a per-CLI
if/elif with no agy branch.

Give every provider the same two-stage contract — hook-reported first, disk-derived fallback —
with per-provider derivation expressed as data rather than branching control flow.

Hook-first is a usability test, not a truthiness test. A hook-reported path is
**usable** only if it exists and is readable. AGY is the concrete case: every event
carries camelCase `transcriptPath` (normalized to `transcript_path` by the adapter,
§4.1), and on `PreInvocation` it names
`~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript_full.jsonl`
(record 1.1.2, observed literally in both modes) **before the file exists** — so a
reported-but-absent path is **pending**: it gets a
bounded recheck on subsequent hook events (`PreToolUse`, `PostToolUse`,
`PostInvocation`, `Stop`) rather than blocking session start, and only a usable path
is persisted on the session. A malformed or unreadable path is **invalid** and falls
through to disk derivation immediately. Disk fallback is bounded: the per-provider
table yields direct candidate paths — for AGY the single candidate
`~/.gemini/antigravity-cli/brain/<external_id>/.system_generated/logs/transcript_full.jsonl`
(record 1.1.22: the complete, native-typed file; `transcript.jsonl` is its truncated
twin and is never a candidate) — never an unbounded directory traversal on
the synchronous hook path. The same table drives path-shape detection:
`_detect_source_from_path` (`sessions/transcript_source.py`) recognizes
`.codex/sessions`, `.qwen`, `.grok/sessions`, `.factory/sessions`, and
`.claude/projects` but nothing under `.gemini/antigravity-cli`, so an AGY transcript
path currently detects as no source; it gains the AGY rule from the table, pinned by
the new `tests/sessions/test_transcript_source.py` suite.

The classifier must own the value at the real caller. `handle_session_start`
(`flow.py`) currently accepts any truthy `input_data["transcript_path"]` directly and
calls the derivation helper only when the reported value is falsy — so a classifier that
lives solely inside the helpers never governs the primary path. Every hook-reported path
routes through the classifier before selection or persistence, at both session-start
acceptance sites in `flow.py`. `flow.py` (934 lines) is inside the Constraints line
budget; if this routing projects it at or above 1,000, decompose in the same task.

Discovery is two contracts, not one. `find_transcript_on_disk` is shared by synchronous
session-start handling, the agent watchdog (`watchdog/transcript_resolver.py`), the
transcript reader's thread-offloaded recovery — `TranscriptReader._ensure_transcript_path`
calls it via `asyncio.to_thread` in `transcript_reader.py` — and validation-evidence
recovery (`tasks/transcript_evidence.py`). The synchronous hook path gets bounded direct
candidates; the late-recovery callers keep discovery through an explicit contract that
carries the caller context (source, external id) they already pass — a helper-only
rewrite may neither retain blocking traversal on the hook path nor strand a recovery
caller. The recovery callers own regression seams of their own: the watchdog suite
(`tests/agents/test_idle_check_transcript_paths.py`) patches discovery at the resolver
seam and asserts cache, invalid-path, attempted-path, and fallback behavior, and the
validation-evidence suite pins evidence recovery — both re-anchor to the split contract
at the real callers, not only through the derivation suite. So does the lifecycle-monitor
integration case: `tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py` patches
`watchdog.transcript_resolver.find_transcript_on_disk` directly and asserts stale-session
discovery mutates no session row; it re-anchors to the split caller-context contract with
that no-mutation assertion preserved.

**Acceptance:**

- 2.2.1 - Every provider resolves through one hook-first/disk-fallback path. symbol: `derive_transcript_path`. file: `src/gobby/hooks/event_handlers/_session_start/transcripts.py`.
- 2.2.2 - Per-provider disk derivation is table-driven rather than an if/elif chain. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 2.2.3 - Hook-reported paths are classified usable, pending, or invalid; pending paths get a bounded recheck without blocking session start, and only usable paths are persisted. symbol: `derive_transcript_path`. file: `src/gobby/hooks/event_handlers/_session_start/transcripts.py`.
- 2.2.4 - Disk fallback derives bounded direct candidates from the per-provider table, with no unbounded traversal on the synchronous hook path. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 2.2.5 - Session-start flow routes every hook-reported path through the classifier before selection or persistence — a truthy but absent or unreadable path is never persisted directly. symbol: `handle_session_start`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 2.2.6 - The watchdog, transcript-reader (`TranscriptReader._ensure_transcript_path`), and validation-evidence recovery callers retain discovery through the split contract with explicit caller context, with usable/pending/invalid, bounded-retry, and fallback cases tested. test: `tests/hooks/test_transcript_path_derivation.py`.
- 2.2.7 - Watchdog recovery keeps caller-context discovery through the split contract: the resolver-seam suite re-anchors its cache, invalid-path, attempted-path, and fallback cases to the new contract at the real caller. test: `tests/agents/test_idle_check_transcript_paths.py`.
- 2.2.8 - Validation-evidence recovery keeps caller-context discovery through the split contract, with its recovery cases re-anchored at the real caller. test: `tests/tasks/test_transcript_evidence.py`.
- 2.2.9 - The stale-session idle-recovery case re-anchors to the split caller-context contract, preserving the assertion that discovery does not mutate the session row. test: `tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py`.
- 2.2.10 - The per-provider table carries the AGY entry — the `brain/<external_id>/.system_generated/logs/` direct candidate naming the file record 1.1.22 proves — and a hook-payload fixture line proves the pending→usable transition across consecutive AGY events. symbol: `find_transcript_on_disk`. file: `src/gobby/sessions/transcript_paths.py`.
- 2.2.11 - `_detect_source_from_path` returns `agy` for the `.gemini/antigravity-cli/brain/` path shape and continues to return `None` for unknown shapes. symbol: `_detect_source_from_path`. file: `src/gobby/sessions/transcript_source.py`.
- 2.2.12 - Validation-evidence recovery resolves an AGY session's transcript through the table instead of failing at `_resolve_transcript_path`. symbol: `_resolve_transcript_path`. file: `src/gobby/tasks/transcript_evidence.py`.

### 2.3 Reconcile critical_hooks and document the fail-open reality [category: code]
`kind: deliverable`

Targets:
- `crates/ghook/src/cli_config.rs::CliConfig::for_cli`
- `crates/ghook/src/cli_config.rs::agy_uses_antigravity_hook_contract`
- `crates/ghook/src/cli_config.rs::droid_recognized_with_no_critical_hooks`
- `crates/ghook/src/cli_config.rs::codex_stop_is_critical`
- `crates/ghook/src/cli_config.rs::qwen_current_critical_hooks`
- `crates/ghook/src/cli_config.rs::CliConfig::malformed_input_exit_code`
- `crates/ghook/src/action.rs::action_from_failure`
- `crates/ghook/src/action.rs::action_from_failure_blocks_critical_hooks`
- `crates/ghook/src/action.rs::skip_stdout_json`
- `crates/ghook/src/action.rs::action_from_failure_returns_json_for_noncritical_hooks`
- `crates/ghook/tests/contract.rs::*` — scope-reason: contract tests asserting per-CLI critical-hook and fail-open behavior are updated wholesale to the revised policy, and the agy rows use the five real events
- `crates/ghook/src/diagnose.rs::*` — scope-reason: the module-local terminal-hook criticality matrix test pins Codex and Qwen Stop as critical and updates wholesale to the final six-provider matrix, and `agy_uses_antigravity_pascal_case_hooks` is rewritten to the five real AGY events
- `src/gobby/hooks/events.py::*` — scope-reason: the module-level `EVENT_TYPE_CLI_SUPPORT` table gains AGY's five native event rows
- `tests/hooks/test_events.py::TestEventTypeMapping`
- `docs/guides/sandboxing.md`
- `docs/guides/ghook-user-guide.md`
- `docs/guides/hook-schemas.md`

`critical_hooks` (declared per CLI in `CliConfig::for_cli`) is arbitrary: claude 3, qwen 4,
grok 3, codex 2, agy 1, droid 0 — and droid short-circuits in `action_from_failure`
(`crates/ghook/src/action.rs`) *before* the criticality check, so adding entries for droid
would do nothing. AGY's single entry is `SessionStart`, which **AGY cannot emit** (its five
events are `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`), making it
dead configuration.

Normalize the policy to session-lifecycle events only, drop AGY's unreachable `SessionStart`
entry, and fix droid's short-circuit so its declaration is meaningful. The final
critical-hook set is stated per provider, in each CLI's native casing, derived from the
proven hook vocabularies (`claude_contract.py`, `CODEX_EVENT_MAP`, `QWEN_HOOK_CONTRACTS`,
grok's snake_case map, `DROID_HOOK_CONTRACTS`, `agy_contract.py`):

| CLI | Final critical set | Delta from today |
| --- | --- | --- |
| claude | `session-start`, `session-end`, `pre-compact` | unchanged |
| codex | `SessionStart`, `SessionEnd`, `PreCompact` | drops `Stop`; gains `SessionEnd`, `PreCompact` |
| qwen | `SessionStart`, `SessionEnd`, `PreCompact` | drops `Stop` |
| grok | `session_start`, `session_end`, `pre_compact` | unchanged |
| droid | `SessionStart`, `SessionEnd`, `PreCompact` | was empty and short-circuited; vocabulary proven in `DROID_HOOK_CONTRACTS` |
| agy | ∅ | drops unreachable `SessionStart`; none of AGY's five events is session-lifecycle, and the turn-level `PreInvocation` carrying 4.1's synthetic registration stays noncritical; also corrects the diagnose test, user guide, and hook-schemas guide that repeat the dead entry |

Turn-level events (`Stop` and every tool or prompt hook) are never critical: a daemon
outage must not block every turn. Then state the honest posture in the sandboxing guide:
**no CLI fails closed on `PreToolUse`**, so a daemon outage degrades every permission
denial to allow. This is currently true, tested, and undocumented.

The fail-open tail has a second defect that dispatch exposed. `action_from_failure`
answers a noncritical daemon failure with exit 1 and stdout
`{"status":"error","message":…}` — locked in by `contract.rs`'s agy `Stop` fail-open
row and by `action_from_failure_returns_json_for_noncritical_hooks`. That object is
protojson-illegal for AGY on every event, exactly as `{"continue":true}` was before
#20624, so a daemon outage would hand AGY an invalid hook response. The tail routes
through the per-CLI skip JSON (`skip_stdout_json`) when `cfg.source == "agy"`
(`{"decision":"allow"}` on `PreToolUse`, `{}` otherwise) and carries the message on
stderr; other CLIs keep their current stdout. Record 1.1.24 fixed the exit code AGY
tolerates for that path: a `PreToolUse` hook that exits **1 or 2** blocks the tool
even with legal stdout (`JSON hook … failed: command failed: exit status N` — AGY is
fail-closed on hook failure), while a nonzero `Stop` exit is ignored. So the AGY
fail-open tail must exit **0** with the skip JSON on every event; exit 1 would turn
every daemon outage into a blocked tool call, the opposite of the documented posture.

Declared vocabularies must be true as well. `diagnose.rs`'s
`agy_uses_antigravity_pascal_case_hooks` probes `SessionStart` (critical) and
`UserPromptSubmit` — neither exists on AGY — and is rewritten to the five real events,
all noncritical. `docs/guides/ghook-user-guide.md` and `docs/guides/hook-schemas.md`
repeat the `SessionStart`/`UserPromptSubmit` claim and are corrected in the same
change (7.1 owns the final matrix and fidelity rewrite of those guides; this
deliverable owns only the dead-event correction). On the Python side,
`EVENT_TYPE_CLI_SUPPORT` (`hooks/events.py`) sets every
event to `None` for agy while droid and grok declare explicit rows; AGY gains
`BEFORE_AGENT→PreInvocation`, `BEFORE_TOOL→PreToolUse`, `AFTER_TOOL→PostToolUse`,
`AFTER_AGENT→PostInvocation`, `STOP→Stop`, and `SESSION_START` stays `None` — the
synthetic session start (§4.1) is adapter-made, not a native event. This row set
lives here rather than in 4.1 because it is the Python twin of the ghook criticality
matrix this deliverable reconciles, it is fully determined today by
`adapters/agy_contract.py::AGY_HOOK_NAMES` with no Gate 0 dependency, and 4.1 is the
largest dispatch-gated section — a declarative truth fix should not wait on it.

**Acceptance:**

- 2.3.1 - AGY's unreachable `SessionStart` critical hook is removed. symbol: `CliConfig::for_cli`. file: `crates/ghook/src/cli_config.rs`.
- 2.3.2 - Droid's short-circuit no longer bypasses the criticality check. symbol: `action_from_failure`. file: `crates/ghook/src/action.rs`.
- 2.3.3 - The fail-open behavior of `PreToolUse` is documented explicitly. behavior: "PreToolUse denial degrades to allow when the daemon is unreachable" in `docs/guides/sandboxing.md`.
- 2.3.4 - `CliConfig::for_cli` declares exactly the final matrix above for all six CLIs, and a per-provider assertion pins each row — including the module-local `codex_stop_is_critical` and `qwen_current_critical_hooks` tests, both updated to assert noncritical `Stop` and its noncritical malformed-input exit code. That exit has its own production owner: `CliConfig::malformed_input_exit_code` today special-cases criticality only for Qwen while Codex inherits the provider-wide `json_error_exit_code` 2 for every hook, so editing `for_cli` alone cannot give Codex a noncritical `Stop` exit of 1 while critical lifecycle malformed input keeps exiting 2 — the method becomes criticality-driven for both providers, and the contract-suite malformed-stdin case pins the split end to end through `run_gobby_owned`. symbol: `CliConfig::for_cli`. file: `crates/ghook/src/cli_config.rs`.
- 2.3.5 - For every provider, a daemon-down contract test proves critical lifecycle hooks block and noncritical events fail open. test: `crates/ghook/tests/contract.rs`.
- 2.3.6 - The module-local diagnose criticality matrix matches the policy: `terminal_hook_criticality_matches_supported_cli_contracts` asserts the final six-provider matrix, including noncritical Codex and Qwen `Stop` and the critical lifecycle rows. file: `crates/ghook/src/diagnose.rs`.
- 2.3.7 - For agy, the noncritical fail-open tail emits the per-event protojson-legal skip JSON on stdout and the failure message on stderr; a unit case beside `action_from_failure_returns_json_for_noncritical_hooks` pins `PreToolUse` → `{"decision":"allow"}` and `Stop` → `{}`. symbol: `action_from_failure`. file: `crates/ghook/src/action.rs`.
- 2.3.8 - The daemon-down contract suite's agy rows use real events only: the agy `SessionStart` critical and malformed-stdin rows are removed, the agy `Stop` fail-open row asserts the legal skip JSON, and an agy `PreToolUse` fail-open row asserts `{"decision":"allow"}`. test: `crates/ghook/tests/contract.rs`.
- 2.3.9 - The module-local diagnose case for agy probes exactly `PreInvocation`, `PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`, all noncritical. symbol: `agy_uses_antigravity_pascal_case_hooks`. file: `crates/ghook/src/diagnose.rs`.
- 2.3.10 - `EVENT_TYPE_CLI_SUPPORT` declares AGY's five native events and keeps `SESSION_START` unset, and a mapping-matches-contract test mirrors the droid one against `AGY_HOOK_NAMES`. test: `tests/hooks/test_events.py`.
- 2.3.11 - The ghook user guide and hook-schemas guide list AGY's five events with no critical hook and no `SessionStart`/`UserPromptSubmit`. behavior: "AGY: PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop — none critical" in `docs/guides/ghook-user-guide.md`.

### 2.4 Share stream-reader limits and dedupe provider constants [category: code]
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidWebChatBackend`
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidManagedChatSession`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/ai/registry_builder.py::_tool_chat_binding`
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `tests/servers/websocket/chat/test_droid_backend.py::*` — scope-reason: existing droid backend tests gain the inactivity-timeout expiry, reset-on-activity, and cancellation cases

Three concrete duplications. `WebChatRuntimeManager.health` and
`WebChatRuntimeManager.create_session` each hardcode a **divergent copy** of
`AGY_UNAVAILABLE_REASON` (dropping "or agent spawning") instead of importing the constant
from `providers/registry.py`. `registry_builder.py` contains an unreachable agy tool-chat
branch, dead because the binding path already returns `None` for agy. And the ACP client
sets a 16 MiB `StreamReader` limit (`ACP_STREAM_READER_LIMIT_BYTES`,
`src/gobby/adapters/acp_client.py:86`) with a read timeout, while Droid inherits asyncio's
64 KiB default with no timeout — a >64 KiB NDJSON line raises `LimitOverrunError` and kills
the turn.

Import the shared constant into the runtime manager, delete the dead agy branch, and have
Droid's subprocess creation (`DroidWebChatBackend.attach_session`) pass
`ACP_STREAM_READER_LIMIT_BYTES` as the reader limit with a read timeout on its
`readline()` loops. The constant stays defined in `acp_client.py`; both NDJSON backends
import it.

The timeout is a contract, not a number. Reuse the ACP precedent —
`DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` (120 s, `acp_client.py:55`) — as an **inactivity**
timeout on each `readline()`, reset by every received line, with an env override following
the existing `GOBBY_<PROVIDER>_ACP_PROMPT_TIMEOUT_SECONDS` pattern. On expiry: emit exactly
one terminal error event for the turn, terminate the owned subprocess tree, remove the
session handle, and leave the session reconnectable — never a silent hang, an orphaned
Droid process, or a dead handle that blocks a new attach.

**Acceptance:**

- 2.4.1 - The runtime manager imports `AGY_UNAVAILABLE_REASON` instead of duplicating it. symbol: `WebChatRuntimeManager.health`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 2.4.2 - The unreachable agy tool-chat branch is deleted. symbol: `_tool_chat_binding`. file: `src/gobby/ai/registry_builder.py`.
- 2.4.3 - Droid's NDJSON reader uses the shared 16 MiB limit and the inactivity timeout, reset by received lines. file: `src/gobby/servers/websocket/chat/backends/droid.py`.
- 2.4.4 - Timeout expiry emits one terminal error, terminates the owned process tree, removes the handle, and leaves the session reconnectable, with focused tests covering expiry, reset-on-activity, and cancellation. test: `tests/servers/websocket/chat/test_droid_backend.py`.

### 2.5 AGY version-gate foundation [category: code] (depends: 2.4)
`kind: deliverable`

Targets:
- `src/gobby/providers/version_gate.py`
- `src/gobby/servers/_app_lifecycle.py::lifespan`
- `src/gobby/runner.py::run_gobby`
- `src/gobby/servers/provider_model_discovery.py::get_cli_version`
- `src/gobby/ai/registry_builder.py::_agy_unavailable_bindings`
- `src/gobby/servers/routes/providers.py::_agy_snapshot_payload`
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: the AGY provider payload asserts the published support record instead of a static snapshot
- `tests/providers/test_version_gate.py`
- `tests/test_runner_lifecycle.py::*` — scope-reason: run_gobby entry-point tests patch the version probe and assert publication precedes runner construction
- `tests/test_runner_pid_file.py::*` — scope-reason: the lock-contention branch asserts the version probe never runs in a losing daemon invocation

The 1.1.18 floor has consumers with incompatible seams. The only async version
probe in the tree, `get_cli_version` (`servers/provider_model_discovery.py`), has
**zero callers today** — the catalog that once imported it is gone — while every
consumer that must gate on the floor is synchronous: capability-registry construction
(`registry_builder.py`, whose `_agy_unavailable_bindings` hardcodes AGY unavailable),
`WebChatRuntimeManager` health, spawn gating, and the `/providers` route's
`_agy_snapshot_payload`. And 5.3 cannot gate on a deliverable (6.2) that depends on
5.3. Break both problems with one earlier foundation: an async startup probe in the
new module `src/gobby/providers/version_gate.py` resolves the installed AGY version
exactly once, reusing `get_cli_version` and `is_at_least_version`, and publishes an
immutable support record (installed version, required floor `1.1.18` as a module
constant, supported flag, actionable upgrade message naming both versions).
Synchronous consumers read the record; none of them await, subprocess, or re-probe.
Once per daemon start is the right cadence because AGY's auto-updater replaces the
binary in place between launches (Gate 0 watched 1.1.16 become 1.1.18 mid-run): an
install-time cache would go stale silently, while a per-start probe sees each upgrade.
A missing or unparseable binary yields an unsupported record with a truthful reason,
never an exception at read time. 5.3, 6.2 and 6.3 consume this record and depend on
this deliverable.

"Exactly one probe" is scoped to the daemon process. `utils/deps.py::get_agy_cli_version`
also runs `agy --version`, but for `gobby status` in the CLI process, where no record
is published; it stays independent and is not a consumer of this module. The
invariant is therefore: the daemon performs one AGY version subprocess call per
startup, and no daemon code path other than `version_gate` invokes `get_cli_version`
for agy.

The record has a concrete initialization owner, and it must run **before** any consumer
freezes its value — and that owner must be able to await. The FastAPI lifespan is too
late: `init_services` (`runner_init/services.py`, unchanged by this deliverable) builds
`ToolChatService` — and with it the capability registry — during runner initialization,
before the server (and its lifespan) starts, so
a lifespan-published record would leave a supported AGY frozen unavailable in the
already-built registry. The init seam itself cannot own the probe either: `init_services`
is a synchronous function invoked from `GobbyRunner.__init__`, and `run_gobby`
(`runner.py:263`) constructs the runner while the event loop is already running — a sync
seam cannot await `get_cli_version`, and `asyncio.run` there would nest event loops. The
awaitable owner is therefore `run_gobby` itself — anchored **after PID ownership
resolution**: `run_gobby` claims the singleton daemon lock first (`claim_pid_file`,
`runner.py:250`) and exits on contention before any subsystem work, so a probe placed
before that resolution would launch AGY subprocesses from losing daemon invocations.
The order is fixed — PID ownership resolution, then the awaited version probe and
record publication, then `GobbyRunner` construction — so every constructor-time
consumer — `build_daemon_tool_chat_service` included — reads an already-published
record and the probe runs only in the invocation that won the lock. The entry point has existing focused tests that this change reaches:
`TestRunGobbyFunction.test_run_gobby_creates_runner` (`tests/test_runner_lifecycle.py:2056`)
patches only `GobbyRunner` and calls `run_gobby` directly, so an unpatched probe would
launch a live AGY version subprocess inside a unit test. Those entry-point tests patch
the async probe and assert probe completion and record publication precede
`GobbyRunner` construction. The lifespan *asserts* publication at startup rather than performing it. Before
publication the module exposes a fail-closed sentinel — unsupported, reason "version
probe has not run" — so a read at any time returns a truthful record and never raises
or blocks. Every daemon consumer reads this one record: registry build
(`_agy_unavailable_bindings` becomes record-driven), runtime health, spawn gating, and
the `/providers` AGY payload (`_agy_snapshot_payload`). None re-probe.

**Acceptance:**

- 2.5.1 - An async startup probe resolves the AGY version once and publishes an immutable support record readable from synchronous consumers. file: `src/gobby/providers/version_gate.py`.
- 2.5.2 - Below the 1.1.18 floor, and when the binary is absent or unparseable, the record is unsupported with a message naming the installed and required versions. file: `src/gobby/providers/version_gate.py`.
- 2.5.3 - Focused tests cover supported, sub-floor, absent-binary and unparseable-output records, and prove sync consumers never trigger a re-probe. test: `tests/providers/test_version_gate.py`.
- 2.5.4 - `run_gobby` resolves PID ownership first, then awaits the probe and publishes the record, then constructs `GobbyRunner` — publication precedes every support-dependent service the synchronous init seam builds, and the probe never runs in an invocation that loses the daemon lock; the lifespan asserts publication, pre-publication reads return the fail-closed sentinel, a startup-order test in `tests/test_runner_lifecycle.py` proves publication precedes retained registry construction with no nested event-loop execution, the existing `run_gobby` entry-point tests patch the probe so no unit test launches a live version subprocess, and the lock-contention branch in `tests/test_runner_pid_file.py` asserts the probe is not called when acquisition loses. symbol: `run_gobby`. file: `src/gobby/runner.py`.
- 2.5.5 - The capability registry's AGY bindings and the `/providers` AGY payload read the published support record and never launch their own AGY version probe; a daemon-construction test proves the installed `ToolChatService` registry sees the record and a `/providers` request triggers no second probe, and `get_cli_version` has no agy caller outside `version_gate`. symbol: `_agy_unavailable_bindings`. file: `src/gobby/ai/registry_builder.py`.

### 2.6 Installer and status truthfulness [category: code]
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/agy.py::install_agy`
- `src/gobby/cli/installers/agy.py::_load_agy_hooks_template`
- `src/gobby/cli/_install_prompts.py::_run_standard_cli_install`
- `src/gobby/utils/deps.py::get_coding_cli_hooks_status`
- `src/gobby/utils/status.py::_format_coding_cli_details`
- `src/gobby/cli/installers/hook_commands.py::set_gobby_hook_timeouts`
- `tests/cli/installers/test_cli_installers_agy.py::*` — scope-reason: gains timeout propagation, `/hooks` verification, and verification-skipped cases
- `tests/install/test_agy_template.py::*` — scope-reason: the hardcoded 45 s template timeout becomes the configured value
- `tests/utils/test_deps.py::*` — scope-reason: AGY hooks-installed detection against `~/.gemini/config/hooks.json`
- `tests/utils/test_utils_status.py::*` — scope-reason: the "unavailable: no machine transport" AGY suffix is removed

Three surfaces still tell the user AGY hooks cannot work. `install_agy` is the only
standard installer not given `hook_timeout_seconds` — `_run_standard_cli_install`
special-cases `agy` to call it without the argument, so the template's hardcoded
45 s (`install/agy/hooks-template.json`, `AGY_HOOK_TIMEOUT_SECONDS`) ignores
`hooks.provider_timeout`. `get_coding_cli_hooks_status` hardcodes
`result["agy"] = False` ("no supported hook transport") instead of checking
`~/.gemini/config/hooks.json` for the `gobby` key, so `gobby status` never reports
AGY hooks installed; `_format_coding_cli_details` appends "unavailable: no machine
transport" for agy unconditionally. Since 1.1.12, `agy -p "/hooks" --output-format
json` lists registered hooks without an agent turn or quota spend (record 1.1.21), so
the installer can verify registration the way it cannot for any other CLI.

`install_agy` accepts `hook_timeout_seconds`, applies it to every handler in both
layouts, and after writing `hooks.json` runs the `/hooks` introspection when `agy` is
on `PATH`: the result reports `verified: true` with the registered gobby hook names,
`verified: false` with AGY's error when the file is rejected, or
`verification: "skipped"` when the binary is absent — never a failure of the install
itself. Status detection reads the same file and reports the hook marker truthfully;
the transport disclaimer is removed. Sub-floor binaries are not 2.6's concern (2.5 owns
the daemon gate; 6.2 owns advertised capabilities).

**Acceptance:**

- 2.6.1 - `install_agy` accepts `hook_timeout_seconds`, the standard-install dispatcher passes it like every other CLI, and both the flat-list and matcher-group handlers carry the configured timeout. symbol: `install_agy`. file: `src/gobby/cli/installers/agy.py`.
- 2.6.2 - After writing `hooks.json`, the installer verifies registration through `agy -p "/hooks" --output-format json` (shape per record 1.1.21) and reports verified, rejected-with-reason, or skipped-no-binary, without failing the install on verification. symbol: `install_agy`. file: `src/gobby/cli/installers/agy.py`.
- 2.6.3 - `get_coding_cli_hooks_status` detects the gobby hook in `~/.gemini/config/hooks.json` instead of hardcoding `False`. symbol: `get_coding_cli_hooks_status`. file: `src/gobby/utils/deps.py`.
- 2.6.4 - `gobby status` no longer appends "unavailable: no machine transport" for AGY. symbol: `_format_coding_cli_details`. file: `src/gobby/utils/status.py`.
- 2.6.5 - Installer tests cover timeout propagation, all three verification outcomes with a faked `agy`, and idempotence with verification; template and status tests are re-anchored. test: `tests/cli/installers/test_cli_installers_agy.py`.

## P3: Web-Chat SRT Migration
`kind: framing`

**Goal**: Bring web chat under the same sandbox boundary spawn already has.

### 3.1 Wrap web-chat backends in SRT [category: code] (depends: P2)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox.py::*` — scope-reason: web_chat_sandbox_config and daemon_owned_sandbox_policy_hash/web_chat_sandbox_policy_hash are reworked here while the resolver hierarchy is extracted out of this module (see sandbox_resolvers.py)
- `src/gobby/agents/sandbox_resolvers.py`
- `src/gobby/agents/srt_runtime.py::*` — scope-reason: prepare_sandbox_launch gains the web-chat callers while the Claude shim/lifetime design extends SandboxLaunch and adds shim-emission and cleanup symbols in the same module
- `tests/agents/test_srt_runtime.py::*` — scope-reason: the incumbent SRT runtime suite gains the shim-emission, cleanup, and provider_env-composition cases
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.start`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.__init__`
- `src/gobby/runner_init/servers.py::init_servers`
- `src/gobby/config/app.py::DaemonConfig`
- `src/gobby/config/daemon_sandbox.py::DaemonOwnedSandboxConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: the runtime config contract carries the flipped web_chat_sandbox default (derived carrier for the config models)
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager._refresh_sandbox_config`
- `src/gobby/servers/websocket/chat/backends/droid.py::DroidWebChatBackend.attach_session`
- `src/gobby/servers/websocket/chat/backends/acp.py::ACPWebChatBackend`
- `src/gobby/adapters/acp_client.py::*` — scope-reason: the ACP subprocess launch moves to session-owned lifetime and gains SRT argv wrapping for the grok/qwen backends
- `src/gobby/sessions/acp_lifecycle.py::ACPSessionLifecycleService`
- `src/gobby/servers/routes/sessions/acp.py::*` — scope-reason: the production route's _service constructor gains the dependencies the persisted-workspace lifecycle contract requires
- `src/gobby/storage/session_models.py::*` — scope-reason: the Session model, row hydration, and serialization gain the persisted canonical workspace identity
- `src/gobby/storage/sessions/_crud.py::*` — scope-reason: session registration persists the canonical workspace-identity column
- `src/gobby/agents/session.py::*` — scope-reason: child-session registration persists the spawn-time canonical workspace identity
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: spawn preparation resolves the worktree workspace persisted on the pre-created child session
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `tests/agents/test_spawn_executor.py::*` — scope-reason: the spawn-time workspace persistence case joins the spawn-executor suite
- `crates/gcore/assets/schema/migrations/402_sessions_workspace_path.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: the 402 EmbeddedMigration entry registers the migration in MIGRATIONS
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for the sessions workspace-identity column
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: latest_version, latest_checksum, and assets_root_hash follow the new latest asset
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the pinned latest_version literal follows the new latest asset
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: the expected schema-identity latest_version follows the new latest asset
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the version-json schema-identity assertions follow the new latest asset
- `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`
- `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `src/gobby/servers/websocket/handlers/session_config.py::handle_set_project`
- `src/gobby/servers/websocket/handlers/session_config.py::handle_set_worktree`
- `src/gobby/storage/worktrees.py::LocalWorktreeManager.delete`
- `tests/servers/websocket/test_set_worktree.py::*` — scope-reason: project-switch and worktree-switch cases assert workspace-identity update or invalidation before teardown
- `tests/storage/test_schema_contract.py::*` — scope-reason: the identity-pin and no-Python-DDL contracts cover the new migration
- `tests/storage/sessions/test_metadata.py::*` — scope-reason: the workspace-identity column round-trips through session persistence
- `tests/storage/sessions/test_storage_sessions_models.py::*` — scope-reason: Session serialization gains the workspace-identity field
- `tests/servers/websocket/chat/test_provider_backends.py::*` — scope-reason: provider-backend regressions re-anchor from the warm shared ACP backend to operation-owned client acquisition
- `tests/servers/routes/test_sessions_acp_routes.py::*` — scope-reason: ACP close/delete route cases adopt the operation-owned client contract for inactive and post-restart sessions
- `tests/sessions/test_acp_lifecycle_service.py::*` — scope-reason: the acp_backend fake migrates to the operation-owned client contract and gains workspace-recovery and failure-branch finalization cases
- `tests/config/test_daemon_sandbox.py::*` — scope-reason: the default assertions migrate from provider-native/allow_network=True to the srt bounded-network default while preserving explicit provider-native override coverage
- `tests/servers/test_chat_session.py::*` — scope-reason: Claude SDK session start/stop and option-cleanup assertions migrate to the SRT shim launch contract
- `tests/servers/websocket/chat/test_servers_websocket_chat_session.py::*` — scope-reason: the hydration/start seam suite re-anchors to the post-hydration SRT launch order
- `src/gobby/agents/resume_executor.py::*` — scope-reason: imports get_sandbox_resolver/coerce_sandbox_config from the extracted module
- `src/gobby/agents/spawn_executor_support.py::*` — scope-reason: imports get_sandbox_resolver from the extracted module
- `src/gobby/agents/tmux/spawner.py::*` — scope-reason: imports get_sandbox_resolver from the extracted module
- `tests/agents/test_sandbox.py::*` — scope-reason: resolver imports re-anchor to sandbox_resolvers
- `tests/agents/test_srt_filesystem_integration.py::*` — scope-reason: merge_claude_settings import re-anchors to sandbox_resolvers
- `src/gobby/servers/websocket/chat/backends/codex.py::*` — scope-reason: the app-server launch moves to session-owned lifetime; provider-native policy threading is replaced by SRT with the CLI's own sandbox pinned off
- `src/gobby/servers/chat_session.py::*` — scope-reason: the Claude SDK session gains SRT via a daemon-emitted executable shim assigned to ClaudeAgentOptions.cli_path
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: ChatSessionMixin._create_chat_session_inner and the session lifecycle owners gain the post-hydration SRT launch seam, extracted into _session_launch.py
- `src/gobby/servers/websocket/chat/_session_launch.py`
- `tests/servers/websocket/chat/test_runtime_manager.py::*` — scope-reason: shared-client identity tests split into daemon-preservation and per-session ownership cases
- `tests/test_runner_lifecycle.py::*` — scope-reason: the shared-codex-client bootstrap identity test splits into daemon-preservation and per-session factory cases
- `tests/servers/websocket/chat/test_launch_contracts.py`

SRT adoption (commit `af1908b3e`) reached only the spawn path — the spawn and resume
executors and the tmux spawner. `prepare_sandbox_launch` is called from exactly two places,
both spawn. There are **zero** SRT references under `src/gobby/servers/websocket/`. Web chat
defaults to `backend="provider-native"` with `allow_network=True`, and of the backends only
Codex threads a real policy (`CodexSandboxResolver.thread_sandbox_policy`) — Claude, Droid
and the ACP backends store `sandbox_config` and never apply it. The web-chat sandbox config
is effectively decorative, feeding only a policy hash for resume invalidation.

Route web-chat subprocess launches through `prepare_sandbox_launch` and flip the
`web_chat_sandbox` default (declared on `DaemonConfig`) to `backend="srt"` with a
**bounded network policy**: `allow_network=False` plus the explicit allowed domains and
scoped Git/package capabilities web chat needs. `prepare_sandbox_launch` hard-refuses
`backend="srt"` with `allow_network=True` (`srt_runtime.py:429` raises an SRT lockout), so
the current `allow_network=True` value cannot survive the flip — the default must be a
policy SRT preflight accepts. Unrestricted networking under SRT is out of scope; wanting it
back means a separate SRT contract change with its fail-closed test updated first.
The flip lands in both default sites — the `DaemonConfig.web_chat_sandbox` field default
(`config/app.py`, with `DaemonOwnedSandboxConfig` in `config/daemon_sandbox.py` and the
runtime config contract `crates/gcore/assets/config/runtime_config_contract.json`
following) and `web_chat_sandbox_config`'s `default_backend`/`default_allow_network`
(`sandbox.py`). `daemon_owned_sandbox_policy_hash` (`_DAEMON_SANDBOX_POLICY_VERSION = 1`)
hashes only version/scope/enabled/mode/allow_network/extra_read_paths/extra_write_paths;
the complete policy must add backend, `allowed_domains`, `denied_domains`,
`allow_git_network`, `allow_package_registries`, `allow_unix_sockets`,
`extra_deny_read_paths`, `extra_deny_write_paths`.

Wrapping is an argv **and environment** composition, and both halves follow one
algorithm at every launch surface. `prepare_sandbox_launch` consumes the caller's
base environment and returns `SandboxLaunch.provider_env` separately
(`srt_runtime.py:66`), and the spawn path already merges it —
`env.update(launch.provider_env)` (`spawn_executor.py:127`) — so a web-chat backend
that passes only its own mapping to the subprocess drops `TMPDIR` and every other
provider variable the sandbox layer injected, while one that builds the SRT env
separately from its identity env drops the session identity instead. Every
web-chat launch therefore composes in one order: construct the identity/base
environment first (including the canonical session context 5.2 exports), pass that
same mapping into `prepare_sandbox_launch`, merge `launch.provider_env` into it,
and hand the merged mapping together with `launch.wrap(argv)` to subprocess
creation — never two divergent environment sources. The Claude shim and its
launch-time emission/cleanup live in `srt_runtime.py` as extensions of
`SandboxLaunch`, covered by the incumbent `tests/agents/test_srt_runtime.py`
suite.

Process ownership changes with the boundary. Today Codex's app-server and the grok/qwen ACP
servers start as warm daemon-shared subprocesses before any session project path exists,
while SRT preparation requires that path — so wrapping current startup cannot confine
concurrent projects. Kill the warm shared start: every web-chat subprocess becomes
**session-owned**, torn down when the session ends. The launch point is the session's
**asynchronous post-hydration seam**, not `create_session`: `WebChatRuntimeManager.create_session`
is synchronous and runs before `_session.py` resolves the session's `project_path`, so a
launch there would either block the event loop or confine the wrong workspace. The SRT
preparation and subprocess launch belong in the awaited session-start path —
`ChatSessionMixin._create_chat_session_inner` (`_session.py:217`) through
`ManagedChatSessionBase.start`/backend attach — after the final project path (including
worktree paths) is known. `create_session` stays the synchronous orchestration entry and
performs no subprocess launch; a failed start cleans up the partially-launched process and
handle. `_session.py` is 943 lines and `_create_chat_session_inner` alone spans ~640 of
them, so the seam work cannot land in place: the post-hydration launch path — provider
resolution, lifecycle-callback wiring (the creation-time `SESSION_START` pre-fire,
`_notify_mode_changed`, `_notify_plan_ready`), SRT preparation, and backend
start/attach — is split out of `_create_chat_session_inner` and moved to the new module
`src/gobby/servers/websocket/chat/_session_launch.py`, which `_session.py` calls after
hydration; 5.3's provider-conditional pre-fire then lands in that module.

Two things landed in `runtime_manager.py` since this plan was parked and the launch seam
composes with both. `WebChatRuntimeManager.__init__` now takes
`config_resolver: Callable[[], DaemonConfig | None]` and `create_session` begins with
`_refresh_sandbox_config()`, which re-reads the live `DaemonConfig`, recomputes
`web_chat_sandbox_config`/`web_chat_sandbox_policy_hash`, and pushes `set_sandbox_config`
to every backend. The post-hydration launch must consume *that* per-creation config and
hash — one resolution per `create_session`, never a second `DaemonConfig` read at launch
time, or the hash the session persists and the policy SRT enforces can diverge. Second,
`create_session` rejects any provider whose `PROVIDER_CAPABILITIES` row lacks
`sensitive_path_enforcement` when sandboxing is enabled (today only Claude sets it).
Under `backend="srt"` the sensitive-root contract is SRT's, so the gate is satisfied by
the SRT boundary for every provider; under explicit provider-native it remains per
provider. The `policy_mismatch_reason` consumers (`servers/routes/sessions/core.py`,
`servers/routes/agent_spawn.py`, `servers/websocket/handlers/session_observe_continue.py`,
`_session.py`) call `web_chat_sandbox_policy_hash` and need no code change when the hash
version bumps. Launch surfaces:

- **Droid**: the subprocess spawn in `DroidWebChatBackend.attach_session` (already
  per-session; gains the SRT wrap).
- **Grok/Qwen (ACP)**: the ACP server subprocess launched by `acp_client.py`, moved from
  shared-warm to session-owned lifetime. The ownership split reaches the operational
  lifecycle routes, not only chat turns: `ACPSessionLifecycleService`
  (`sessions/acp_lifecycle.py:186`) obtains the runtime manager's shared ACP backend to
  drive `session/close` and `session/delete` — including for inactive and post-restart
  sessions with no live managed session. Preserving that path after the warm shared
  client is killed would retain an unconfined daemon-owned process, so close/delete
  instead obtain an operation-owned client launched under the target session's resolved
  project path and SRT policy, preserve the existing capability gating
  (`acp_session_capabilities`) and close/expire/delete fallback semantics, and no warm
  shared ACP subprocess remains anywhere. The confinement root must be durable, and the
  resolver path cannot make it so: `Session` persists `project_id` but no final
  `project_path` or worktree identity, live worktree overrides are in-memory, and
  `resolve_session_workspace` (`servers/session_changes.py:140`) derives isolated
  worktrees from **active task claims** (`_resolve_isolated_workspace` lists tasks by
  `claimed_by_session_id`) that release, closure, and escalation clear — after which it
  silently falls back to the repository root, exactly the confinement regression this
  deliverable exists to prevent; the production route constructor
  (`servers/routes/sessions/acp.py::_service`) also lacks the task-manager dependency
  that resolver requires. The contract is therefore **persisted workspace identity**:
  the canonical resolved workspace path is recorded on the session row when the
  session's workspace is first resolved — spawn-time worktree, web-chat post-hydration
  project path, or session-start adoption — carried by the numbered migration
  `crates/gcore/assets/schema/migrations/402_sessions_workspace_path.sql`, which this
  deliverable claims; 4.1's claim-generation and receipt-effects migrations are 403
  and 404. The live migration home is the gcore embedded-asset set: 371–375 were
  consumed pre-park and flattened into the sealed baseline at `BASELINE_VERSION` 375,
  376–401 are registered in `crates/gcore/src/schema/assets.rs`, and the plan claims
  the contiguous 402–404 range after the latest applied asset
  (`401_model_metadata_reasoning.sql`). Each migration lands through the full
  embedded-asset contract in the Constraints — `EmbeddedMigration` entry in
  `assets.rs`, regenerated `catalog.manifest.json`, refreshed
  `src/gobby/storage/schema_expected_identity.json`, the `latest_version` pins in
  `crates/gcore/src/grant/bundle.rs` and `grant/tests.rs`, the identity assertions in
  `crates/gcore/tests/schema_contract.rs` and `crates/gdaemon/tests/cli_contract.rs`,
  the `MIGRATIONS` enumeration in `runner_tests.rs`, the
  `catalog_manifest_freshness.rs` counts, and the signed golden grant vectors under
  `tests/runtime_grants/golden/` — and never edits `baseline.sql`. Python carries no
  DDL. The row change is mirrored in the `Session` model/row hydration and
  serialization. The identity is mutable state, not a
  write-once record: a project switch (`handle_set_project`) or worktree
  switch (`handle_set_worktree`) atomically updates or invalidates the
  persisted identity before the old session's teardown, and worktree
  deletion (`LocalWorktreeManager.delete`) tombstones every persisted
  identity referencing the removed path — a later close/delete on a
  tombstoned or stale identity fails closed exactly as an absent one does,
  never consuming a path from the old confinement root.
  Each first-resolution writer is in scope, not only the schema:
  spawn preparation passes the resolved worktree into child-session registration
  (`agents/spawn_executor.py`, `agents/session.py`), hook adoption persists it in
  `handle_session_start`, and web-chat hydration persists it at the post-hydration
  seam — pre-created spawn, ordinary hook adoption, and web-chat hydration all
  persist the same validated identity before any later close/delete depends on
  it. Close/delete consume that persisted value, validate it still lies
  within the session's project or worktree confinement, and fail closed with a truthful
  lifecycle error when it is absent or invalid — never a repository-root fallback. The
  operation-owned client is acquired through an async context manager or try/finally
  and finalized on every branch — ACP failure, storage failure, and success alike.
- **Codex**: the app-server subprocess, moved from shared-warm to session-owned lifetime;
  the provider-native policy threading is superseded. This split reaches runner
  bootstrap, not just the backend: `init_servers` (`runner_init/servers.py:191-193`) creates
  one daemon `CodexAppServerClient` and hands the same instance to both
  `WebChatRuntimeManager` and `HTTPServer`, and the `HTTPServer` uses it for hook and
  session synchronization that has nothing to do with web chat. The bootstrap therefore
  splits ownership: the daemon keeps a daemon-owned synchronization client serving the
  `HTTPServer` consumers, unchanged in lifetime, while `WebChatRuntimeManager.__init__`
  stops receiving the shared instance and instead takes a per-session client factory
  used at the post-hydration launch seam. Existing tests asserting shared-instance
  identity split into daemon-preservation and per-session confinement cases — concretely
  `TestInitSubsystems.test_init_servers_wires_shared_codex_client_to_chat_backends`
  (`tests/test_runner_lifecycle.py:321`), which today requires `HTTPServer` and
  `WebChatRuntimeManager` to receive the same client instance and directly contradicts
  the factory split.
- **Claude**: the Agent SDK session in `chat_session.py`. `ClaudeAgentOptions.cli_path`
  (consumed by `SubprocessCLITransport`) accepts one executable path while `SandboxLaunch`
  exposes a wrapped argv vector, so the daemon emits a fresh per-launch executable shim
  that execs the SRT-wrapped argv with `"$@"` appended, and `cli_path` points at that shim.
  The shim is private API of the sandbox layer, written at launch time by the same daemon
  that computed the policy (no installed-binary skew), and removed on session teardown,
  disconnect, and failed start.

Preserve `policy_mismatch_reason` resume-invalidation semantics — existing sessions carrying
the old policy hash must surface the mismatch, never silently run under a different boundary.
For that guarantee to hold across this migration, `web_chat_sandbox_policy_hash` must cover
the **complete** normalized effective policy — backend, network fields, domains, Git/package
capabilities, socket allowances — with an explicit hash version; today it omits backend and
material network fields, so a provider-native session could retain its hash across the SRT
flip and resume silently under a different boundary.
Follow the established nesting precedent: when SRT is enforced, pin the CLI's own sandbox
off, exactly as the spawn path does for Claude (`--settings '{"sandbox": {"enabled":
false}}'`) and Codex (`sandbox_mode="danger-full-access"`).

Pin the launch seam with a parameterized launch-contract test matrix covering the five
incumbent providers — Claude, Codex, Droid, and the ACP pair (Grok/Qwen). For each path it
asserts: `prepare_sandbox_launch` contributes exactly one SRT wrapper (no double wrapping),
the bounded network policy is represented in the SRT policy and accepted by preflight, the
provider-native sandbox is disabled when SRT enforces, the explicit provider-native backend
remains usable, and a stale policy hash refuses resume. The AGY row joins this matrix in
5.2, where `AgyWebChatBackend` exists. This matrix is the concrete anchor for the V2
cross-provider regression item.

Line budget: `acp_client.py` is 955 lines with the ACP launch-ownership change landing in
it. If it projects at or above 1,000, extract the subprocess launch/lifetime seam into its
own module within this task, per the `decompose-monolith` constraint.
`sandbox.py` is 914 lines and both this deliverable (policy-hash rework) and 3.2
(`AgySandboxResolver`) add to it, so the resolver decomposition is performed **here**:
split `SandboxResolver`, its four subclasses, `get_sandbox_resolver`, and the Claude
settings helpers (`merge_claude_settings`, `preflight_provider_native_settings`,
`preflight_provider_native_settings_file`, `preflight_provider_native_settings_file_async`,
`materialize_claude_settings`, `materialize_claude_settings_async`) out of `sandbox.py`
and move them to `src/gobby/agents/sandbox_resolvers.py`, with no re-export shim (the
new module imports `SandboxConfig`/`ResolvedSandboxPaths` from `sandbox.py`, so
`sandbox.py` must not import it back). Importers follow: `agents/resume_executor.py`,
`agents/spawn_executor.py`, `agents/spawn_executor_support.py`, `agents/tmux/spawner.py`,
`servers/websocket/chat/backends/codex.py` (`CodexSandboxResolver`),
`agents/srt_runtime.py` (`SandboxResolver` type), and `tests/agents/test_sandbox.py`,
`tests/agents/test_srt_runtime.py`, `tests/agents/test_srt_filesystem_integration.py`.
3.2 then lands `AgySandboxResolver` in the new module.

**Acceptance:**

- 3.1.1 - Web-chat subprocess launches are wrapped by `prepare_sandbox_launch` at the asynchronous post-hydration seam, and no subprocess launch occurs in synchronous `create_session`. symbol: `ChatSessionMixin._create_chat_session_inner`. file: `src/gobby/servers/websocket/chat/_session.py`.
- 3.1.2 - The `web_chat_sandbox` default is `backend="srt"` with a bounded network policy that SRT preflight accepts. symbol: `DaemonConfig`. file: `src/gobby/config/app.py`.
- 3.1.3 - Sessions carrying a stale sandbox policy hash surface a mismatch rather than resuming under a changed boundary. symbol: `WebChatRuntimeManager.policy_mismatch_reason`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 3.1.4 - A CLI's own sandbox flag is pinned off when SRT is the enforcing boundary. file: `src/gobby/servers/websocket/chat/backends/codex.py`.
- 3.1.5 - A parameterized launch-contract matrix over the five incumbent providers pins exactly one SRT wrapper, bounded-network-policy representation, native-sandbox-off, provider-native usability, and stale-hash refusal per provider. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.6 - `web_chat_sandbox_policy_hash` covers the complete normalized effective policy with an explicit version, and changing the backend, a domain, a Git/package capability, or a socket allowance each produces a stale-resume refusal in tests. symbol: `web_chat_sandbox_policy_hash`. file: `src/gobby/agents/sandbox.py`.
- 3.1.7 - Codex and ACP subprocesses are session-owned: launched at the async post-hydration seam under the session's final project path and policy, torn down with the session, with failed-start cleanup, and tests covering first start, resume, failure, teardown, concurrent sessions, and two projects receiving distinct filesystem confinement under final worktree paths. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.8 - The Claude shim execs the SRT-wrapped argv, SDK-appended arguments pass through exactly one wrapper, and the shim is cleaned up on teardown, disconnect, and failed start. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 3.1.9 - `acp_client.py` remains below 1,000 lines, or its launch seam is decomposed in the same task. file: `src/gobby/adapters/acp_client.py`.
- 3.1.10 - The post-hydration launch path is extracted from `_create_chat_session_inner` into `_session_launch.py` and `_session.py` remains below 1,000 lines after the seam work. file: `src/gobby/servers/websocket/chat/_session_launch.py`.
- 3.1.11 - Runner bootstrap splits Codex client ownership: the daemon-owned synchronization client keeps serving the `HTTPServer` hook/session consumers while web-chat sessions construct per-session clients through the factory; the shared-identity assertion in `tests/test_runner_lifecycle.py` is replaced by daemon-preservation and per-session confinement cases. symbol: `init_servers`. file: `src/gobby/runner_init/servers.py`.
- 3.1.12 - ACP `session/close` and `session/delete` for inactive and post-restart sessions obtain an operation-owned client whose confinement root is the persisted session workspace identity — recorded at first workspace resolution, validated against project/worktree confinement at use, failing closed when absent or invalid, never falling back to the repository root — under the target session's SRT policy; capability gating and close/expire/delete fallback behavior are preserved, the client is finalized on success and every failure branch, no warm shared ACP subprocess remains, and closed-task, released-task, escalated, deleted-worktree, and post-restart cases plus ACP- and storage-failure finalization are tested. symbol: `ACPSessionLifecycleService`. file: `src/gobby/sessions/acp_lifecycle.py`.
- 3.1.13 - The daemon-sandbox config suite asserts the new `backend="srt"` bounded-network default and keeps explicit provider-native override coverage. test: `tests/config/test_daemon_sandbox.py`.
- 3.1.14 - The direct lifecycle suites migrate with the ownership change: the ACP lifecycle suite's `acp_backend` fake becomes the operation-owned client contract, the Claude chat-session suite's start/stop and option-cleanup assertions adopt the shim launch contract, and the websocket session suite pins the post-hydration launch order. test: `tests/sessions/test_acp_lifecycle_service.py`.
- 3.1.15 - The canonical workspace identity persists on the session row through migration `402_sessions_workspace_path.sql` with model, hydration, serialization, and embedded-asset identity coverage, and the production ACP route constructor supplies the lifecycle service's dependencies; every first-resolution writer persists the validated identity — spawn-time registration (with a persistence case in `tests/agents/test_spawn_executor.py`), hook adoption, and web-chat hydration. file: `crates/gcore/assets/schema/migrations/402_sessions_workspace_path.sql`.
- 3.1.16 - Every workspace-identity mutation writer is covered: `handle_set_project` and `handle_set_worktree` atomically update or invalidate the persisted identity before teardown, worktree deletion tombstones referencing identities, and tests cover project switch, worktree switch, deletion before close/delete, and restart before rehydration — a stale or tombstoned identity always fails closed at use. test: `tests/servers/websocket/test_set_worktree.py`.
- 3.1.17 - The resolver hierarchy and Claude settings helpers move from `sandbox.py` to `sandbox_resolvers.py` with no re-export shim; `resume_executor.py`, `spawn_executor.py`, `spawn_executor_support.py`, `tmux/spawner.py`, `backends/codex.py`, `srt_runtime.py`, and the agents test suites import from the new module, and `sandbox.py` stays below 1,000 lines after the policy-hash rework with headroom for 3.2. file: `src/gobby/agents/sandbox_resolvers.py`.
- 3.1.18 - The post-hydration launch consumes the sandbox config and policy hash produced by `_refresh_sandbox_config` for that `create_session` call — one `DaemonConfig` resolution per session creation — and the `sensitive_path_enforcement` gate in `create_session` is satisfied by the SRT boundary under `backend="srt"` while remaining per-provider under explicit provider-native; both are pinned per provider in the launch-contract matrix. symbol: `WebChatRuntimeManager._refresh_sandbox_config`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 3.1.19 - Migration 402 lands through the embedded-asset contract: an `EmbeddedMigration` entry in `assets.rs`, regenerated `catalog.manifest.json`, refreshed `schema_expected_identity.json` (`latest_version` 402), the `latest_version` pins in `grant/bundle.rs` and `grant/tests.rs`, the identity assertions in `schema_contract.rs` and `cli_contract.rs`, the `runner_tests.rs` enumeration, the freshness counts, the signed golden grant vectors, an untouched `baseline.sql`, and no DDL in Python. file: `crates/gcore/src/schema/assets.rs`.

### 3.2 Add the AGY sandbox resolver [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_resolvers.py`
- `src/gobby/agents/sandbox_policy.py::*` — scope-reason: the module-level provider maps (_PROVIDER_DOMAINS, _PROVIDER_AUTH_PATHS, _PROVIDER_AUTH_READ_ONLY_PATHS, _PROVIDER_CREDENTIAL_ENV) gain agy entries together
- `src/gobby/agents/provider_capabilities.py::*` — scope-reason: the AGY row lands in the module-level PROVIDER_CAPABILITIES table that provider_supports_sandbox consults
- `tests/agents/test_sandbox.py::*` — scope-reason: reachability and capability-gate cases for the agy resolver join the sandbox suite

`SandboxResolver` has subclasses for Claude, Codex, Qwen and Grok but none for AGY, so the
provider-native path has nothing to resolve. Add `AgySandboxResolver` to
`src/gobby/agents/sandbox_resolvers.py` modeled on `GrokSandboxResolver` (the smallest, at
19 lines, in `sandbox_resolvers.py` after 3.1), returning `--sandbox` for the
provider-native path and `--sandbox=false` when SRT is the enforcing boundary — the
boolean form Gate 0 recorded under 1.1.7 — never an unproven syntax. The 1.1.10 changelog
records that AGY's native sandbox mounts `.git` read-only; provider-native is therefore a
degraded mode for any Git-writing workflow and SRT remains the default boundary. Under
SRT the resolver's only contribution is the `--sandbox=false` pin, per the nesting rule in
3.1.

A resolver class nobody can reach is dead code: `get_sandbox_resolver` (the closed factory,
in `sandbox_resolvers.py` after 3.1) and the provider sandbox-capability gate must both learn the agy entry
in this task, or the provider-native path still raises for AGY with the class present.
That gate is concrete: `get_sandbox_resolver` refuses any provider for which
`provider_supports_sandbox` returns False, and that predicate reads the module-level
`PROVIDER_CAPABILITIES` table in `provider_capabilities.py`. The AGY row of that table
therefore lands **here**, not in 6.1 — otherwise acceptance 3.2.3 is unsatisfiable until a
downstream dependent completes. The row has three fields (`reasoning_flag`, `sandbox`,
`sensitive_path_enforcement`). `sandbox=True`; `sensitive_path_enforcement` is set only if
1.1.7 proves AGY's native sandbox denies the sensitive roots, else stays False
(fail-closed) — which means web chat admits agy only under `backend="srt"` via the
`create_session` gate 3.1 composes with; `reasoning_flag` follows 6.1's `--effort` record.
6.1 consumes the completed capability row; spawn stays
gated meanwhile by `SPAWN_CAPABLE_PROVIDERS` and the `execute_spawn` rejection it removes.

The resolver alone does not make an AGY launch viable under SRT. `sandbox_policy.py`
supplies each provider's network domains (`_PROVIDER_DOMAINS`), credential and state roots
(`_PROVIDER_AUTH_PATHS`, `_PROVIDER_AUTH_READ_ONLY_PATHS`), and masked credential env vars
(`_PROVIDER_CREDENTIAL_ENV`) — and it has **no agy entries**, so an AGY launch would pass
wrapper preflight yet run with no upstream network access and no access to
`~/.gemini/antigravity-cli` credentials, state, or transcripts. Add the agy entries using
exactly the domains and read/write roots recorded by 1.1's network/state probe (probe
record 1.1.9) and the credential env vars recorded by the authentication-footprint probe
(record 1.1.15) — never guessed values. The 5.2 launch-contract row proves them at the launch seam.

`sandbox.py` is 914 lines; 3.1 already extracted the resolvers into
`sandbox_resolvers.py`, so this deliverable adds nothing to `sandbox.py`. 3.2.4's inputs
are unchanged records 1.1.9 and 1.1.15. None of the new records 1.1.17–1.1.24 feeds this
deliverable; `HIDDEN_PROVIDERS` un-hiding is 6.2's.

**Acceptance:**

- 3.2.1 - `AgySandboxResolver` exists and returns AGY's `--sandbox` for provider-native. file: `src/gobby/agents/sandbox_resolvers.py`.
- 3.2.2 - `sandbox.py` gains no lines from this deliverable; the resolver lives in `sandbox_resolvers.py`. file: `src/gobby/agents/sandbox_resolvers.py`.
- 3.2.3 - `get_sandbox_resolver("agy")` returns `AgySandboxResolver`, and the provider sandbox-capability gate admits agy, using the live-proven flag form. file: `src/gobby/agents/sandbox_resolvers.py`.
- 3.2.4 - `sandbox_policy.py` gains agy entries for provider domains, credential/state read and write roots, and credential env masking, using only probe-recorded values — domains and roots from 1.1.9, credential env from 1.1.15. file: `src/gobby/agents/sandbox_policy.py`.
- 3.2.5 - The AGY `PROVIDER_CAPABILITIES` row lands in this deliverable, `provider_supports_sandbox("agy")` returns True, and reachability tests pin `get_sandbox_resolver("agy")` through the capability gate. test: `tests/agents/test_sandbox.py`.
- 3.2.6 - `AgySandboxResolver` emits `--sandbox` for provider-native and `--sandbox=false` when SRT enforces, exactly the boolean form recorded under 1.1.7, and the native-sandbox `.git` read-only caveat from the 1.1.10 changelog is documented on the resolver. file: `src/gobby/agents/sandbox_resolvers.py`.
- 3.2.7 - The AGY `PROVIDER_CAPABILITIES` row declares `sandbox=True` and sets `sensitive_path_enforcement` only on 1.1.7 proof (default False), so `WebChatRuntimeManager.create_session` admits agy under `backend="srt"` and rejects it under provider-native while the flag is False. test: `tests/agents/test_sandbox.py`.

## P4: AGY Hook and Transcript Layer
`kind: framing`

**Goal**: Make AGY sessions visible to Gobby — registered, parsed, and summarizable.

### 4.1 Correct the AGY hook contract to camelCase and synthesize SESSION_START [category: code] (depends: 2.2, 2.3, 3.1)
`kind: deliverable`

Targets:
- `src/gobby/adapters/agy.py::AgyAdapter`
- `src/gobby/adapters/agy_contract.py::*` — scope-reason: the module-level hook contract tables (not indexed symbols) gain camelCase payload-key metadata
- `src/gobby/adapters/capabilities.py::_agy_capabilities`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_session_start`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::handle_pre_created_session`
- `src/gobby/hooks/event_handlers/_session_start/context.py::mark_startup_context_injected`
- `src/gobby/servers/routes/mcp/hooks.py::_run_adapter_hook`
- `src/gobby/servers/routes/mcp/hooks.py::execute_hook`
- `src/gobby/storage/sessions/_terminal.py::_TerminalMixin.update_terminal_pickup_metadata`
- `src/gobby/workflows/state_manager.py::SessionVariableManager.claim_startup_context`
- `src/gobby/storage/session_models.py::*` — scope-reason: the Session model, row hydration, and serialization gain the startup-context claim generation together
- `crates/gcore/assets/schema/migrations/403_sessions_startup_claim_generation.sql`
- `crates/gcore/assets/schema/migrations/404_hook_receipt_effects.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: the 403 and 404 EmbeddedMigration entries register both migrations in MIGRATIONS
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerated catalog entries for the claim-generation column and the hook_receipt_effects table
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: latest_version, latest_checksum, and assets_root_hash follow the new latest asset
- `crates/gcore/src/grant/bundle.rs::*` — scope-reason: the pinned latest_version literal follows the new latest asset
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: the expected schema-identity latest_version follows the new latest asset
- `crates/gcore/tests/schema_contract.rs::embedded_assets_publish_a_complete_schema_identity`
- `crates/gdaemon/tests/cli_contract.rs::*` — scope-reason: the version-json schema-identity assertions follow the new latest asset
- `crates/gcore/src/schema/runner_tests.rs::migrations_directory_exists_and_copy_agent_entry_is_registered`
- `crates/gcore/tests/catalog_manifest_freshness.rs::catalog_manifest_is_fresh_for_embedded_assets`
- `tests/runtime_grants/golden/brokered_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/direct_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/old_client_new_grant.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `tests/runtime_grants/golden/unavailable_datastores.json::*` — scope-reason: signed golden grant vector embeds the schema identity
- `src/gobby/storage/hook_receipts.py`
- `tests/storage/test_hook_receipts.py`
- `src/gobby/hooks/runtime_compat.py::*` — scope-reason: the hook-response capability floor joins the existing schema-version and minimum-ghook-version runtime compatibility contract
- `src/gobby/workflows/hooks.py::*` — scope-reason: rule one-shot guards and sibling set_variable/mcp_call success variables move from eager persistence to the staged receipt commit
- `src/gobby/workflows/engine/effects.py::*` — scope-reason: response-visible effect application stages one-shot guards and sibling variables in the receipt effect record
- `src/gobby/workflows/engine/delivery_formatting.py::*` — scope-reason: staged-memory injected-ID finalization moves behind the acknowledgment commit
- `src/gobby/hooks/hook_manager.py::*` — scope-reason: the finalize_staged_memory_delivery call site adopts the receipt-staged commit boundary
- `tests/workflows/test_hooks.py::*` — scope-reason: rule-persistence cases migrate from eager guard writes to receipt-staged commit, release, and terminal boundaries
- `tests/workflows/test_delivery_pipeline.py::*` — scope-reason: staged-memory delivery cases migrate to acknowledgment-gated finalization
- `src/gobby/storage/sessions/_crud.py::*` — scope-reason: the transactional resolve-or-adopt helper composes pre-created-id adoption with tuple-keyed registration
- `src/gobby/hooks/event_handlers/_session_start/context.py::classify_session_start_context`
- `src/gobby/hooks/event_enrichment.py::*` — scope-reason: pending-message delivery marking moves from enrich-time to the staged post-acknowledgment commit
- `crates/ghook/src/dispatch.rs::*` — scope-reason: retryable-retention/action decoupling and the post-emission delivery receipt land across the dispatch flow
- `crates/ghook/src/transport.rs::*` — scope-reason: the delivery-receipt ack envelope joins the inbox transport contract
- `crates/ghook/src/envelope.rs::*` — scope-reason: the versioned delivery-receipt wire type lands beside the hook envelope encoding, and every hook envelope gains the immutable producer response-capability field beside schema_version
- `crates/ghook/schemas/delivery-receipt.v1.schema.json`
- `schemas/delivery-receipt.v1.schema.json`
- `crates/ghook/src/action.rs::skip_stdout_json`
- `src/gobby/adapters/capabilities.py::ContextChannel`
- `tests/adapters/test_capabilities.py::test_agy_hook_capabilities_have_no_live_transport_claims`
- `tests/adapters/test_agy_contract.py::*` — scope-reason: the alias table and decode helper join the contract-data tests
- `crates/ghook/src/output.rs::*` — scope-reason: emission-plus-flush returns an I/O result that gates acknowledgment enqueue
- `crates/ghook/src/runtime.rs::write_runtime_stamp`
- `tests/hooks/test_runtime_compat.py::*` — scope-reason: capability-floor stamp, below-floor rejection, and both version-skew cases join the runtime-compatibility suite
- `src/gobby/hooks/inbox.py::*` — scope-reason: the drain path routes delivery receipts to a dedicated idempotent consumer instead of hook execution
- `src/gobby/hooks/event_handlers/_agent.py::*` — scope-reason: handle_stop records the terminal-undelivered receipt disposition and the first-turn agent-preamble guard moves behind the staged receipt commit
- `tests/hooks/test_agent_events_coverage.py::*` — scope-reason: the first-prompt, persona-switch, stale-repair, and rehydration cases assert eager _agent_context_injected writes and re-anchor to the staged guard boundaries
- `src/gobby/sessions/liveness_monitor.py::*` — scope-reason: session-expiry recording of the terminal-undelivered receipt disposition
- `src/gobby/sessions/lifecycle.py::*` — scope-reason: lifecycle expiry records the terminal-undelivered receipt disposition
- `tests/hooks/test_event_enrichment.py::*` — scope-reason: direct enrichment cases migrate from eager delivery marking to the staged receipt commit
- `crates/ghook/tests/contract.rs::*` — scope-reason: critical vs noncritical retryable-timeout disposition and delivery-receipt cases join the daemon-down contract suite, ordered after 2.3's wholesale policy update
- `src/gobby/hooks/envelope_dedupe.py::*` — scope-reason: the envelope processing marker becomes a verifiable ownership lease with worker-exit finalization so a timed-out delivery is retained for replay rather than terminally processed
- `tests/storage/test_schema_contract.py::*` — scope-reason: identity-pin and no-Python-DDL coverage for the two migrations
- `tests/hooks/conftest.py::*` — scope-reason: shared hook fixtures encoding the eager context_injected boolean migrate to the claim-generation contract
- `tests/hooks/test_handler_execution.py::*` — scope-reason: handler-execution assertions of the eager marker migrate to claim/commit/rollback boundaries
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: session-start handler assertions of context_injected transitions migrate to the claim-generation boundaries
- `tests/hooks/event_handlers/test_session_variable_preservation.py::*` — scope-reason: the direct handler case asserting eager context_injected inside handle_session_start re-anchors to the two-phase claim boundaries
- `tests/storage/sessions/test_metadata.py::*` — scope-reason: the context_injected persistence cases gain the claim-generation column round-trip
- `tests/storage/sessions/test_storage_sessions_models.py::*` — scope-reason: Session model serialization assertions gain the claim-generation field
- `tests/storage/test_sessions_import.py::*` — scope-reason: the pinned update_terminal_pickup_metadata signature follows the claim-generation contract
- `tests/servers/test_mcp_routes.py::*` — scope-reason: envelope-lease route cases force a worker past the replay grace period and pin reclaim and losing-owner finalization
- `tests/workflows/test_session_variable_manager.py::*` — scope-reason: the boolean startup-claim assertions migrate to generation claim, commit, compare-and-rollback, and invalidate cases
- `tests/hooks/test_inbox.py::*` — scope-reason: inbox replay gains the retained-envelope adapter-timeout redelivery case, receipt-acknowledgment drain routing, and enqueue-only below-floor terminal quarantine at drain — repeated-drain, restart, quarantine-retention, and zero-effect cases
- `src/gobby/hooks/rule_evaluator.py::*` — scope-reason: the memory and skill discovery-dedupe claims (injected_memory_ids, suggested_skill_names) move from eager evaluation-time claims to the receipt-staged commit
- `src/gobby/workflows/engine/injection_tracking.py::InjectionTrackingMixin._filter_and_track_new_review_lessons`
- `tests/hooks/test_hook_extracted_helpers.py::*` — scope-reason: the evaluator-result merging cases adjacent to the staged dedupe seam stay green through the boundary move
- `tests/hooks/test_hook_manager_extra.py::*` — scope-reason: TestDedupMemoryResults and TestDedupSkillResults directly assert the eager claim_set_variable_values dedupe contract and migrate to receipt-staged boundaries
- `src/gobby/workflows/definitions.py::RuleEffect`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/guard-plan-memory-writes.yaml::*` — scope-reason: the acknowledge_variable guard declares the on_receipt delivery disposition
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/memory-capture-nudge.yaml::*` — scope-reason: the inject_context plus one-shot set_variable guards declare the on_receipt disposition
- `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml::*` — scope-reason: both rules' one-shot guards declare the on_receipt disposition
- `src/gobby/install/shared/workflows/rules/skill-discovery/discover-skill-hubs-on-turn-start.yaml::*` — scope-reason: the inject_result plus success_variable mcp_call declares the on_receipt disposition
- `src/gobby/workflows/sync_rules.py::*` — scope-reason: the sync path gains the typed disposition data-migration/validation owner for user- and project-owned one-shot rule definitions that template refresh deliberately never touches
- `src/gobby/mcp_proxy/tools/workflows/_rules.py::*` — scope-reason: create_rule and update_rule persist rule definitions post-activation and adopt the shared write-time disposition classifier
- `src/gobby/servers/routes/rules.py::*` — scope-reason: the HTTP create and full-replacement update endpoints adopt the shared write-time disposition classifier
- `src/gobby/cli/rules.py::*` — scope-reason: the rule-file import command adopts the shared write-time disposition classifier
- `src/gobby/workflows/imports.py::sync_imported_definition`
- `tests/mcp_proxy/tools/test_rule_tools.py::*` — scope-reason: post-activation MCP create and update cases prove no eager one-shot guard persists
- `tests/servers/routes/test_rules_routes.py::*` — scope-reason: post-activation HTTP create and full-replacement cases prove no eager one-shot guard persists
- `tests/cli/test_cli_rules.py::*` — scope-reason: post-activation rule-file import cases prove no eager one-shot guard persists
- `tests/workflows/test_imports.py::*` — scope-reason: post-activation generic import-sync cases prove no eager one-shot guard persists
- `tests/workflows/test_rule_models.py::*` — scope-reason: RuleEffect delivery-disposition serialization round-trips and legacy-row deserialization join the direct model suite
- `src/gobby/runner_lifecycle_periodic.py::*` — scope-reason: the receipt-retention pruning loop registers beside the existing periodic maintenance tasks
- `tests/servers/routes/test_hooks_agy_dispatch.py::*` — scope-reason: claim-token commit, rollback, and late-timeout cases join the AGY hook-dispatch route suite
- `tests/adapters/test_adapters_agy.py::*` — scope-reason: existing AGY adapter tests gain two-phase dispatch, alias, injectSteps, terminationBehavior, and repeated-invocation cases
- `tests/hooks/test_pending_message_provider_contracts.py::*` — scope-reason: AGY pending-message delivery cases join the provider contract suite
- `tests/workflows/test_memory_lifecycle_rules.py::*` — scope-reason: the direct suite for guard-plan-memory-writes and memory-capture-nudge asserts each edited rule's eager/on_receipt payload grouping
- `tests/workflows/test_plan_mode_rules.py::*` — scope-reason: the direct suite for both handle-plan-mode-entry rules asserts their on_receipt grouping
- `tests/workflows/test_skill_discovery_rules.py::*` — scope-reason: the direct suite for discover-skill-hubs-on-turn-start asserts its on_receipt grouping
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: the SHA-256 entries for the four edited rule templates are regenerated
- `crates/ghook/schemas/inbox-envelope.v1.schema.json::*` — scope-reason: the optional producer response-capability property is admitted in the v1 schema
- `schemas/inbox-envelope.v1.schema.json::*` — scope-reason: the root public mirror admits the same optional property and stays byte-identical to the crate copy
- `src/gobby/runner_init/storage.py::*` — scope-reason: the unconditional narrow rule-disposition migration call is the ordered production trigger completing before receipt-capability activation, and the two retention-loop task attributes initialize here
- `src/gobby/cli/installers/shared.py::sync_bundled_content_to_db`
- `src/gobby/sync_registry.py::sync_bundled_content_to_db`
- `src/gobby/cli/sync.py::*` — scope-reason: the --reinstall path propagates the disposition-migration diagnostic as a typed Click failure
- `src/gobby/mcp_proxy/tools/workflows/_import.py::*` — scope-reason: the MCP reload caller propagates the disposition-migration diagnostic instead of swallowing it
- `tests/workflows/test_rule_yaml_sync.py::*` — scope-reason: first-run, repeated-run zero-writes, ambiguous-rollback, partial-failure, and concurrent-edit disposition-migration cases join the direct sync suite
- `tests/test_runner_lifecycle.py::*` — scope-reason: the daemon-startup seam gains cases proving the non-dev disposition migration runs exactly once before hook service, aborts on an ambiguous or partial diagnostic, and proceeds on a clean zero-write repeat
- `tests/test_runner_maintenance_startup.py::*` — scope-reason: the injected-loop harness proves the receipt-retention and quarantine pruning owners register and schedule exactly once in start_periodic_tasks
- `src/gobby/runner.py::GobbyRunner`
- `src/gobby/runner_lifecycle_shutdown.py::_cancel_periodic_tasks`
- `tests/test_runner_shutdown.py::*` — scope-reason: shutdown cases prove both retention-loop tasks join failure tracking and are cancelled and awaited exactly once before hook storage and database teardown
- `tests/cli/test_sync_reinstall.py::*` — scope-reason: typed-failure and intact-row cases for ambiguous/partial disposition results
- `tests/mcp_proxy/tools/workflows/test_import_reload.py::*` — scope-reason: stale-cache activation suppressed on ambiguous or partial rule-sync results
- `crates/ghook/src/diagnostics.rs::*` — scope-reason: the exhaustive Envelope fixture literal is a compile-time consumer of the new response-capability field
- `tests/servers/routes/mcp/test_hook_session_metadata.py::*` — scope-reason: raw success-path envelope constructors gain the supported response-capability value
- `tests/servers/routes/test_hooks_droid_dispatch.py::*` — scope-reason: raw success-path envelope constructors gain the supported response-capability value
- `tests/e2e/conftest.py::*` — scope-reason: CLIEventSimulator._hook_envelope gains the supported response-capability value for E2E success paths
- `tests/e2e/test_daemon_auth.py::*` — scope-reason: the raw envelope constructor gains the supported response-capability value
- `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py::*` — scope-reason: the raw envelope constructor gains the supported response-capability value
- `tests/servers/routes/test_hold_open_gate.py::*` — scope-reason: the raw envelope constructor gains the supported response-capability value
- `tests/servers/test_http_endpoints.py::*` — scope-reason: the raw envelope constructor gains the supported response-capability value
- `tests/servers/test_http_server.py::*` — scope-reason: the raw envelope constructor gains the supported response-capability value

Two defects. First, AGY sends **camelCase protojson** payloads — `conversationId`,
`transcriptPath`, `workspacePaths`, `stepIdx`, `toolCall` — but the adapter
(`ACPHookAdapter.translate_to_hook_event`) dual-reads only `sessionId`, `hookEventName`,
and `toolName` — keys AGY never sends — and `cwd`. AGY's `conversationId`,
`transcriptPath`, `workspacePaths`, `artifactDirectoryPath`, `modelName`,
`toolCall{name,args}`, `stepIdx`, `invocationNum`/`initialNumSteps`,
`executionNum`/`terminationReason`/`fullyIdle` are read nowhere in Python; the only
consumer in `crates/` is `dispatch.rs::project_root_from_workspace_paths` (#20624), and
the committed fixture uses snake_case pointing at a stale `.pb` path.

Aliasing is AGY-local, following the Grok precedent
(`adapters/grok.py::GrokAdapter._normalize_event_data`): `agy_contract.py` declares
`AGY_PAYLOAD_ALIASES` (`conversationId→session_id`, `transcriptPath→transcript_path`,
`workspacePaths→workspace_paths`, `artifactDirectoryPath→artifact_directory_path`,
`modelName→model`, `stepIdx→step_idx`, `invocationNum→invocation_num`,
`initialNumSteps→initial_num_steps`, `executionNum→execution_num`,
`terminationReason→termination_reason`, `fullyIdle→fully_idle`) plus the nested flatten
`toolCall.name→tool_name`, `toolCall.args→tool_input`, and `workspace_paths[0]→cwd` when
`cwd` is absent. `AgyAdapter.translate_to_hook_event` applies the table **before**
calling `super()`, because `ACPHookAdapter` extracts `session_id` and `cwd` from the raw
payload ahead of `_normalize_event_data`; `hooks/_normalization_tools.py::normalize_tool_fields`
and `ACPHookAdapter` gain no AGY keys. Record 1.1.5 shows hook `toolCall.args` arrive
as a native JSON object (`{"CommandLine": "ls -la", "Cwd": …}`, `{"ServerName":
"gobby", "ToolName": …, "Arguments": {…}}`), so the adapter does not decode strings;
`agy_contract.py::decode_agy_tool_args` exists for 4.2's parser, whose input
`transcript_full.jsonl` also carries native args (record 1.1.22) — the helper keeps
the raw-fallback branch only for the JSON-string form of `transcript.jsonl`, which is
never the parser input. The AGY `TOOL_MAP` keys are the 1.1.5-recorded snake_case names
(`list_dir`, `run_command`, `view_file`, `find_by_name`, `call_mcp_tool`, …), the same
vocabulary as the 1.1.6 stream `tool_name`.

Second, AGY has **no `SessionStart` hook**, so `flow.py` (inside `handle_session_start`) —
which reads `transcript_path` only during session start — never runs. This, not the
casing, is why AGY sessions have no transcript. Follow the Codex precedent for the event
shape: `codex_impl/app_server_adapter.py:110`
maps `thread/started` to `SESSION_START` and `:588-596` constructs the event with
`data["transcript_path"]` and `cwd` populated. Synthesize that event from `PreInvocation`,
mapping `conversationId` to session id, `transcriptPath` into `data["transcript_path"]`,
and `workspacePaths[0]` to `cwd` — but **never by remapping**. `PreInvocation` is AGY's
`BEFORE_AGENT` carrier: per-turn rules and `inject_context` dispatch on it, and a remap
that turns it into `SESSION_START` would suppress that path on every turn. Handling is
**two-phase**, and the executable seam is an `AgyAdapter.handle_native` override:
`translate_to_hook_event` returns one `HookEvent` and `BaseAdapter.handle_native`
(`adapters/base.py:193`) invokes `HookManager.handle` exactly once, so translation alone
cannot dispatch two events. The override processes the idempotent synthetic
`SESSION_START` side effect first, then processes the original `PreInvocation` as
`BEFORE_AGENT` exactly once, and returns *that* original event's translated response —
including `injectSteps` — back to AGY. The synthetic phase's response is **merged, never
discarded**: `handle_session_start` composes startup context and a system message and
marks the session `context_injected=True`, so dropping the synthetic `SESSION_START`
response would lose the startup context permanently — later events correctly see it as
already delivered and suppress it. The override merges the synthetic response's `context`
and `system_message` into the original `BEFORE_AGENT` response, preserves the original
event's decision, translates once to `injectSteps`, and marks startup context injected
only after successful emission. That ordering is a commit-boundary change, not
adapter-local behavior: today both session-start flows — `handle_session_start` and
`handle_pre_created_session` (`flow.py`) — call `mark_startup_context_injected`
(`context.py`) eagerly, before the adapter ever translates the merged response, so a
translation or envelope failure would strand the context as delivered-but-lost. The
claim state has two concrete owners today, and neither can express the token:
`SessionVariableManager.claim_startup_context` (`workflows/state_manager.py:667`) is the
atomic owner, compare-and-setting the boolean `_startup_context_injected` variable, and
the session row's `context_injected boolean` (`crates/gcore/assets/schema/baseline.sql`;
`session_models.py`; persisted via `_TerminalMixin.update_terminal_pickup_metadata`,
`storage/sessions/_terminal.py`) is a second irreversible marker — a bare boolean cannot
identify the claimant for commit, rollback, or invalidation. The marker becomes a durable
claim **generation**: a token-bearing record at the atomic owner with claim, commit,
compare-and-rollback, and invalidate operations, carried by a new numbered sessions
migration (`crates/gcore/assets/schema/migrations/403_sessions_startup_claim_generation.sql`,
through the embedded-asset contract in the Constraints; the sealed baseline is untouched)
plus `Session` model/row hydration and serialization. The generation is allocated **before executor
submission**, and that allocation must have an identity to claim against:
`claim_startup_context` takes a canonical session id and session variables are
session-backed, while on the first AGY `PreInvocation` the canonical session row is
created only inside the executor's synthetic `SESSION_START` phase. The pre-submission
step is therefore **resolve-or-adopt-or-register**, and it must honor every existing
session-creation path's identity rules: provider plus `conversationId` alone is not the
persisted uniqueness key — registration is keyed by the four-column `idx_sessions_unique`
tuple `(external_id, source, project_id, session_type) NULLS NOT DISTINCT` **plus**
machine ownership, enforced by `sessions_id_machine_id_key UNIQUE (id, machine_id)` and
the registration path's owner-machine check (`storage/sessions/_crud.py`,
`require_local_machine_id`) — together the five-part identity this plan means wherever it
says so — and a spawned pre-created
child initially carries its Gobby session id as `external_id` (`agents/session.py`),
rebound to the CLI-native id during session start — so a minimal provider-plus-id
lookup can miss the pre-created row, register a duplicate, and strand parent/child
linkage on an orphan. One transactional resolve-or-adopt helper owns the step: it
first honors the internal pre-created Gobby session id carried by terminal/session
context — the ghook-recognized `GOBBY_SESSION_ID`/`GOBBY_PROJECT_ID` environment
(`dispatch.rs` reads it into the envelope's session context) exported by spawn-time
terminals and, per 5.2, by AGY web-chat subprocesses alike. The hint is a hint,
never an authority: before it binds anything, the helper atomically validates the
selected row's **full identity** against the envelope — `project_id` against the
envelope's project context, `source` against the adapter's provider, `machine_id`
against the local machine identity, `session_type` against the expected pre-created
type, and the persisted workspace identity from 3.1 against tombstone and
staleness state — and only a fully matching row is adopted. A mismatching hint is
rejected with a truthful diagnostic and the helper proceeds through the ordinary
resolution below; it never adopts, rebinds, or mutates the mismatched row, and no
claim allocation, `conversationId` binding, transcript classification, or metadata
write happens before validation passes. The pre-created row's persisted workspace
is preserved unless an explicit 3.1 switch writer changes it. The helper
then resolves the full persisted uniqueness tuple for ordinary sessions,
registers the minimal canonical row only when both miss, binds `conversationId` onto
the adopted row, and returns that row's canonical id for the generation claim —
adoption preserves the pre-created row's `session_type`, and concurrent
terminal-versus-web_chat collisions on shared native identity are tested, alongside
wrong-project, wrong-source, wrong-machine, wrong-worktree, tombstoned-workspace,
pending-transcript, and concurrent-switch hint-mismatch cases.
Because `execute_hook` is async and this preflight is synchronous database work, the
preflight runs as **one bounded, shielded future** — resolve/adopt/register plus
generation claim — awaited before adapter submission with explicit queue and
execution bounds, never inline on the event loop; when the request exits before the
preflight completes, an attached compare-and-invalidate cleanup disposes the late
outcome — the idempotent minimal registration may stand, but no live claim remains
ownerless. The executor's synthetic phase performs the remaining session-start side
effects (startup-context composition, classifier-routed transcript selection,
metadata) on the already-resolved session, staying idempotent for repeated and
concurrent invocations, for pre-created and ordinary sessions alike. Downstream
classification adopts rather than re-claims: `classify_session_start_context`
(`_session_start/context.py`) currently calls `claim_startup_context` itself for both
session-start flows, so the owning worker's own synthetic phase would observe the
pre-claimed generation as already live and suppress the full startup context the
claim exists to deliver. `execute_hook` passes the expected generation and an owner
token through the synthetic `SESSION_START` event's internal metadata; the classifier
and its atomic helper adopt a matching owner's pre-claim and return the full-context
classification, while non-owners attempt or observe the claim and classify live —
ordinary, pre-created, repeated, concurrent, invalidated, and late-worker cases are
each tested. A claim made inside the executor work item would leave a
queue-timeout-before-claim race where a late worker creates a fresh claim after the
caller has already returned failure; with the bounded pre-submission preflight, the
queue-timeout path always holds an invalidatable generation with a resolvable
identity. The token and canonical session id travel privately
through the `HookResponse` to `execute_hook`; `AgyAdapter.translate_from_hook_response`
emits only `decision`, `reason`, `overwrite` (AGY's arg-rewrite key is `overwrite`, not
`updatedInput`), and `injectSteps`, so
private claim fields are never emitted to AGY. The commit boundary lives where the
fallible steps actually run: envelope persistence happens in `execute_hook`
(`servers/routes/mcp/hooks.py`), whose nested `mark_processed_and_return` swallows a
persistence failure with a warning — translation success never proves envelope storage;
`_run_adapter_hook` runs the adapter on an executor thread that `asyncio.wait_for`
cannot cancel, so a timed-out worker can finish late after the caller already returned
failure. The token is therefore claimed pre-submission, committed only after
`execute_hook` confirms envelope persistence, compare-and-rolled-back on every earlier
failure, and invalidated on timeout so a late-finishing executor worker holding a stale
token can neither commit nor strand delivery.

Timeout is where two delivery promises must be kept apart. The envelope state machine
currently terminalizes the only invocation: `execute_hook`'s `TimeoutError` branch
returns a graceful response through `mark_processed_and_return`, which stamps the
idempotency marker via `mark_envelope_processed` (`hooks/envelope_dedupe.py`), and ghook
deletes the inbox envelope on any 2xx (`crates/ghook/src/transport.rs` writes
`~/.gobby/hooks/inbox/` before the POST and removes it on success), so the daemon-side
replay (`hooks/inbox.py`) never re-delivers that `PreInvocation` — the first AGY turn
proceeds without startup context and with no retained retry work. Adapter timeout
becomes a **retryable envelope outcome** — but replay is daemon-side event recovery,
never provider response delivery: ghook has already failed open and returned `continue`
to AGY, the originating hook process is gone, and the inbox replay re-POSTs the
envelope consuming only the HTTP status — any response body is discarded. Replay
therefore re-delivers the envelope so daemon-side effects (idempotent session
registration, rule dispatch) complete, and terminally processes it on success — but it
never commits the startup-context generation. The generation stays uncommitted after a
timeout, and startup context is delivered on the **next live `PreInvocation`**: the
synthetic phase finds the claim unowned, re-merges the startup context into that turn's
`BEFORE_AGENT` response, and the commit fires only when a response reaches the live
hook process that consumes it. Overlap between replay and the original worker is
fenced at worker exit, not at timeout: `_run_adapter_hook`'s executor thread is
uncancellable and can still register the session, pulse activity, run rules, and
consume pending messages after the caller returns, so the timeout path returns the
retryable non-2xx immediately but defers the envelope-claim disposition to a
finalization attached to the shielded executor future — a worker that ran to
completion terminally processes the envelope with no replay, a worker that failed or
aborted releases the claim so replay proceeds, and replay attempts before finalization
observe the envelope still claimed and back off with bounded redelivery. The marker
carrying that exclusion cannot be fixed-age: `clear_stale_envelope_processing_marker`
(`hooks/envelope_dedupe.py`) today clears any processing marker older than the replay
grace period, which would let replay claim the envelope while a slow live worker is
still mutating session state. The marker becomes a **verifiable lease** — an owner
token renewed while the shielded future is live, reclaimable only after lease expiry
plus failed owner-liveness validation (distinguishing a slow live worker from a dead
daemon instance), finalized by compare-and-set on the same token so a losing owner
cannot finalize. Late worker output for an invalidated generation is discarded in
every case.

Retention and provider-visible action are decoupled outcomes. ghook's dispatch
currently maps retry backpressure (503 plus `{"status":"retry"}`) to `continue`
**before** the criticality check (`crates/ghook/src/dispatch.rs`, `run_gobby_owned`) —
documented as applying even on critical hooks — so routing adapter timeout through
that branch unchanged would fail open on critical lifecycle hooks that 2.3's matrix
requires to block. The two retry classes therefore stay distinguishable on the wire:
the retryable response carries a stable `retry_kind` discriminator alongside the
provider-visible action computed by 2.3's criticality policy. `ingress_backpressure`
— today's `is_retry_backpressure` shape (`transport.rs:65`) — keeps ghook's
unconditional `continue` with retention exactly as documented, preserving the
live-lock rationale for a daemon that keeps asking for retry; `adapter_timeout`
honors the computed action: the envelope is retained for replay in both classes,
critical lifecycle hooks emit the existing fail-closed blocking result, and
noncritical hooks — every AGY event included — emit `continue` — for agy the emitted
`continue` body is the protojson-legal per-event skip form from
`action.rs::skip_stdout_json` (#20624) — never `{"continue":true}` and never the
`{"status":"error","message":…}` body `action_from_failure` still emits for noncritical
failures; 2.3.7 owns replacing that body, this deliverable pins the `adapter_timeout`
path against it — keeping retention and replay delivery-side while the provider-visible
action stays exactly 2.3's policy.

Commit requires proof of delivery, and neither worker completion nor server
persistence is that proof. The persisted response carries an opaque **delivery
receipt**; ghook acknowledges it only after successfully parsing the response and
emitting the action to the host CLI. The acknowledgment is a first-class wire type,
not a reused hook envelope: a versioned delivery-receipt schema
(`crates/ghook/schemas/delivery-receipt.v1.schema.json`, encoded beside the hook
envelope in `envelope.rs`) carrying `receipt_id`, the original envelope id, the
canonical session identity, and the **current-attempt identity** — a monotonic
delivery generation stamped on the receipt row at every prepare and re-prepare,
carried in the prepared response, and echoed back by the acknowledging ghook.
An acknowledgment commits only the exact delivery attempt whose output
generated it: because a released receipt re-prepares onto the next durable
envelope under the same `receipt_id`, a delayed acknowledgment from the earlier
carrying envelope would otherwise be indistinguishable from the current attempt
and could commit the re-prepared row before its current envelope is ever
emitted — so the consumer CASes on `receipt_id`, delivery generation, and
`prepared` together, and a stale-generation acknowledgment is a recorded
terminal no-op. The daemon's inbox drain (`hooks/inbox.py`) routes it to
a dedicated idempotent compare-and-set consumer — never through the ordinary
hook-execution adapters — duplicate direct and replayed acknowledgments are no-ops,
acknowledgments generate no receipts of their own, and a consumed acknowledgment
terminalizes the original envelope without re-executing its hook. The receipt
states have a concrete storage authority, not just behavioral prose: a
relational receipt-effects table — receipt id as primary key, original envelope
id, canonical session identity, delivery generation, state, staged payload, and transition
timestamps — lands as migration
`crates/gcore/assets/schema/migrations/404_hook_receipt_effects.sql` (through the
embedded-asset contract; the sealed baseline is untouched) with a dedicated storage
module (`storage/hook_receipts.py` — DML only; the table DDL lives exclusively in the
migration) owning the compare-and-set transition guards,
restart recovery, and cleanup APIs; the inbox acknowledgment consumer, the
`Stop` handler, both expiry owners, transport release, and duplicate-ack
no-ops all mutate state through that one authority. The table's growth is
bounded by an explicit per-state retention lifecycle, not a named-but-ownerless
cleanup API — and the lifecycle is numerically implementable, not adjectivally
"explicit": the recovery/idempotency window is a named constant,
`HOOK_RECEIPT_IDEMPOTENCY_WINDOW`, with a fixed default declared in
`storage/hook_receipts.py` (configuration may override it; the default is the
contract), measured on wall-clock time against the row's recorded transition
timestamp — the cutoff is `transition_timestamp + window`, and a row becomes
prune-eligible strictly **after** the cutoff (a row exactly at the boundary is
retained). `prepared` rows are never pruned. `released` rows have both exits
defined, not merely named: when the released payload moves to the next durable
envelope, the **same receipt row** atomically re-prepares — a compare-and-set
from `released` to `prepared` that records envelope lineage (the original
envelope id is kept and the current envelope id updated) so a receipt is one
delivery obligation across however many envelopes carry it; and the terminal
owners (`Stop`, both expiry owners) compare-and-set from `released` to
`terminal-undelivered` exactly as they do from `prepared`, so a released row
always has a reachable terminal transition and can never be permanently
non-prunable. `acknowledged` and `terminal-undelivered` rows become
prune-eligible only after the window, preserving duplicate-ack no-op detection
for late replayed acknowledgments inside it; a duplicate acknowledgment
arriving after its row was pruned is a **terminal idempotent no-op in the
dedicated receipt consumer itself** — an acknowledgment whose `receipt_id`
resolves to no row executes no adapter or effect and writes no new record, so
post-prune idempotency terminates at the bounded receipt authority and depends
on no second durable record (the processed-envelope markers in
`envelope_dedupe.py` are durable and unpruned; leaning on them as a
terminalization ledger would merely relocate unbounded growth). Pruning runs
in bounded, indexed batches from a periodic receipt-retention loop registered
in `start_periodic_tasks` (`runner_lifecycle_periodic.py`), following the
`workflow_audit_cleanup_loop` precedent (`runner_maintenance_audit.py`), with
prune-versus-transition races excluded by the same compare-and-set guards. Emission itself
becomes observable: ghook's output helper currently discards write errors
(`output.rs`), so emission-plus-flush returns an I/O result, receipt metadata is
stripped before provider-specific action mapping, and the acknowledgment is durably
enqueued only after a successful full write. The handoff is atomic across every
branch: the original inbox envelope is retained until the receipt
acknowledgment is durably enqueued (or atomically replaced by it), so an
ack-write failure after successful emission leaves a replayable original
rather than a stranded execution; ghook's enqueue-failure fallback (the
`direct_post_after_enqueue_failure` branch inside
`dispatch.rs::run_gobby_owned`) POSTs with no durable envelope identity, and
that identity-less path is an explicit **at-least-once presentation mode**,
not an undefined one: the response still carries the one-shot payload —
startup context is never withheld from a fallback-only session — but no
receipt is created and no staged effect or dedupe claim commits, so repeated
presentation across consecutive enqueue-failure turns is the accepted,
tested mode; the effects stay uncommitted until a later durable hook creates
and commits a receipt, and a session that ends on the fallback path — `Stop`
or expiry with no durable successor — records terminal-undelivered exactly
as a transport loss does; and terminal
owners compare-and-set only from `prepared` or `released` — never from
`acknowledged` — so an acknowledgment durably
enqueued before a `Stop` or expiry wins the CAS and a late acknowledgment
arriving after terminal-undelivered is a recorded no-op. The residual crash window is stated
honestly: a ghook death after a successful write but before the acknowledgment
persists means provider-visible presentation is **at-least-once** — the generation
commit and every staged effect are compare-and-set exactly-once, and
re-presentation of already-visible startup context or pending messages is the
accepted, tested duplicate mode. Every one-shot response effect — the
startup-generation commit, pending-message delivery marking, one-shot injected
context, rule one-shot guards with their sibling `set_variable`/`mcp_call`
success variables (persisted eagerly today by `WorkflowHookHandler` and
`EffectsMixin`, `workflows/hooks.py` and `workflows/engine/effects.py`),
staged-memory injected-ID finalization (`finalize_staged_memory_delivery`,
`workflows/engine/delivery_formatting.py`, called from
`hooks/hook_manager.py`), the discovery-dedupe claims —
`WorkflowRuleEvaluator.dedup_memory_results` and `.dedup_skill_results`
(`hooks/rule_evaluator.py`) claim `injected_memory_ids` and
`suggested_skill_names` at evaluation time, and
`InjectionTrackingMixin._filter_and_track_new_review_lessons`
(`workflows/engine/injection_tracking.py`) appends
`injected_review_lesson_ids` at filter time, all before any transport — and
the first-turn agent-preamble guard
(`event_handlers/_agent.py`) — is staged behind that acknowledgment through durable prepared,
acknowledged, released, and terminal-undelivered states keyed by receipt, original
envelope, and canonical session: `EventEnricher._inject_pending_messages`
(`hooks/event_enrichment.py`) currently marks messages delivered at enrich time,
before any transport, and that marking moves to the staged commit — for the five
incumbent providers exactly as for AGY. Context that is deliberately re-sent
every turn — carrying no one-shot guard — keeps its eager path; only
guard-suppressed payloads stage, so a write failure before emission can never
suppress a future delivery the provider never received. On transport loss the staged effects
release for the next live hook; the terminal owners are concrete — the `Stop`
handler (`event_handlers/_agent.py::handle_stop`) and both session-expiry owners
(`sessions/liveness_monitor.py`, `sessions/lifecycle.py`) record the
terminal-undelivered disposition and retire the claim — so a single-turn session
that times out can never strand a semantically unresolved generation or silently
lose pending messages.

The eager-versus-staged split is declared by the producer, never inferred.
`RuleEffect` (`workflows/definitions.py`) today carries effect types, per-effect
`when` conditions, and guard fields but no delivery semantics, and the
effect-application seams expose independent effects plus a whole-variable diff —
so staged-commit code guessing one-shot status from a sibling mutation or
condition shape would misclassify both directions: delaying ordinary state
(a brevity-drift counter must survive a transport loss) or eagerly committing
a delivery guard. `RuleEffect` therefore gains an explicit delivery
disposition — `eager` by default, preserving ordinary state and deliberately
per-turn context unchanged, `on_receipt` declared on mutations that exist to
suppress future delivery of a response payload — and within one fired rule the
`on_receipt` effects and the payload they suppress commit as a single receipt
group, so the grouping needs no invented identifier. The bundled sweep is an
enumeration, not a representative: every bundled template whose effects pair a
response payload with a delivery-suppressing mutation declares `on_receipt` —
the `acknowledge_variable` guard in
`memory-lifecycle/guard-plan-memory-writes.yaml`, the `inject_context` +
one-shot `set_variable` guards in `memory-lifecycle/memory-capture-nudge.yaml`
and `plan-mode/handle-plan-mode-entry.yaml` (both rules in that file, the
qwen-gcode hint included), and the `inject_result` + `success_variable`
`mcp_call` in `skill-discovery/discover-skill-hubs-on-turn-start.yaml` — with
`tests/workflows/test_rule_models.py` pinning the field's serialization
round-trip and legacy-row deserialization, and the direct behavioral suites
that load and assert those exact rules —
`tests/workflows/test_memory_lifecycle_rules.py`,
`test_plan_mode_rules.py` (both handle-plan-mode-entry rules), and
`test_skill_discovery_rules.py` — extended to assert each edited rule's
eager/`on_receipt` payload grouping; serialization coverage alone proves
nothing about the rules' behavior. The edited templates are bundled content
with committed hashes: each has a SHA-256 entry in
`src/gobby/install/bundled_content_manifest.json` and
`test_committed_bundled_content_manifest_matches_shared_tree`
(`tests/test_build_backend.py`) enforces exact parity, so the manifest is
regenerated in this same deliverable and that parity test joins the V2
validation runs. Propagation is ownership-aware,
because template sync deliberately does not reach every row:
`_is_sync_managed_rule` (`workflows/sync_rules.py`) manages only
`source == "installed"` global rows, so Gobby-owned bundled rows refresh on
definition drift while user-owned and project-owned definitions are preserved
verbatim — and a preserved row without the new field deserializes to the
`eager` default, silently re-opening the commit-before-delivery hole for
exactly the guards this contract stages. The sync path therefore gains a typed
data-migration/validation owner for those preserved rows: a user- or
project-owned definition whose effects match the one-shot criterion gets an
explicit disposition written in place — ownership, enabled toggle, and every
other field preserved — and a row the classifier cannot decide fails
validation with an actionable diagnostic naming the rule and the ambiguous
effect, never a silent eager default. The migration has one deterministic
production trigger, not a caller lottery: it runs inside `sync_bundled_rules`
so every invoker executes it idempotently, and the ordered startup owner is
deliberately **narrow** — `sync_bundled_content_to_db` stays exactly where
its docstring pins it (install, explicit CLI sync, and the dev-mode startup
branch): the aggregator syncs eight bundled domains and, in non-dev
production, imports user templates from `Path.cwd()`, while
`init_storage_and_config` already syncs build profiles unconditionally at
startup — so widening the whole aggregator across the dev-mode gate would
double-sync build profiles and drag a cwd-sensitive filesystem import into
every daemon start for a migration that needs neither. Instead
`init_storage_and_config` gains an unconditional call to a narrow
rule-disposition migration/validation entry point in `workflows/sync_rules.py`
— the same migration `sync_bundled_rules` embeds, invoked directly — which
completes before the daemon serves hooks and therefore before the receipt
capability can prepare any staged effect. The startup seam is proven where
it runs — `tests/test_runner_lifecycle.py` (whose `TestRunGobbyFunction`
entry-point cases already patch `init_storage_and_config`) gains cases
proving an ordinary non-dev daemon runs the migration exactly once before
hook service, an ambiguous or partial diagnostic aborts startup before hooks
are served, a clean or zero-write repeat proceeds in order, and the other
bundled domains and the user-template filesystem import are not invoked at
non-dev startup. Writes
apply by definition-version compare-and-set inside the sync transaction, a
second run performs zero writes, and a partial failure or undecidable row
blocks receipt-capability activation with one propagated diagnostic — through
`sync_bundled_content_to_db`'s result for the installer and CLI sync
callers, the narrow entry point's result for the startup caller, and
surfaced identically by the direct CLI (`cli/sync.py::sync --reinstall`) and MCP
reload (`mcp_proxy/tools/workflows/_import.py::reload_cache`) callers, never
swallowed. `src/gobby/cli/workflows/manage.py` no longer exists: the owner is
`cli/sync.py::sync` (`--reinstall`) with `_reinstall_bundled_definitions` and
`_delete_installed_definitions`; the bundled fan-out moved to
`src/gobby/sync_registry.py::sync_bundled_content_to_db`, and
`cli/installers/shared.py::sync_bundled_content_to_db` is a wrapper over it.
`sync --reinstall` already performs delete-plus-reinstall atomically per domain
inside one `db.transaction()` and its suite
(`tests/cli/test_sync_reinstall.py::test_failed_reinstall_leaves_prior_bundled_rows`)
pins prior-row preservation; what it lacks is a typed exit: errors are merged into
`result["errors"]`, so a blocking disposition failure must raise
`click.ClickException` with the preserved diagnostic (exit 1) instead of a summary
line, and the suite asserts `CliRunner` exit code 1 for both injected failure
shapes with the seeded installed row set byte-for-byte intact and a safe retry.
`reload_cache` already surfaces bundled-sync errors
(`tests/mcp_proxy/tools/workflows/test_import_reload.py::test_reload_cache_surfaces_bundled_sync_errors`)
but clears the loader cache before syncing, so it still gains the
stale-cache-activation-suppressed path: the direct suite injects ambiguous and
partial rule-sync results and asserts stale-cache activation is suppressed and the
rule/effect diagnostic is preserved. The startup entry point in `sync_rules.py` is
invoked through `gobby.sync_registry`.
First-run, repeated-run zero-writes, ambiguous-rollback, partial-failure, and
concurrent-edit cases are pinned in `tests/workflows/test_rule_yaml_sync.py`.
The migration is a snapshot; the invariant must survive every later write.
Rule definitions persist through live ingresses the startup trigger never
sees — MCP `create_rule`/`update_rule` (`mcp_proxy/tools/workflows/_rules.py`),
the HTTP create and full-replacement update endpoints
(`servers/routes/rules.py`), the CLI rule-file import (`cli/rules.py`), and
the generic `sync_imported_definition` (`workflows/imports.py`) — and a
definition missing the field deserializes to `eager`, so any one of them
could recreate the commit-before-delivery hole one write after the migration
cleared it. One shared write-time disposition classifier — applying the
same criterion as the migration — therefore validates every rule create,
full-definition update, and import before commit: a recognizable delivery
suppressor persists its explicit `on_receipt` grouping, an ambiguous
definition fails with the same rule/effect diagnostic, and post-activation
create-and-replace cases at each ingress
(`tests/mcp_proxy/tools/test_rule_tools.py`,
`tests/servers/routes/test_rules_routes.py`, `tests/cli/test_cli_rules.py`,
`tests/workflows/test_imports.py`) prove no eager one-shot guard activates.
Installed-global, user-global, and
project-owned rows are each tested through claim, release, acknowledgment, and
terminalization. A mixed rule — eager state beside an `on_receipt` guard in
one evaluation — is tested explicitly.

The wire changes ship as one strict protocol, never mixed-version behavior.
`retry_kind` and receipt-bearing responses change fail-open/fail-closed
behavior and delivery commitment, and the installed binary lags the repo, so
the daemon activates them only for a request whose originating ghook
advertises the matching hook-response capability — and the gate's input is
**request-carried, never stamp-read**. The machine-global
`.ghook-runtime.json` stamp is mutable: a reinstall rewrites it for every
process on the machine, so a stamp-read gate would classify an old envelope
drained after a reinstall — or a still-running old ghook — by the new
binary's capability and hand it response behavior its producer cannot honor.
Every hook envelope therefore carries an immutable producer
response-capability field beside `schema_version` (`envelope.rs` — today the
envelope carries only `schema_version`), written at envelope construction by
the binary that will consume the response, preserved verbatim through inbox
persistence and replay, and the daemon gates on that request-carried value
before adapter execution or effect preparation, for direct, detached, and
enqueue-only paths alike. The pre-field fleet is first-class, not an
afterthought: `inbox-envelope.v1.schema.json` declares
`additionalProperties: false`, so the optional response-capability property
is admitted in the v1 schema itself — an envelope written by a pre-field
ghook parses cleanly with the field absent, and **absence means
legacy/below-floor, never malformed**: pre-field direct and detached requests
are rejected before adapter execution exactly as advertised-below-floor ones
are, and pre-field enqueue-only envelopes take the same terminal quarantine;
missing-field parsing is tested through all three paths. The stamp still gains the capability field from its
producer — `runtime.rs::write_runtime_stamp` writes only `schema_version` and
`ghook_version` today — but as installation-health diagnostics beside
`hooks/runtime_compat.py`'s existing `schema_version` and minimum-version
pins, never as the per-request gate input. The gate covers every transport
path, pinned as a matrix rather than assumed: direct and detached POSTs are
rejected below-floor before adapter execution or effect preparation;
below-floor **enqueue-only** envelopes — whose ghook already emitted
`continue` locally, so no live process exists to consume a response — are
**terminally quarantined at drain**, not retried: today's drain retains every
non-2xx for the next pass forever (`inbox.py` warns and continues), so the
below-floor branch instead performs no adapter or effect execution, atomically
moves the envelope out of the active drain set following the existing
`_quarantine_or_warn` precedent, releases any lease,
emits an actionable protocol diagnostic, counts as settled for startup
barriers, and terminalizes any associated prepared receipt as undelivered.
The quarantine bound is a lifecycle, not an adjective: `_quarantine_or_warn`
today writes the envelope plus a `.meta.json` sidecar and nothing ever prunes
either, so quarantine gains a named `HOOK_QUARANTINE_RETENTION_WINDOW`
constant with a fixed default in `hooks/inbox.py`, measured on wall-clock
time against the quarantine timestamp persisted in the sidecar, with prune
eligibility strictly after the cutoff (an entry exactly at the boundary is
retained) and a bounded-batch pruner that removes payload and sidecar
coherently — recovering orphaned halves of a pair — registered in the same
`start_periodic_tasks` loop as receipt retention, with registration itself
proven at the scheduler boundary: the injected-loop harness in
`tests/test_runner_maintenance_startup.py` asserts both retention owners —
quarantine and receipt pruning — register and schedule exactly once with the
intended database and shutdown inputs and join the tracked periodic-task
set. Registration is half the lifecycle: the `workflow_audit_cleanup_loop`
precedent also declares a typed task attribute on `GobbyRunner` and enrolls
it in the exhaustive cancellation tuple in
`runner_lifecycle_shutdown.py::_cancel_periodic_tasks`, so both retention
tasks follow the precedent end to end — typed attributes declared on
`GobbyRunner` and initialized in `runner_init/storage.py`, failure tracking,
and cancel-and-await exactly once in `_cancel_periodic_tasks` before hook
storage and the database close — with idempotent-shutdown and
non-terminating-loop cases in `tests/test_runner_shutdown.py`; inside-cutoff,
exact-boundary, outside-cutoff, restart, orphan-file, bounded-batch, and
concurrent quarantine-versus-prune cases are tested;
the identity-less fallback stages nothing at any floor; and Claude's
statusline hook remains a local daemon-free non-effect path (`dispatch.rs`
returns `statusline::handle` before any transport) with an explicit
assertion. A request below the floor is rejected before adapter execution or
effect preparation with the stale-runtime diagnostic, never handed an
`adapter_timeout` or receipt-bearing response it cannot honor; the reverse
skew is safe by construction — a ghook that receives no receipt emits no
acknowledgment. Both version skews are tested, alongside the provenance
races: an old envelope drained after a reinstall and a still-running old
ghook posting after a reinstall are each gated by their request-carried
capability, not the refreshed stamp. V2's rebuild-and-reinstall gate moves
accordingly: the binary is reinstalled after 2.3 and again after this
deliverable, with an activation check that the installed ghook advertises the
receipt capability before any staged effect is prepared.

The same worker-exit fencing and disposition split
govern the generic `TimeoutError` branch for every source, so no non-AGY
response-bearing hook gains a duplicate-execution, fabricated-response, or
fail-open-critical path. The synthetic
phase also lands on 2.2's completed discovery contract — this deliverable depends on
2.2, and the synthetic `SESSION_START`'s reported `transcriptPath` routes through the
usable/pending/invalid classifier before selection or persistence, never persisted raw;
the two `flow.py::handle_session_start` edits are thereby ordered, not concurrent.
Each hook fires in a separate `ghook`
process, so the adapter has no process-local "first event" state to consult: it emits the
synthetic phase unconditionally, and idempotency lives at the session registration
boundary — `handle_session_start` keyed by provider plus `conversationId`. Repeated
synthetic `SESSION_START` events for one conversation must yield one canonical session,
one startup-context injection, and one transcript association, while every `PreInvocation`
still receives its own `BEFORE_AGENT` dispatch — first and repeated invocations are both
tested for both phases. Cardinality is record-driven, not assumed. With
`--input-format stream-json` (1.1.15, record 1.1.18) one process carries many turns, so
one `conversationId` produces many `PreInvocation`s from one process and one
`GOBBY_SESSION_ID` hint — the keying above already tolerates that, and the
persistent-process sequence PreInvocation(turn 1, timeout) → Stop(turn 1) →
PreInvocation(turn 2) must commit exactly once on turn 2, with the Stop handler
*releasing* the prepared receipt (terminal-undelivered for that turn) and never marking
the generation delivered. `Stop` is per-execution for AGY (`executionNum`, `fullyIdle`),
not process end. Interactive/terminal dispatch (record 1.1.17) and print mode share one
payload-driven path — the synthetic phase keys on payload fields only, never on launch
mode — so spawned terminals (6.1) and web-chat subprocesses (5.2) run identical adapter
code. Records 1.1.5/1.1.17 settled the cardinality: `PreInvocation` fires once per
model invocation within a turn (a tool-using turn produced 2–12 of them), and
`invocationNum` is a **per-turn origin** — `0` on the first invocation of every
turn, including a resumed or stream-input follow-up turn, while `initialNumSteps`
is cumulative across the conversation. So the first branch applies: `invocationNum
== 0` dispatches `BEFORE_AGENT` and later invocations dispatch
`HookEventType.BEFORE_MODEL`; the cumulative-counter branch is dead.

Line budget: `flow.py` is 934 lines and the registration-idempotency work lands in
`handle_session_start`. If it projects at or above 1,000, extract the idempotency keying
into a helper module within this task, per the `decompose-monolith` constraint.
`servers/routes/mcp/hooks.py` is 781 lines, and the claim commit, rollback, timeout
invalidation, retryable-envelope, and late-worker fencing work lands in `execute_hook`
and `_run_adapter_hook`. If it projects at or above 1,000, extract the adapter-execution
and envelope-finalization claim-lifecycle seam into its own module within this task.
The receipt-staging targets carry measured budgets too: `hooks/hook_manager.py` is 864
lines, `workflows/hooks.py` is 790, and `workflows/definitions.py` is 680 with the
delivery-disposition and grouping fields landing in `RuleEffect` — if any projects at
or above 1,000 under the staged-commit adoption, decompose it in the same task per the
`decompose-monolith` constraint.

Also correct `_agy_capabilities`: it declares `ContextChannel.NONE`, but 1.1.16's
`PreInvocation` and `PostInvocation` both accept `injectSteps` with `userMessage` and
`ephemeralMessage` payloads. Gobby's `inject_context` rule action currently cannot reach AGY
despite the CLI supporting it. Advertising the channel is not enough — the response side
must emit it. `AgyAdapter.translate_from_hook_response` already emits `injectSteps`
(`ephemeralMessage` for `context`, `userMessage` for `system_message`) but without the
adapter context-truncation helper and without capability backing, so 4.1.5 pins and
completes the existing emission rather than introducing it. Map unified
`HookResponse` context into the live-proven `injectSteps` structure (`userMessage` and
`ephemeralMessage` payloads) on `PreInvocation` and `PostInvocation` responses, applying the
existing adapter context-truncation helper, with `injectSteps` as the explicit transport.

`ContextChannel` has only `ADDITIONAL_CONTEXT`, `SYSTEM_MESSAGE`, `NONE`; neither names
AGY's transport, and `servers/routes/mcp/hooks.py` and `event_enrichment.py` route on the
enum. Add `ContextChannel.INJECT_STEPS = "injectSteps"` and declare it in
`_agy_capabilities` on `PreInvocation` and `PostInvocation` only; `PreToolUse`,
`PostToolUse`, `Stop` stay `NONE`.
`tests/adapters/test_capabilities.py::test_agy_hook_capabilities_have_no_live_transport_claims`
asserts `transport_capabilities == {}` (still true) and is re-anchored to assert the
per-event channels. The `EVENT_TYPE_CLI_SUPPORT` agy rows land in 2.3.10.

1.1.16 widened the response surface and record 1.1.24 measured what 1.1.18 honors.
**Honored live:** PreToolUse `decision: deny` (tool becomes a `TOOL_ERROR` step
`tool call denied by pre-tool hook: <reason>`, no `PostToolUse`), `deny_unless_prior_grant`
(the grant is `--dangerously-skip-permissions`; without it the tool is `Permission
denied`), `overwrite` (the rewritten arguments execute, while the stream and
transcript still show the original); PostInvocation `terminationBehavior: terminate`
(`Stop.terminationReason: TERMINAL_CUSTOM_HOOK`) and `force_continue` (re-invokes until
the print timeout — 46 invocations observed, so the "bounded by the 1.1.9 changelog"
claim is **not** a safety net); `injectSteps` `userMessage`/`ephemeralMessage`; `Stop`
`decision: "continue"` honored **10** times per execution, the 11th forced to end.
**Not honored:** `permissionOverrides` (headless auto-deny wins, exit 1 `user denied
permission`) and `injectSteps.toolCall` (fatal: `unknown injected step type: <nil>`,
`Stop.terminationReason: ERROR`, exit 1). **Hook exit codes:** a PreToolUse hook that
exits 1 or 2 blocks the tool regardless of stdout; a Stop hook's nonzero exit is
ignored. **Never observed:** `PostToolUse` for any `TOOL_ERROR` step, and
`PostToolUse.error` was `""` in every capture including a shell exit 7.

The unified `HookResponse` (`hooks/events.py`) has no field meaning "deny unless
previously granted", no permission-override structure (`updated_permissions` is
Claude-shaped and is **not** inferred into `permissionOverrides`), and no "run this
tool" field. Mapping: `AFTER_AGENT` `deny`/`block` → `terminationBehavior:
"force_continue"` with the normalized reason carried as an `ephemeralMessage` step,
`allow` omits the field, `terminate` is never emitted; `deny_unless_prior_grant` is
emitted only from an explicit `HookResponse` field introduced against the 1.1.24
shape, otherwise never; `force_ask` (unmeasured: headless auto-denies every ask),
`permissionOverrides` and `toolCall` inject steps are **never** emitted — the last two
because AGY does not honor them. Because `force_continue` is unbounded on AGY's side,
the adapter owns the bound: the existing `_is_cancelled_after_agent` heuristic stays
and the adapter emits `force_continue` at most `AGY_FORCE_CONTINUE_LIMIT` consecutive
times per execution (a module constant in `agy_contract.py`), after which a deny is
downgraded to an `ephemeralMessage`-only response. Tool-failure observation never
relies on `PostToolUse`: `AFTER_TOOL` is absent for failed tools on AGY, so failure
is visible only through the stream (§5.1) and the transcript (§4.2).

**Acceptance:**

- 4.1.1 - camelCase `transcriptPath`, `conversationId` and `workspacePaths` are read from AGY payloads. symbol: `AgyAdapter.translate_to_hook_event`. file: `src/gobby/adapters/agy.py`.
- 4.1.2 - An `AgyAdapter.handle_native` override dispatches the synthetic `SESSION_START` without process-local first-event state, dispatches the original `PreInvocation` as `BEFORE_AGENT` exactly once, and returns the original event's translated response with the synthetic phase's startup `context`/`system_message` merged into it — on first and repeated invocations. symbol: `AgyAdapter`. file: `src/gobby/adapters/agy.py`.
- 4.1.3 - AGY declares a context channel supporting `injectSteps` rather than `NONE`. symbol: `_agy_capabilities`. file: `src/gobby/adapters/capabilities.py`.
- 4.1.4 - The stale `tool_outcome` provenance stamp `agy.provider_contract_unproven` is replaced with live-proven outcomes. file: `src/gobby/adapters/agy.py`.
- 4.1.5 - Unified `HookResponse` context translates to `injectSteps` `userMessage`/`ephemeralMessage` on `PreInvocation` and `PostInvocation` responses. symbol: `AgyAdapter.translate_from_hook_response`. file: `src/gobby/adapters/agy.py`.
- 4.1.6 - An `inject_context` rule's payload reaches the emitted AGY hook response. test: `tests/adapters/test_adapters_agy.py`.
- 4.1.7 - Repeated `PreInvocation` events for one conversation yield one canonical session, one startup-context injection, and one transcript association. symbol: `handle_session_start`. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 4.1.8 - A per-turn `BEFORE_AGENT` rule (including `inject_context`) fires on every `PreInvocation`, not only the first, with the synthetic `SESSION_START` phase active. test: `tests/adapters/test_adapters_agy.py`.
- 4.1.9 - `flow.py` remains below 1,000 lines, or the idempotency keying is decomposed in the same task. file: `src/gobby/hooks/event_handlers/_session_start/flow.py`.
- 4.1.10 - Startup context commits exactly once through a durable claim generation owned by `SessionVariableManager.claim_startup_context`, allocated in a bounded shielded pre-submission preflight against a canonical session resolved by pre-created Gobby id — the hint fully validated before it binds anything: the selected row's `project_id`, `source`, `machine_id`, `session_type`, and persisted workspace/tombstone state must all match the envelope, a mismatching hint is rejected with a truthful diagnostic and falls through to ordinary resolution without adopting, rebinding, or mutating the mismatched row, and no claim, `conversationId` binding, transcript classification, or metadata write precedes validation — adopted by the five-part `(external_id, machine_id, source, project_id, session_type)` identity with `conversationId` bound onto the adopted row and `session_type` preserved, or idempotently registered — and committed only after the delivery receipt confirms emission to the live hook process, with provider-visible presentation at-least-once across the acknowledged crash window; the owning worker's synthetic phase adopts the pre-claim by owner token and delivers full startup context while non-owners observe live: first-event (no pre-existing session), pre-created-child, repeated, concurrent, and terminal-versus-web_chat-collision invocations each commit once, and wrong-project, wrong-source, wrong-machine, wrong-worktree, tombstoned-workspace, pending-transcript, and concurrent-switch hint mismatches each reject without mutation; translation failure, envelope-persistence failure, adapter timeout, queue-timeout-before-worker-start, and request-exit-before-preflight-completion each compare-and-roll-back or invalidate the generation so a later live turn re-delivers, a late-finishing timed-out worker can neither commit nor create a fresh claim, no live claim is ever ownerless, and no private claim or receipt field is emitted to AGY. test: `tests/servers/routes/test_hooks_agy_dispatch.py`.
- 4.1.11 - Pending-message delivery moves to the staged receipt commit for all six providers: the provider matrix — the five incumbents plus AGY — proves prepare-without-mark, acknowledged commit, transport release, duplicate-ack no-op, daemon restart, and terminal expiry. test: `tests/hooks/test_pending_message_provider_contracts.py`.
- 4.1.12 - Adapter timeout is a retryable envelope outcome with worker-exit fencing and a decoupled provider action: the retryable non-2xx carries a stable `retry_kind` discriminator and the 2.3-computed criticality action — `ingress_backpressure` keeps ghook's unconditional continue with retention, `adapter_timeout` honors the computed action with critical lifecycle hooks failing closed and noncritical hooks including every AGY event continuing — the envelope-claim disposition is decided at the finalization of the shielded executor future by compare-and-set on the lease token — a completed worker terminally processes the envelope with no replay, a failed worker releases the claim for daemon replay, and replay before finalization backs off — replay completes daemon-side effects only and never commits the claim generation or fabricates a provider-visible response, the uncommitted startup context re-delivers on the next live `PreInvocation`, and a forced-overlap test covers session registration, a non-idempotent rule, pending-message consumption, and activity mutation, plus non-AGY response-bearing critical and noncritical timeout cases in both retry classes pinning the same fencing and dispositions. test: `tests/servers/routes/test_hooks_agy_dispatch.py`.
- 4.1.13 - `hooks.py` remains below 1,000 lines, or the adapter-execution and envelope-finalization claim-lifecycle seam is decomposed in the same task. file: `src/gobby/servers/routes/mcp/hooks.py`.
- 4.1.14 - The claim generation lands as a numbered gcore migration and in the incumbent suites: the migration is registered through the embedded-asset contract (the number is claimed at implementation — 403 as of this revision, after 3.1's workspace-identity migration takes 402 with 376–401 registered in `crates/gcore/src/schema/assets.rs`; the receipt-effects migration takes 404 — and the range is re-checked against the applied set at implementation time, since parked plans go stale) with schema-identity coverage, and the hook fixtures and session-storage suites that hardcode the eager `context_injected` boolean — the direct handler case in `tests/hooks/event_handlers/test_session_variable_preservation.py` and the `update_terminal_pickup_metadata` signature pin in `tests/storage/test_sessions_import.py` included — re-anchor to claim, commit, rollback, and invalidate boundaries. test: `tests/storage/test_schema_contract.py`.
- 4.1.15 - One-shot response effects are staged behind the versioned delivery receipt, backed by the relational receipt-effects storage authority (`storage/hook_receipts.py`, migration `404_hook_receipt_effects.sql` registered through the embedded-asset contract, with focused storage tests in `tests/storage/test_hook_receipts.py`): the receipt carries `receipt_id`, the original envelope id, canonical session identity, and the monotonic delivery generation, echoed by the acknowledging ghook; ghook acknowledges only after emission-plus-flush succeeds — `output.rs` returns the I/O result and receipt metadata is stripped before action mapping; the original envelope is retained until the acknowledgment is durably enqueued; the identity-less `direct_post_after_enqueue_failure` fallback is explicit at-least-once presentation — the emitted response carries the one-shot payload while no receipt is created and no effect or dedupe claim commits, repeated presentation across consecutive enqueue failures is the tested mode, a later durable hook creates and commits the receipt, and a fallback-only session ending in Stop or expiry records terminal-undelivered; terminal owners compare-and-set only from prepared or released — never from acknowledged — so a durably enqueued acknowledgment beats a later Stop or expiry; the drain routes acknowledgments to a dedicated idempotent compare-and-set consumer where the CAS keys on `receipt_id`, delivery generation, and prepared state together — a stale-generation ack from an earlier carrying envelope is a recorded terminal no-op — duplicate direct and replayed acks are no-ops, no acknowledgment generates a receipt of its own, and a consumed acknowledgment terminalizes the original envelope without re-executing its hook; staged effects traverse durable prepared, acknowledged, released, and terminal-undelivered states with the `Stop` handler and both expiry owners recording terminal-undelivered; and write-failure, partial-flush-failure, death-after-write-before-ack, ack-write disk and permission failure, original-versus-ack drain ordering, queued-ack-then-Stop, restart recovery, duplicate replay, single-turn timeout, disconnect-after-persistence, acknowledged-delivery, pending-message rollback, repeated-enqueue-failure-with-no-durable-successor, durable-recovery-after-fallback-only-stretch, and stale-generation-ack-after-release-and-re-prepare cases are tested. test: `tests/servers/routes/test_hooks_agy_dispatch.py`.
- 4.1.16 - The envelope processing marker is a verifiable ownership lease at the route seam: renewed while the shielded future is live, reclaimable only after lease expiry plus failed owner-liveness validation, finalized by compare-and-set — with a worker forced past the replay grace period retaining exclusion, daemon-death recovery, and losing-owner finalization each pinned. test: `tests/servers/test_mcp_routes.py`.
- 4.1.17 - Every response-visible one-shot producer stages through the receipt authority: rule one-shot guards and sibling `set_variable`/`mcp_call` success variables, staged-memory injected-ID finalization, the discovery-dedupe claims (`dedup_memory_results`/`injected_memory_ids`, `dedup_skill_results`/`suggested_skill_names`, `_filter_and_track_new_review_lessons`/`injected_review_lesson_ids`), and the first-turn agent-preamble guard each commit on acknowledgment, release on transport loss, and terminalize on Stop or expiry — a write failure before emission suppresses no future delivery, and the rule-persistence, delivery-pipeline, and extracted-helper suites migrate to the staged boundaries — with the direct owners correctly attributed: the eager `claim_set_variable_values` dedupe contract lives in `TestDedupMemoryResults` and `TestDedupSkillResults` in `tests/hooks/test_hook_manager_extra.py`, which migrate to prepare-without-claim, acknowledged-commit, transport-loss release, duplicate-ack no-op, and terminalization cases while their ID-less and fail-open filtering cases are retained, and the first-turn preamble suite `tests/hooks/test_agent_events_coverage.py` re-anchors its first-prompt, persona-switch, and rehydration cases to staged guard boundaries, with the prior-activity stale-repair branch explicitly classified eager — it marks the guard from prior-session evidence and delivers no payload, so it pairs with no receipt. The split is producer-declared: `RuleEffect` carries the explicit delivery disposition (`eager` default, `on_receipt` on delivery-suppressing mutations, grouped with the payload they suppress within one fired rule), staged-commit code never infers one-shot status from sibling variable diffs or condition shapes, deliberately per-turn unguarded context and ordinary state stay eager, and the model round-trip and legacy-row deserialization are pinned in `tests/workflows/test_rule_models.py`. Propagation covers every ownership class: the bundled sweep enumerates all one-shot-guard templates — `guard-plan-memory-writes.yaml`, `memory-capture-nudge.yaml`, `handle-plan-mode-entry.yaml` (both rules), `discover-skill-hubs-on-turn-start.yaml` — while the sync path's typed data-migration/validation owner writes an explicit disposition onto matching user- and project-owned definitions preserving ownership and enabled toggles, fails ambiguous rows with an actionable diagnostic, and installed-global, user-global, and project-owned rows plus a mixed eager-plus-staged single-evaluation case are each tested. The direct behavioral suites for the edited templates — `tests/workflows/test_memory_lifecycle_rules.py`, `test_plan_mode_rules.py` (both rules), and `test_skill_discovery_rules.py` — assert each rule's eager/`on_receipt` payload grouping; `src/gobby/install/bundled_content_manifest.json` is regenerated with the YAML edits and the committed-manifest parity test stays green; and the disposition migration has one ordered production trigger — the narrow rule-disposition entry point invoked unconditionally from `runner_init/storage.py` at daemon startup, completing before receipt-capability activation, while the full `sync_bundled_content_to_db` aggregator stays install/CLI/dev-mode-only — applying by definition-version compare-and-set with a zero-write second run, every caller (installer, CLI sync, direct CLI `cli/sync.py`, MCP reload `_import.py`) propagating the failure diagnostic, and first-run, repeated-run zero-writes, ambiguous-rollback, partial-failure, and concurrent-edit cases pinned in `tests/workflows/test_rule_yaml_sync.py`; the invariant is write-preserved after activation: one shared write-time disposition classifier gates every post-activation rule ingress — MCP `create_rule`/`update_rule`, the HTTP create and full-replacement update endpoints, the CLI rule-file import, and `sync_imported_definition` — persisting explicit `on_receipt` grouping for recognizable delivery suppressors and failing ambiguous definitions with the same rule/effect diagnostic, with post-activation create-and-replace cases in `tests/mcp_proxy/tools/test_rule_tools.py`, `tests/servers/routes/test_rules_routes.py`, `tests/cli/test_cli_rules.py`, and `tests/workflows/test_imports.py` proving no eager one-shot guard activates; the startup trigger — the narrow entry point, unconditional at startup while the full aggregator stays install/CLI/dev-mode-only — is proven at the daemon-startup seam in `tests/test_runner_lifecycle.py` (ordinary non-dev daemon runs the migration exactly once before hook service, ambiguous or partial diagnostic aborts before hooks are served, clean or zero-write repeat proceeds, and the other bundled domains and user-template import are not invoked), and caller propagation is pinned at each direct seam — `tests/cli/test_sync_reinstall.py` and `tests/mcp_proxy/tools/workflows/test_import_reload.py` inject ambiguous and partial sync results and assert daemon reload/notification and stale-cache activation suppressed and the rule/effect diagnostic preserved, the CLI failure raising through Click with `CliRunner` exit code 1, and the reinstall path keeping its existing per-domain atomic delete-plus-reinstall transaction so both failure shapes leave the seeded installed row set byte-for-byte intact with safe retry. test: `tests/workflows/test_hooks.py`.
- 4.1.18 - The hook-response protocol is strict-versioned with request-carried provenance: every envelope carries an immutable producer response-capability field beside `schema_version` (`envelope.rs`), preserved through inbox persistence and replay, and the daemon gates on that request-carried value — never the mutable machine-global stamp — before adapter execution or effect preparation; `runtime.rs::write_runtime_stamp` publishes the capability beside `schema_version` and `ghook_version` as installation-health diagnostics joining the `runtime_compat.py` contract; `adapter_timeout` and receipt-bearing responses are never emitted to a request that has not advertised the capability, and the transport-path matrix is pinned — direct and detached POSTs gate before execution; below-floor enqueue-only envelopes are terminally quarantined at drain per the `_quarantine_or_warn` precedent with no adapter or effect execution, lease release, an actionable protocol diagnostic, startup-barrier settlement, and any prepared receipt terminalized undelivered — quarantine retention is the named `HOOK_QUARANTINE_RETENTION_WINDOW` constant with a fixed default in `hooks/inbox.py`, wall-clock against the sidecar-persisted quarantine timestamp, prune-eligible strictly after the cutoff (exact-boundary entries retained), pruned in bounded batches that remove payload and `.meta.json` sidecar coherently with orphan-pair recovery, from the same `start_periodic_tasks` loop as receipt retention, registration proven in the `tests/test_runner_maintenance_startup.py` injected-loop harness and shutdown owned end to end — typed task attribute on `GobbyRunner`, failure tracking, and cancel-and-await exactly once in `runner_lifecycle_shutdown.py::_cancel_periodic_tasks` before hook storage and database teardown, proven in `tests/test_runner_shutdown.py` — with repeated-drain, restart, inside/exact/outside-cutoff retention, orphan-file, bounded-batch, concurrent quarantine-versus-prune, and zero-effect assertions in `tests/hooks/test_inbox.py`; the v1 envelope schema — tracked as two byte-identical copies, `crates/ghook/schemas/inbox-envelope.v1.schema.json` and the root public mirror `schemas/inbox-envelope.v1.schema.json`, today `additionalProperties: false` — admits the optional response-capability property in both copies, kept byte-identical by a focused parity assertion, and an absent field parses cleanly and means legacy/below-floor — never malformed — with missing-field parsing tested through the direct, detached, and enqueue-only paths; every exhaustive `Envelope` literal is owned by the field addition — the diagnostics fixture (`crates/ghook/src/diagnostics.rs::envelope`) populates the response-capability member or migrates to a constructor — and every success-path raw-envelope test fixture carries the supported capability value: the constructors in `tests/servers/routes/mcp/test_hook_session_metadata.py`, `tests/servers/routes/test_hooks_droid_dispatch.py`, `tests/e2e/conftest.py::CLIEventSimulator._hook_envelope`, `tests/e2e/test_daemon_auth.py`, `tests/servers/routes/mcp_endpoints/test_execution_session_end_cleanup.py`, `tests/servers/routes/test_hold_open_gate.py`, `tests/servers/test_http_endpoints.py`, and `tests/servers/test_http_server.py`, while focused missing-field cases keep asserting pre-execution rejection and enqueue-only terminal quarantine; the identity-less fallback stages nothing at any floor; and statusline is asserted to remain a local daemon-free non-effect path outside envelope construction; both version skews plus the reinstall-boundary provenance races — an old envelope drained after reinstall and a still-running old ghook posting after reinstall — are tested, and the V2 reinstall-plus-activation check runs after this deliverable. file: `src/gobby/hooks/runtime_compat.py`.
- 4.1.19 - Receipt-effects growth is bounded by an implementable per-state retention lifecycle: the recovery/idempotency window is the named `HOOK_RECEIPT_IDEMPOTENCY_WINDOW` constant with a fixed default in `storage/hook_receipts.py`, measured on wall-clock time against the row's recorded transition timestamp with prune eligibility strictly after `transition_timestamp + window` (a row exactly at the boundary is retained); prepared rows are never pruned; released rows have both exits — the same receipt row atomically re-prepares by CAS from released to prepared when its payload moves to the next durable envelope, recording envelope lineage and incrementing the delivery generation so acknowledgments from the earlier carrying envelope can no longer commit, and terminal owners CAS from released to terminal-undelivered exactly as from prepared, so no released row is permanently non-prunable; acknowledged and terminal-undelivered rows become prune-eligible only after the window, a duplicate ack after pruning is a terminal idempotent no-op in the dedicated receipt consumer — a `receipt_id` resolving to no row executes no adapter or effect and writes no new record, with no dependence on any second durable ledger — and pruning runs in bounded indexed batches from the periodic receipt-retention loop registered in `start_periodic_tasks`, its registration proven in the `tests/test_runner_maintenance_startup.py` injected-loop harness (both retention owners schedule exactly once with the intended database and shutdown inputs and join the tracked periodic-task set) and its shutdown owned in the typed runner inventory — declared on `GobbyRunner`, cancelled and awaited exactly once in `_cancel_periodic_tasks` before hook storage and database teardown, with idempotent-shutdown coverage in `tests/test_runner_shutdown.py` — with just-inside-window, exact-boundary, just-outside-window, released-re-prepare, released-terminalization, stale-generation-ack-after-re-prepare, duplicate-ack-after-pruning, restart-recovery, and concurrent ack/terminal/prune race cases tested. file: `src/gobby/storage/hook_receipts.py`.
- 4.1.20 - camelCase aliasing is AGY-local: `agy_contract.py` declares `AGY_PAYLOAD_ALIASES` and the `toolCall{name,args}`→`tool_name`/`tool_input` flatten with `workspacePaths[0]`→`cwd`, `AgyAdapter.translate_to_hook_event` applies it before `ACPHookAdapter` extracts `session_id` and `cwd`, `normalize_tool_fields` and `ACPHookAdapter` gain no AGY keys, and `tool_input` string values decode through `decode_agy_tool_args` iff record 1.1.5 shows encoded-string args. test: `tests/adapters/test_adapters_agy.py`.
- 4.1.21 - `ContextChannel` gains `INJECT_STEPS`; `_agy_capabilities` declares it on `PreInvocation` and `PostInvocation` only; the hooks route and event enrichment treat it as a live channel; `test_agy_hook_capabilities_have_no_live_transport_claims` is re-anchored to the per-event channels with `transport_capabilities` still empty. symbol: `ContextChannel`. file: `src/gobby/adapters/capabilities.py`.
- 4.1.22 - Response fields are mapped per record 1.1.24's honored set: `AFTER_AGENT` deny/block emits `terminationBehavior: force_continue` with the reason as an `ephemeralMessage` step, capped at `AGY_FORCE_CONTINUE_LIMIT` consecutive emissions per execution, allow omits it, `terminate` is never emitted; `deny_unless_prior_grant` is emitted only from an explicit `HookResponse` field added against the recorded shape; `force_ask`, `permissionOverrides` (not honored), and `toolCall` inject steps (fatal on AGY) are never emitted and never inferred from `updated_permissions`; and the first synthetic `SESSION_START` of a live conversation produces the `source=agy` line in `~/.gobby/logs/hooks.log` and an AGY session row — the Dispatch Evidence Gate's condition-2 evidence, proven against the committed `hook-payloads.jsonl` fixtures. symbol: `AgyAdapter.translate_from_hook_response`. file: `src/gobby/adapters/agy.py`.
- 4.1.23 - `PreInvocation` cardinality follows records 1.1.5/1.1.17/1.1.18: under `--input-format stream-json` one process yields one canonical session; the sequence PreInvocation(turn 1, timeout) → Stop(turn 1) → PreInvocation(turn 2) commits the generation exactly once on turn 2 with Stop releasing rather than marking delivered; interactive and print dispatch share the payload-driven path; and because `invocationNum` is a per-turn origin, `invocationNum == 0` dispatches `BEFORE_AGENT` while later invocations in the same turn dispatch `BEFORE_MODEL`. test: `tests/servers/routes/test_hooks_agy_dispatch.py`.
- 4.1.24 - Migrations 403 and 404 land through the embedded-asset contract (`assets.rs` entries, regenerated `catalog.manifest.json`, `schema_expected_identity.json`, grant bundle pins, the `schema_contract.rs` and `cli_contract.rs` identity assertions, the `runner_tests.rs` enumeration, the freshness counts, the signed golden grant vectors, an untouched `baseline.sql`), `storage/hook_receipts.py` carries DML only, and `delivery-receipt.v1.schema.json` is tracked as two byte-identical copies with a parity assertion. file: `crates/gcore/src/schema/assets.rs`.
- 4.1.25 - The `adapter_timeout` provider-visible continue for agy is the protojson-legal per-event body from `action.rs::skip_stdout_json` (the body 2.3.7 establishes), never `{"continue":true}` nor `{"status":"error"}`, pinned per AGY event in `contract.rs`. file: `crates/ghook/src/action.rs`.

### 4.2 Add the AGY transcript parser [category: code] (depends: 2.1, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/sessions/transcripts/agy.py`
- `src/gobby/sessions/transcripts/__init__.py::get_parser`
- `src/gobby/sessions/processor_transcripts.py::*` — scope-reason: the codex-only incremental-parse and snapshot gates admit agy
- `src/gobby/sessions/transcript_source.py::_detect_source_from_path`
- `tests/sessions/test_transcript_source.py`
- `src/gobby/adapters/agy_contract.py::*` — scope-reason: the parser reuses the decode_agy_tool_args helper 4.1 declares beside the alias table
- `src/gobby/sessions/processor_lifecycle.py::ProcessorLifecycleMixin._hydrate_registration_from_sidecar`
- `tests/sessions/test_sessions_processor_integration.py::*` — scope-reason: sidecar reconstruction integration cases gain the agy append-admission path
- `tests/sessions/test_transcript_parsers.py::*` — scope-reason: the frozen registry assertion TestParserRegistry.test_registry_has_correct_parsers gains the agy entry
- `tests/sessions/test_agy_transcript_parser.py`
- `tests/tasks/test_agy_validation_evidence.py`

Add `AgyTranscriptParser` in the new module `src/gobby/sessions/transcripts/agy.py`,
subclassing `BaseTranscriptParser`, registered in `PARSER_REGISTRY`, and modeled on the
droid parser (the newest). The frozen registry assertion in
`TestParserRegistry.test_registry_has_correct_parsers` gains the agy entry. The record
shapes are verified:

Common fields are `step_index`, `source`, `type`, `status`, `created_at`. Records carry
`content`, `tool_calls`, `thinking`, `truncated_fields`, or `error`. Record 1.1.10
**disproved** the structured `exit_code` field and the typed tool records: on 1.1.18
the census across every Gate 0 conversation is `USER_EXPLICIT/USER_INPUT`,
`SYSTEM/CHECKPOINT`, `SYSTEM/SYSTEM_MESSAGE`, `MODEL/PLANNER_RESPONSE`, and
`MODEL/GENERIC` — nothing else.

- `USER_EXPLICIT/USER_INPUT` → user message from `content`.
- `MODEL/PLANNER_RESPONSE` → assistant message from `content`, thinking from `thinking`
  (this type alone carries it), or tool calls from `tool_calls` as `{name, args}` elements
  with native-typed `args`. A record carries `content` or `tool_calls`, not both.
- **Any other `MODEL/*` type is a tool result, and on 1.1.18 that type is always
  `GENERIC`** with free-text `content` (`Created At: … Completed At: …\n\nThe command
  exited with code 7.\nOutput:\nboom`). The typed set observed on 1.1.9/1.1.10
  (`RUN_COMMAND`, `VIEW_FILE`, `MCP_TOOL`, `LIST_DIRECTORY`, `GREP_SEARCH`, `SEARCH_WEB`,
  `CODE_ACTION`) no longer appears; the parser still treats any unknown `MODEL/*` type as
  a tool result so a future typed record degrades gracefully, but nothing may depend on
  one existing. Tool identity comes only from the preceding `PLANNER_RESPONSE.tool_calls[]`
  (positional pairing below). The shell exit status is recovered by parsing the sentence
  `The command exited with code N.` from `GENERIC.content` of a result paired to a
  `run_command` call (`agy_contract.py::parse_agy_command_exit`, anchored regex, `None`
  when absent); a `GENERIC` result without that sentence is an **unstructured** outcome
  for the evidence pipeline, never a success.
- `SYSTEM/*` (`CONVERSATION_HISTORY`, `CHECKPOINT`, `SYSTEM_MESSAGE`, `EPHEMERAL_MESSAGE`,
  `ERROR_MESSAGE`) is bookkeeping — skip, but do not treat as malformed. `CHECKPOINT`
  appears at step 1 of every conversation, single-turn ones included (record 1.1.16).

**File identity (record 1.1.22).** The parser consumes exactly the hook-reported
`transcriptPath` (2.2's disk table derives the same path from `conversationId`); sibling
files are never substituted. Record 1.1.22 names that file: `transcriptPath` is
`…/logs/transcript_full.jsonl`, the complete file — full tool-result `content` (AGY
itself caps tool output at ~8 KiB with a `<truncated N bytes>` marker inside the
content) and native-typed `tool_calls[].args`, never decoded. `transcript.jsonl` is a
token-efficient twin with the same `step_index` set, `content` capped at ~4 KiB with
`truncated_fields: ["content"]`, and every arg value JSON-string-encoded
(`"CommandLine":"\"ls -la\""`); it is never the parser input, and
`agy_contract.py::decode_agy_tool_args` (raw fallback) exists only so a test can prove
both files parse to identical records. The branch is keyed on the file's basename, never
on value shape, and the top-level file is authoritative over its chunked copies
(`chunks/*/00000000.jsonl` were byte-identical to their parents in every probed
conversation, largest 10,751 bytes; a second chunk never opened, so the rollover
threshold is unobserved and the parser treats chunk files as read-only mirrors it never
opens). Path-shape detection
for the `.gemini/antigravity-cli/brain/` form — chunked copies included — is
`_detect_source_from_path`'s rule, owned by 2.2.11 and pinned in
`tests/sessions/test_transcript_source.py`; this deliverable consumes it.

Tool calls pair to results by `step_index` order: a `PLANNER_RESPONSE` bearing `tool_calls`
is followed by its result record. AGY emits no tool-call ID, so derive a stable one from
conversation id plus `step_index`. Pairing is **positional only**: call-level names are
snake_case (`list_dir`, `find_by_name`, `run_command`, `call_mcp_tool`) while result
`type` is `GENERIC` on 1.1.18 (SCREAMING typed names on older versions) and never a case
transform of the name — names are never compared. `ParsedToolEvent.tool_name` carries
the snake_case call name normalized through the shared AGY tool map, the result `type`
is preserved as event metadata, and for a `run_command` pairing the parsed exit
sentence becomes the event's exit code for the evidence pipeline.

Handle `truncated_fields`: AGY self-truncates and names the affected fields, so a truncated
`tool_calls` may be structurally incomplete and must not raise. `status` is `DONE`, `ERROR`
or `RUNNING`; because the file is append-only with no rewrites, a `RUNNING` record is
permanently stale and marks an interrupted step.

Incremental correctness must survive daemon reconstruction. Parser-state sidecar
persistence is currently Codex-only — `processor_transcripts.py:220` snapshots
`parser.snapshot_state()` only when the parser source is `codex` — and the base snapshot
carries no call/result correlation. Because AGY pairs a `PLANNER_RESPONSE` bearing
`tool_calls` with a *later* result record by `step_index` order, a restart whose saved
cursor falls between the two would otherwise lose the pending tool-call ID. Persistence
is three gates, not one: `_parse_incremental_records` in `processor_transcripts.py`
(the codex-only `iter_parse_events`+`finalize` path), the snapshot gate in the same
module, and the reconstruction-side admission gate in
`ProcessorLifecycleMixin._hydrate_registration_from_sidecar`
(`processor_lifecycle.py`), which passes `allow_append=source == "codex"` to
`load_index_sidecar` — so an enlarged AGY transcript is rejected at rehydration even
with a saved snapshot. All three gates admit agy by parser capability (a
`supports_incremental_state`-style predicate) rather than a source-string comparison
(append-only growth is verified for AGY exactly as for Codex), and `AgyTranscriptParser`
implements `snapshot_state`/`hydrate_state` carrying the pending correlation, proven by
a restart test that appends the result record after a saved tool-call boundary.
Sidecars live under `~/.gobby/cache/transcript-indexes/<sha256>…` (c71085d120); no
sidecar is ever written under `~/.gemini`.

AGY transcript records carry no usage fields, so the parser emits `usage=None`; the
window-only branch (`_WINDOW_ONLY_CONTEXT_SOURCES`, `ContextUsageSnapshot.from_agy`)
remains the transcript-side context contract. Real context pressure comes only from the
stream `usage` object on the web-chat path, which 5.2 owns via
`ContextUsageSnapshot.from_token_breakdown(source="agy", …)`, retiring the `from_agy`
TODO there.

Every parsing behavior above is pinned by a focused test module,
`tests/sessions/test_agy_transcript_parser.py`, built on scrubbed fixtures from 1.1 —
registry membership alone proves nothing about parsing.

Parsing is also not the finish line: open tasks **#18381** and **#18677** require AGY
command outcomes to flow through the unified validation-evidence pipeline with full
provider parity. Definitive AGY tool outcomes must travel native result →
`ParsedToolEvent` (`transcripts/base.py`) → normalized outcome → stored
`TranscriptEvidence` (`tasks/transcript_evidence.py`) → readiness → close-time context,
with success, failure, nonterminal, contradictory, unstructured, and provenance-free cases
each behaving per the fail-closed matrix the other five providers already satisfy. This
deliverable, together with 4.1's live-proven provenance and the V2 parity run, **supersedes
#18381 and #18677**; close both with a supersession reference to this plan when the
approved manifest is applied.

Parity includes recovery, not only isolated outcomes: the live-captured zero- and
nonzero-exit `GENERIC` records from 1.1 (probe record 1.1.10, `transcript-manifest.json`
`zero_exit_run_command` / `nonzero_exit_run_command`, exit 0 and exit 7) are the payload
fixtures, and a
sequential case must prove a definitive failure holds readiness fail-closed until a later
correlated definitive success restores readiness and close-time context.

**Acceptance:**

- 4.2.1 - `AgyTranscriptParser` is registered in `PARSER_REGISTRY`. symbol: `get_parser`. file: `src/gobby/sessions/transcripts/__init__.py`.
- 4.2.2 - Every non-`PLANNER_RESPONSE` `MODEL/*` record parses as a tool result: the live 1.1.18 `GENERIC` form, the seven legacy typed forms, and unknown types; a `run_command` pairing yields the exit code parsed from the `The command exited with code N.` sentence via `parse_agy_command_exit`, and a `GENERIC` result without that sentence is an unstructured outcome. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.3 - `thinking` on `PLANNER_RESPONSE` is parsed distinctly from `content`. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.4 - Records naming `truncated_fields` parse without raising. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.5 - Malformed lines and unknown record types are tolerated with stable ordering preserved. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.6 - Tool-call IDs are derived stably from conversation id plus `step_index`. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.7 - Incremental reads resume correctly on an append-only file. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.8 - Focused fixture-backed tests cover every record class, unknown `MODEL/*` tools, `truncated_fields`, malformed lines, stable tool-call IDs, interrupted `RUNNING` records, and append-only incremental reads. test: `tests/sessions/test_agy_transcript_parser.py`.
- 4.2.9 - Success, failure, nonterminal, contradictory, unstructured, and provenance-free AGY outcomes flow through `ParsedToolEvent`, stored `TranscriptEvidence`, readiness, and close-time context with the same fail-closed behavior as the five incumbent providers. test: `tests/tasks/test_agy_validation_evidence.py`.
- 4.2.10 - A sequential case proves a definitive AGY failure keeps readiness fail-closed and a later correlated definitive success restores readiness and close-time context, using the record-1.1.10 live-captured `GENERIC` payloads (exit 0 and exit 7) from `transcript-manifest.json`. test: `tests/tasks/test_agy_validation_evidence.py`.
- 4.2.11 - Parser state persists and rehydrates across daemon restart: the codex-only persistence gate admits agy, `AgyTranscriptParser.snapshot_state`/`hydrate_state` carry pending tool-call correlation, and a restart test appends the result record after a saved tool-call boundary. test: `tests/sessions/test_agy_transcript_parser.py`.
- 4.2.12 - The reconstruction admission gate admits verified append-only AGY growth: `_hydrate_registration_from_sidecar` no longer restricts `allow_append` to codex, and an integration case reconstructs an enlarged AGY sidecar with the result appended beyond the saved boundary. symbol: `ProcessorLifecycleMixin._hydrate_registration_from_sidecar`. file: `src/gobby/sessions/processor_lifecycle.py`.
- 4.2.13 - The parser consumes exactly the hook-reported `transcriptPath` — `transcript_full.jsonl` per record 1.1.22 — whose `tool_calls[].args` are native and never decoded; the basename-keyed branch decodes `transcript.jsonl`'s JSON-string args once with raw fallback only so a fixture test proves both files parse to identical records, and chunk files are never opened. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.14 - Call/result pairing is positional by `step_index` only; snake_case call names and SCREAMING result types are never compared; `ParsedToolEvent.tool_name` carries the snake_case call name normalized through the shared AGY tool map and the result type is preserved as metadata. file: `src/gobby/sessions/transcripts/agy.py`.
- 4.2.15 - All three codex-only gates — `_parse_incremental_records`, the snapshot gate, and `_hydrate_registration_from_sidecar`'s `allow_append` — admit agy by parser capability rather than source string, and no sidecar is written under `~/.gemini`. file: `src/gobby/sessions/processor_transcripts.py`.
- 4.2.16 - AGY transcript records carry no usage: the parser emits `usage=None`, the window-only branch and `ContextUsageSnapshot.from_agy` stay the transcript-side contract, and stream-usage context pressure is owned by 5.2 via `from_token_breakdown`. file: `src/gobby/sessions/transcripts/agy.py`.

## P5: AGY Streaming Web Chat
`kind: framing`

**Goal**: A resumable streaming AGY web-chat backend on the subprocess protocol.

### 5.1 Add the AGY stream normalizer [category: code] (depends: 2.4, 4.2)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/agy_stream.py`
- `tests/servers/websocket/chat/test_agy_stream.py`
- `src/gobby/adapters/agy_contract.py::*` — scope-reason: the module-level AGY snake_case→canonical tool-name table is shared by the hook adapter and the stream normalizer and is module data rather than an indexed symbol
- `tests/adapters/test_agy_contract.py::*` — scope-reason: the shared tool-name table gains parity cases with the stream adapter

Add the new module `src/gobby/servers/websocket/chat/backends/agy_stream.py` translating
AGY NDJSON into the shared `StreamEvent` vocabulary (`init`, `content_delta`, `result`,
`error`) defined in `src/gobby/adapters/acp_stream.py`, mirroring `parse_droid_stream_line`
in the sibling `droid_stream.py`. `acp_stream.py` itself is unchanged — the new module only
imports from it.

Record shapes as observed on the **1.1.18** floor (probe records 1.1.6 and 1.1.18,
`stream-json-samples.jsonl`). The 1.1.8 changelog added `tool_info`, `subagent_info`,
and a `usage` block to the `result` body — observed as `{input_tokens, output_tokens,
thinking_tokens, cache_read_tokens, total_tokens}` on both `result` and the `DONE`
`agent_response` step; 1.1.15 fixed non-ASCII `text_delta` corruption. **Every record
is nested under its own event key** — the payload never appears flat beside `event`:

```
{"event":"init","conversation_id":"…","init":{"cwd":…,"tools":[…],"permission_mode":…}}
{"event":"step_update","step_update":{"conversation_id":…,"step_index":…,"state":…,"step_type":…}}
{"event":"result","result":{"conversation_id":…,"status":…,"response":…,"num_turns":…}}
```

The parser therefore reads `record["event"]` for the discriminator and
`record[record["event"]]` for the body; a parser written against a flat layout
sees every field as missing. Assistant output is `step_type="agent_response"`
carrying `text_delta`; tools are `step_type="tool"` with `state` in
`ACTIVE|DONE|ERROR`, `tool_name`, and `tool_info` containing `name`,
`parameters`, then `output` or a structured `error`. Stream-level `tool_name` is AGY's
**snake_case** spelling (`run_command`, `list_dir`, `find_by_name`, `view_file`, …) — not
Gobby's canonical names. The module exports `agy_tool_name_adapter(raw_tool_name)`
mirroring `droid_tool_name_adapter` in `droid.py`: it maps the AGY spelling table to
Gobby canonical names (`run_command` → `Bash`, file writers → `Write`, readers → `Read`,
…) and then runs `normalize_tool_fields` (`src/gobby/hooks/_normalization_tools.py`) so
shell and MCP spellings are canonicalized once — the same
`canonicalize_shell_tool_name` / `canonical_gobby_tool_name` path workflow enforcement
already uses (commit 32733c9735). The AGY spelling table lives in
`src/gobby/adapters/agy_contract.py` so the hook-path adapter (`adapters/agy.py`) and
this stream path cannot diverge. Record 1.1.12 captured the MCP spelling: every MCP
call is the single built-in `tool_name: "call_mcp_tool"` with
`tool_info.parameters {ServerName, ToolName, Arguments}` — the real tool identity is
`ServerName/ToolName`, so the adapter resolves `call_mcp_tool` to `mcp__<ServerName>__<ToolName>`
before `canonical_gobby_tool_name` (`src/gobby/workflows/enforcement/blocking.py`)
canonicalizes Gobby's own server tools. The terminal `result` body carries
`conversation_id`, `status` (`SUCCESS|ERROR|CANCELED`), `response`, `num_turns`,
`duration_seconds` (cumulative since conversation creation, record 1.1.1 — never
per-turn latency), `error` on failure, and the `usage` object. Tool output inside
`tool_info.output` is capped by AGY at ~8 KiB with a `<truncated N bytes>` marker.
The normalizer passes `usage` through verbatim in the `result`
`StreamEvent.data` under the key `usage`; it does **not** compute context pressure. The
consumer and owner of token/context tracking is 5.2
(`AgyManagedChatSession._translate_event` → `DoneEvent`), which is the existing web-chat
path through `ChatStreamPersistence.persist_done_metadata`
(`src/gobby/servers/websocket/chat/_stream_persistence.py`). The hook/transcript-side
`ContextUsageSnapshot.from_agy` window-only fallback is unchanged by this plan section.

**Turn-boundary contract.** `parse_agy_stream_line` is stateless and per-line. Turn
delimitation is a separate, explicit contract: a turn is the sequence of records from the
first record after the previous `result` up to and including the next `result`. The
module exports `iter_agy_turn(lines) -> AsyncIterator[StreamEvent]` that stops after
yielding the `result` event; `init` is yielded only when it is the first record of the
process (record 1.1.18: persistent mode emits exactly one `init` per process and one
`result` per turn; a repeated `init` would be skipped as bookkeeping, never treated as a
new session). A `result` with a non-`SUCCESS` status (`ERROR`, `CANCELED`) is still the
turn terminator. EOF before `result` yields exactly one `error` event with `code="eof"`
and ends the turn. Non-ASCII text deltas are delivered intact on 1.1.18 (1.1.15 fix) —
the parser asserts byte-exact UTF-8 round-trip and performs no mojibake repair.

The `step_type` vocabulary is wider than `agent_response` and `tool`: the probes
observed `user_input`, `checkpoint`, `system_message` (a resumed turn), `error_message`
(a turn AGY terminated on error, record 1.1.24), and a literal `unknown`. All five are
bookkeeping — skip them without emitting a `StreamEvent` and without treating them as
malformed, exactly as the parser treats an unrecognized `step_type` it has never seen;
the turn's failure is carried by the `result` record that follows, never by the
bookkeeping step.

Two correctness requirements. **Do not emit assistant text twice** — `result.response`
repeats what `agent_response.text_delta` already streamed. And derive tool-call IDs from
conversation id plus `step_index`, since AGY emits none.

Compaction parsing follows record 1.1.16, which is **negative**: no compaction or
context-pressure record exists in the stream, and the `checkpoint` step (a
conversation summary at step 1 of every conversation, single-turn ones included) is not
a pressure signal. The absent branch applies — this module declares no compaction event
and the shared `acp_stream.py` vocabulary stays unchanged; 5.2's `PRE_COMPACT` callback
has no AGY source (§5.3).

**Acceptance:**

- 5.1.1 - `init`, `step_update` and `result` records map onto shared `StreamEvent` types, read from the nested `record[record["event"]]` body rather than a flat layout. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.2 - Assistant text is not duplicated between `text_delta` and `result.response`. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.3 - Tool `ACTIVE`/`DONE`/`ERROR` transitions produce correct call and result events. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.4 - Malformed lines and unknown record types are tolerated without terminating the turn, and the probe-observed bookkeeping `step_type` values `user_input`, `checkpoint`, `system_message`, `error_message`, and `unknown` are skipped without emitting an event and without being classified malformed. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.5 - Focused tests cover init, text-delta, result deduplication, tool `ACTIVE`/`DONE`/`ERROR` lifecycle, the bookkeeping `step_type` values, a flat-layout record proving the nested read is required, and malformed-line branches. test: `tests/servers/websocket/chat/test_agy_stream.py`.
- 5.1.6 - Per record 1.1.16 (negative): no compaction event is declared, the stream vocabulary is unchanged, and a test pins that a `checkpoint` step emits no event. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.7 - `agy_tool_name_adapter` maps every stream-level snake_case spelling in the shared `agy_contract.py` table to Gobby's canonical name and then through `normalize_tool_fields`; `run_command` → `Bash` is pinned, and the record-1.1.12 MCP form `call_mcp_tool{ServerName,ToolName}` resolves to `mcp__<ServerName>__<ToolName>` and canonicalizes via `canonical_gobby_tool_name`. test: `tests/servers/websocket/chat/test_agy_stream.py`.
- 5.1.8 - The `result` event carries the upstream `usage` object verbatim under `data["usage"]` including `cache_read_tokens`; the parser computes nothing from it. file: `src/gobby/servers/websocket/chat/backends/agy_stream.py`.
- 5.1.9 - Turn-boundary contract: `iter_agy_turn` ends a turn exactly at the `result` record, passes a first-record `init` and skips repeated `init`s, and EOF before `result` yields exactly one `error` event with `code="eof"`; a two-turn NDJSON fixture (persistent-mode shape from record 1.1.18) proves two turns with no bleed. test: `tests/servers/websocket/chat/test_agy_stream.py`.
- 5.1.10 - Non-ASCII `text_delta` content round-trips byte-exact with no replacement-character handling in the module. test: `tests/servers/websocket/chat/test_agy_stream.py`.

### 5.2 Add the AGY web-chat backend [category: code] (depends: 5.1, 3.1, 3.2, 4.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/backends/agy.py`
- `tests/servers/websocket/chat/test_agy_backend.py`
- `tests/servers/websocket/chat/test_launch_contracts.py`

Add `AgyWebChatBackend` and `AgyManagedChatSession` in the new module
`src/gobby/servers/websocket/chat/backends/agy.py`, subclassing `ManagedChatSessionBase`
plus `ManagedWebChatPermissionsMixin` and satisfying `ChatSessionProtocol`. Model on
`DroidWebChatBackend`, the closest analogue. The mirror is concrete: `_DroidProcessHandle`
→ `_AgyProcessHandle`; `DroidWebChatBackend.attach_session` / `detach_session` /
`send_message` (reattach once on `BrokenPipeError`/closed stream, write one NDJSON line,
iterate `iter_agy_turn`; detach on `error code=eof`) / `interrupt` / `switch_model`
(process-level `--model`: detach then reattach with `--conversation`) /
`_terminate_handle` (close stdin, terminate, 2 s grace, kill) / `_log_process_stderr`.
Session-side: `DroidManagedChatSession._tool_name_adapter` → 5.1's
`agy_tool_name_adapter`; `_translate_event` maps `result` to
`DoneEvent(input_tokens, output_tokens, cache_read_input_tokens, context_window=self._resolve_context_window())`
from `data["usage"]` so `ChatStreamPersistence.persist_done_metadata` records cache-read
tokens — the single owner of AGY web-chat token/context tracking. Unlike Droid the child
env must **not** set `GOBBY_HOOKS_DISABLED=1` (5.3 single-authority); it does set
`GOBBY_WEB_CHAT_CHILD=1` and the canonical `GOBBY_SESSION_ID`/`GOBBY_PROJECT_ID` context.

**Transport: persistent process (decided by record 1.1.18).** One long-lived
subprocess per managed session, in the argv form 1.1.18 recorded:

```
agy --input-format stream-json --output-format stream-json --disable-slash-commands --dangerously-skip-permissions --sandbox=false --add-dir <workspace> --print-timeout <5.2.13 value> [--model …] [--effort …] [--conversation <id> on reattach]
```

There is **no `--print`/`-p`** in this form: `-p` requires an argument and a CLI prompt
is rejected together with `--input-format stream-json`. `--dangerously-skip-permissions`
is mandatory (record 1.1.7: without it every headless tool call auto-denies with
`status: CANCELED`, and a hook `allow` cannot override that); `--sandbox=false` is the
1.1.7 form when SRT enforces; `--add-dir <workspace>` is record 1.1.3's remedy (without
it `workspacePaths` is `[]` and the conversation binds to `default-cli-project`).
Prompts are NDJSON lines on stdin — `{"event":"user","message":{"content":"…"}}`
(a `[{"type":"text","text":"…"}]` content list is also accepted; any other block type
is rejected) — and turns are delimited by `result` (5.1.9); this mirrors the Droid/ACP
persistent-process model. 1.1.18 proved the three adoption criteria: (i) each stdin
message yields exactly one `result`; (ii) an in-flight turn **cannot** be cancelled
without process exit — SIGINT ends the process with `result{status:ERROR, error:"context
canceled"}` and exit 1 — but `--conversation` reattach preserves the conversation
(`num_turns` continues); (iii) `conversation_id` is stable across turns. The per-turn
process form is retained only as the reattach shape after process loss: `--conversation`
is the reconstruction/resume mechanism after process loss, websocket reattachment, model
switch, or daemon restart, and record 1.1.1 proved resume on 1.1.18 including after
SIGINT/SIGTERM; 5.2.3/5.2.9/5.2.10 are unconditional. A malformed stdin line, a
message without `event`, or a non-text content block is fatal (`result ERROR`, exit 1),
so the backend validates every outbound line before writing it. stdin EOF ends the
process with exit 0 after the current turn, which is the clean detach. Store the upstream AGY
conversation id **separately** from Gobby's canonical
session and conversation identity; they are different namespaces and conflating them will
corrupt session resume. Persist it in the existing chat-session metadata used for session
reconstruction — no new store — so resume survives websocket reattachment and runtime
reconstruction, not only a continuously live managed session. Cancellation or a failed
result preserves the last confirmed usable id rather than clearing or overwriting it.
This deliverable depends on 4.1 because the persisted upstream id enters the five-part
`(external_id, machine_id, source, project_id, session_type)` adoption contract: a
terminal session and a web-chat session sharing one AGY `conversationId` must remain
distinct rows, with hook events resolving only their own `session_type` — proven
through the real backend, not the storage helper alone. The `conversationId` alone
cannot select between those rows, so the selection has a production mechanism, not
just a storage contract: every AGY web-chat subprocess exports the canonical
pre-created row identity — the session's `db_session_id`, project id, and source —
into its spawn environment through the ghook-recognized session context
(`GOBBY_SESSION_ID`/`GOBBY_PROJECT_ID`, the same channel spawn-time terminals use;
`dispatch.rs` reads it into the envelope), and 4.1's resolve-or-adopt preflight
resolves that canonical id and validates the row's full identity — project, source,
machine, `session_type=web_chat`, and persisted workspace state — before any
external-id fallback. A hook from a web-chat subprocess therefore lands on the
web_chat row by canonical identity, never by external-id guessing. The identity
export composes with the SRT wrap through 3.1's single environment algorithm: the
identity/base environment is built first, passed into `prepare_sandbox_launch`,
merged with `launch.provider_env`, and handed with the wrapped argv to subprocess
creation — so the canonical context and the sandbox-injected variables reach the
same child process, proven jointly at the launch seam. Native ghook hooks are the
**only** workflow-effect authority for these subprocess-driven events — the managed
session must not mirror them through `_fire_lifecycle`; 5.3 owns that
single-authority contract.

Required behaviors: streaming text, tool lifecycle events, error and non-zero exit handling,
cancellation, per-session locking via `ManagedChatSessionBase._lock`, `--model` and `--effort`
arguments, stderr redaction through a dedicated drain task, and the shared
`ACP_STREAM_READER_LIMIT_BYTES` reader limit from 2.4. Do **not** copy
Droid's unbounded `readline()` (even though AGY caps each tool output at ~8 KiB — the
limit is a defence, not a sizing claim; 5.2.8's >64 KiB case is a synthetic line).
Cancellation implements exactly what records 1.1.8 and 1.1.18 prove: there is no
in-process interrupt, so `interrupt` is `_terminate_handle` (close stdin, SIGTERM, 2 s
grace, SIGKILL) plus conversation-id preservation, and the next turn reattaches with
`--conversation`. AGY's own exit on SIGINT/SIGTERM leaves the in-flight tool's **shell
child running** (a `sleep 40` outlived the CLI) while its MCP child dies with it — so
`_terminate_handle` terminates the owned process **tree** (the SRT wrapper's process
group), not the CLI pid alone. Exactly one terminal event, lock release, last confirmed
usable id preserved. Never invented semantics.

The read timeout is the 2.4 contract, restated so no implementer invents a different one:
`DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` (120 s) as a **per-line inactivity** clock on each
`readline()`, reset by every received line, with env override
`GOBBY_AGY_ACP_PROMPT_TIMEOUT_SECONDS`. On expiry: exactly one terminal error event for
the turn, termination of the owned subprocess tree, lock release, the last confirmed
usable conversation id preserved, and a session that remains reconstructable — never a
silent hang, a duplicate terminal error, or a locked orphaned session.

Two clocks exist and they are different kinds. The wrapper's inactivity clock is
**renewable** — every received line resets it — while `--print-timeout` is an **absolute
whole-turn** limit, so no finite value of it can be subordinate to a renewable clock: a
healthy stream that keeps resetting the inactivity window will eventually cross any
finite whole-turn bound. Probe record 1.1.13 settles the policy (recorded on 1.1.10,
re-confirmed on 1.1.18):

- The flag takes **Go duration syntax** (`90s`, `5m`, `2h30m`) and defaults to `5m0s`;
  `banana` exits 2.
- There is **no disable sentinel** — `0` expires immediately, no `off`, no `none`, and
  omission yields the `5m0s` default rather than an unbounded turn.
- `2562047h` is accepted on 1.1.18 (re-run: the turn completes normally). It is the
  largest value the parser admits and is the **effectively-unbounded form** this plan
  uses.
- Expiry exits **1**. In text mode the message goes to **stderr**; under
  `--output-format json|stream-json` (the forms 5.2 uses) the message is a **stdout**
  `result{status:ERROR, error:"timeout waiting for response"}` record — byte-identical
  to the SIGINT/SIGTERM result — followed by exit 1. Mid-stream expiry leaves the tool
  step `ACTIVE` and its shell child running. The committed 1.0.11 fixture's
  exit-0-on-stdout shape stays disproven: the payload is on stdout, but the exit is 1.
- Under `--input-format stream-json` the clock is **per turn**: a process idled 25 s
  past a 2 m timeout between turns survived and answered the next turn.

5.2 therefore passes `--print-timeout 2562047h` and lets the renewable inactivity clock
be the only real limit, exactly as the disabled-or-unbounded branch intended. Because the
flag is per turn, the default would not kill an idle persistent session, but it would
still cap a long healthy turn, so the effectively-unbounded value remains mandatory.
`--disable-slash-commands` stays. The absolute bound still exists at roughly 292 years
and needs no separate maximum-turn contract. A CLI-timeout expiry, should one ever
occur, is a **nonzero exit** whose error text arrives as the turn's `result` record and
is surfaced through the same path as any other nonzero exit (5.2.7): one terminal error,
owned process-tree cleanup, lock release, and the last confirmed usable conversation id
preserved.

This backend also contributes the **AGY row to the 3.1 launch-contract matrix**: exactly one
SRT wrapper, the bounded network policy represented and accepted, AGY's native `--sandbox`
pinned off in the 1.1.7-recorded form when SRT enforces, and a stale policy hash refusing
resume.

Compaction follows record 1.1.16, which is negative: no stream compaction or
context-pressure signal exists, so this backend has no compaction path and 5.3 removes
`PRE_COMPACT` from the AGY parity claim. Context pressure for AGY web chat is derived
only from the `result`/`agent_response` `usage` object against the resolved context
window (5.2.17).

Plan mode is not a per-turn process flag: `--mode plan|accept-edits` (1.1.12) is set at
process start, so a persistent session cannot flip it per turn. AGY web-chat plan-mode
write blocking therefore rides the native `PreToolUse` deny through the hook route (the
5.3 single authority), exactly as `are_plan_mode_write_paths_allowed`
(`src/gobby/servers/tool_approvals.py`) gates Codex/Droid on the stream side. Record
1.1.23 showed headless `--mode plan` writes the plan as an artifact under
`brain/<id>/<name>.md`, makes no workspace write, and emits no approval record, while
`init.permission_mode` stays `request-review` — additive and harmless to hook-driven
denial, but also useless to the web-chat flow (no approval round-trip exists), so it
is **not** passed at attach.

**Acceptance:**

- 5.2.1 - A first turn spawns the documented argv — no `-p`, with `--input-format stream-json`, `--dangerously-skip-permissions`, `--add-dir <workspace>`, and `--sandbox=false` when SRT enforces — and streams assistant text. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.2 - A subsequent turn writes one NDJSON line to the live process, and every reattach after detach/teardown resumes with `--conversation` carrying the upstream id (5.2.16). file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.3 - The upstream AGY conversation id is persisted in existing chat-session metadata, distinct from Gobby's session identity. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.4 - A concurrent turn on a locked session is rejected. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.5 - Cancellation terminates the owned process tree (not the CLI pid alone, since records 1.1.8/1.1.18 show shell children outlive the CLI) and releases the lock; no in-process interrupt is attempted. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.6 - Model and effort selections reach the argv. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.7 - stderr is redacted and a non-zero exit surfaces as an error event. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.8 - A tool output above 64 KiB is read without `LimitOverrunError`. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.9 - After teardown and reconstruction of the managed session, the next turn's argv carries the same `--conversation` id. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.10 - Cancellation or a failed result preserves the last confirmed usable conversation id, matching the post-interrupt resumability recorded by 1.1.8 and 1.1.18. file: `src/gobby/servers/websocket/chat/backends/agy.py`.
- 5.2.11 - The AGY row joins the launch-contract matrix: exactly one SRT wrapper, bounded-network-policy representation, native `--sandbox` off in the 1.1.7 form, stale-hash refusal, and the 3.2.4 sandbox-policy entries proven at the launch seam — credentials readable, state and transcript roots writable, probe-recorded domains granted, everything else refused — and one real-backend case captures the wrapped argv and the final child environment **together** at the child boundary, proving the canonical identity variables and `launch.provider_env` (TMPDIR included) both survive 3.1's composition algorithm in the production launch. test: `tests/servers/websocket/chat/test_launch_contracts.py`.
- 5.2.12 - Timeout expiry and reset follow the 2.4 inactivity contract: exactly one terminal error, owned process-tree cleanup, lock release, preserved confirmed conversation id, and a reconstructable session, with expiry and reset-on-activity tests. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.13 - The turn-limit policy matches the 1.1.13 record: the argv carries `--print-timeout 2562047h` (the accepted effectively-unbounded value; no disable sentinel exists and omission would apply the `5m0s` default), a turn streaming steadily past the former 60-second setting completes under the inactivity clock alone, and a CLI-timeout expiry is asserted as a nonzero exit whose stdout `result{status:ERROR,error:"timeout waiting for response"}` record is routed through the 5.2.7 nonzero-exit path — one terminal error, process-tree cleanup, lock release, and conversation-id preservation — with no zero-exit error payload expected anywhere. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.14 - Provider-native identity survives the five-part session contract end to end through the production handoff: the AGY web-chat subprocess spawn environment carries the canonical `db_session_id`, project id, and source through the ghook-recognized `GOBBY_SESSION_ID`/`GOBBY_PROJECT_ID` context — captured from the real backend launch, not injected by the test — an AGY web-chat session persists and reconstructs its upstream conversation id, a terminal session sharing the same `conversationId` coexists as a distinct row, hooks driven through the actual adapter/hook route resolve the web_chat row by canonical identity and terminal hooks only the terminal row with no external-id fallback selecting across `session_type`, and neither path mints, rebinds, or resumes the other. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.15 - Per record 1.1.16 (negative): the backend has no compaction path, never invokes a `PRE_COMPACT` callback, and a test pins that a `checkpoint` step triggers no lifecycle call; `PRE_COMPACT` drops from the AGY parity claim. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.16 - Transport follows the 1.1.18 record: one subprocess per managed session is attached with the recorded argv, a second turn writes one validated `{"event":"user","message":{"content":…}}` line to the same process's stdin with no new spawn, an outbound line that would be fatal to AGY (missing `event`, non-text block) is rejected before writing, and `--conversation` appears only on reattach after detach/teardown. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.17 - The `result` `usage` object reaches persistence: `_translate_event` emits a `DoneEvent` with input, output, and cache-read tokens and the resolved AGY context window, and `persist_done_metadata` is invoked once per turn with those values. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.18 - Persistent-mode process loss (EOF before `result`, nonzero exit, or `_terminate_handle`) yields exactly one terminal error, preserves the last confirmed conversation id, and the next turn reattaches with `--conversation` carrying it; `switch_model` detaches and reattaches with the same id. test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.2.19 - The AGY child environment never contains `GOBBY_HOOKS_DISABLED`, does contain `GOBBY_WEB_CHAT_CHILD=1`, the argv never carries `--mode`, and plan-mode write blocking is asserted through the native `PreToolUse` deny path (per record 1.1.23). test: `tests/servers/websocket/chat/test_agy_backend.py`.

### 5.3 Integrate AGY into WebChatRuntimeManager [category: code] (depends: 5.2, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.create_session`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.health`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.start`
- `src/gobby/servers/websocket/chat/runtime_manager.py::WebChatRuntimeManager.stop`
- `src/gobby/servers/websocket/handlers/session_config.py::handle_set_provider`
- `src/gobby/servers/websocket/chat/_lifecycle.py::ChatLifecycleMixin._fire_lifecycle`
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: the fire-and-forget SESSION_START pre-fire at session creation becomes provider-conditional, suppressed for AGY under the native single-authority contract, ordered after 3.1's launch-seam edits to the same file
- `src/gobby/servers/websocket/chat/_session_launch.py`
- `tests/servers/websocket/test_set_provider.py::*` — scope-reason: provider-switch cases gain the agy row across switching, cancellation, teardown, pending-provider state, and confirmation
- `tests/servers/test_fire_lifecycle_parity.py::*` — scope-reason: the managed web provider lifecycle parity matrix gains a behavioral agy row driving all five lifecycle events
- `tests/servers/websocket/chat/test_agy_backend.py`

Replace the two hardcoded AGY rejections — the `RuntimeError` in `create_session` and the
unavailable `ProviderBackendHealth` in `health` — with real startup health, session creation,
shutdown and provider status. AGY is already accepted as a valid provider slug in
`_message_ingress.py`, `_session.py` and `routes/sessions/core.py`, but the
runtime manager is not the last gate: `handle_set_provider`
(`session_config.py`, `valid_providers` literal) validates against its own closed
five-provider set, so a websocket client switching an existing
conversation to AGY still receives "Invalid provider" after the manager accepts it —
admit agy in that validation source in this deliverable. Gate availability on the
immutable support record from 2.5 — the synchronous health path reads the record and
never re-probes. `gobby status`'s AGY transport disclaimer and the hooks-installed
detection are corrected in 2.6; 5.3 owns only the daemon runtime gate. The web UI's
`HIDDEN_PROVIDERS = {"agy"}` gate (commit ca1ea53474, #20049) is **not** lifted here: it
is the UI twin of `ProviderMetadata("agy").supports_web_chat=False` and is removed in 6.2
together with the capability flip, so the UI never offers a provider the registry still
reports unavailable (6.2 depends on 5.3, so ordering is preserved). Runtime acceptance is
also parity-tested:
`tests/servers/test_fire_lifecycle_parity.py` parametrizes the managed web providers
as exactly the five incumbents, so AGY joins that lifecycle parity matrix here — and
the parity is behavioral, not enumerative, with **exactly one workflow-effect
authority per event**. AGY is unique among the managed providers in running both
lifecycle machineries at once if left unreconciled: 5.2.14 requires the subprocess
to export the canonical session context and drive the real adapter/hook route, so
native ghook hooks fire and execute the full `HookManager` path — while
`ChatLifecycleMixin._fire_lifecycle` (`websocket/chat/_lifecycle.py`) is an explicit
`HookManager.handle` mirror running rule evaluation, blocking webhooks, `mcp_call`
dispatch, event handlers, pending-message piggyback with delivery marking, and
broadcasts. Driving both for one turn would execute every rule, MCP call, handler,
message delivery, webhook, and broadcast twice. Droid resolves this collision on
the stream side by disabling native hooks in its child environment
(`GOBBY_HOOKS_DISABLED=1`, `backends/droid.py`); AGY takes the opposite,
5.2.14-mandated branch: **native ghook is the sole workflow-effect authority** for
`SESSION_START`, `BEFORE_AGENT`, `BEFORE_TOOL`, `AFTER_TOOL`, and `STOP` on AGY
web-chat sessions — the managed session's stream parsing is limited to UI text
and tool-state events and never routes those event types through
`_fire_lifecycle`. `SESSION_START` is in that list because it collides at
session creation, not at streaming: `_session.py` today fires `SESSION_START`
through `_fire_lifecycle` fire-and-forget for **every** provider when the
managed session is created, while 4.1 synthesizes AGY `SESSION_START` from the
first native `PreInvocation` — left unreconciled, AGY startup would evaluate
twice, and the managed pre-fire could mark startup context delivered before
the receipt-bearing native response ever reaches the child. The managed
pre-fire therefore becomes provider-conditional and is suppressed for AGY (the
3.1 performs the move of the pre-fire into `src/gobby/servers/websocket/chat/_session_launch.py`,
so the conditional lands there, with `_session.py` touched only where the call site
remains);
the native synthetic phase is the only `SESSION_START` evaluator, and an
end-to-end first-`PreInvocation` case proves exactly one synthetic
`SESSION_START`, exactly one `BEFORE_AGENT`, and startup context plus system
message delivered and committed only through the native receipt path. The AGY parity row
is therefore driven through the actual adapter/hook route, asserting block
decisions, injected context, modified input, source, and **every side effect
exactly once** — rule effects, MCP dispatch, handler context, pending-message
delivery, webhooks, and broadcasts each single-fired per event. `_fire_lifecycle`
still gains the parsed-provider fix for the incumbents — it hardcodes
`source="claude"` when building `PRE_COMPACT` compaction context — with a focused
unit case pinning parsed-provider compaction context, and the incumbent
provider-list case is retained as the closed-registry guard. `PRE_COMPACT` follows
record 1.1.16, which is negative: AGY has no compaction hook and no stream compaction
or context-pressure signal (the `checkpoint` step is a conversation summary written
at step 1 of every conversation). `PRE_COMPACT` is therefore **removed from the AGY
backend-parity claim**: the agy row of the parity matrix marks it unsupported with the
record as its reason, 5.1 declares no compaction event (5.1.6) and 5.2 has no
compaction path (5.2.15).

**Acceptance:**

- 5.3.1 - `create_session(provider="agy")` returns a live session instead of raising. symbol: `WebChatRuntimeManager.create_session`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 5.3.2 - `health("agy")` reports real backend health gated on the 2.5 support record. symbol: `WebChatRuntimeManager.health`. file: `src/gobby/servers/websocket/chat/runtime_manager.py`.
- 5.3.3 - An AGY session streams text and tool events, resumes, and interrupts over the websocket, with interrupt behavior matching the 1.1.8/1.1.18 records (process-tree termination, preserved conversation id, `--conversation` reattach). test: `tests/servers/websocket/chat/test_agy_backend.py`.
- 5.3.4 - `handle_set_provider` admits agy: switching an existing conversation to AGY succeeds through pending-provider state and confirmation, with cancellation and teardown of the old session covered. test: `tests/servers/websocket/test_set_provider.py`.
- 5.3.5 - The managed web lifecycle parity matrix gains a behavioral agy row under the single-authority contract: native ghook is the sole workflow-effect authority for AGY `SESSION_START`, `BEFORE_AGENT`, `BEFORE_TOOL`, `AFTER_TOOL`, and `STOP` — the managed session's creation-time fire-and-forget `SESSION_START` pre-fire in `_session.py` is suppressed for AGY and its stream handling never routes those event types through `_fire_lifecycle`, and the agy row drives them through the actual adapter/hook route with block-decision, injected-context, modified-input, and source assertions plus exactly-once assertions on rule effects, MCP dispatch, handler context, pending-message delivery, webhooks, and broadcasts; an end-to-end first-`PreInvocation` case proves exactly one synthetic `SESSION_START`, exactly one `BEFORE_AGENT`, and startup context committed only through the native receipt path; `_fire_lifecycle` passes the session's parsed provider into compaction context with a focused unit case; `PRE_COMPACT` is marked unsupported in the agy row per record 1.1.16 (negative) and never fires for AGY; and the closed-registry provider guard is retained. symbol: `ChatLifecycleMixin._fire_lifecycle`. file: `src/gobby/servers/websocket/chat/_lifecycle.py`.

## P6: AGY Spawn, Capabilities, and Catalog
`kind: framing`

**Goal**: Turn on the remaining AGY surfaces and retire the stale metadata.

### 6.1 Enable AGY terminal spawning [category: code] (depends: P4, 3.2, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_executor.py::*` — scope-reason: multi-symbol edit in which execute_spawn loses its AGY rejection and gains dispatch while the new _spawn_agy_terminal helper lands alongside the five existing spawners
- `src/gobby/agents/spawners/command_builder.py::build_cli_command`
- `src/gobby/mcp_proxy/tools/spawn_agent/_provider_resolution.py::*` — scope-reason: the AGY entry lands in the module-level SPAWN_CAPABLE_PROVIDERS frozenset, which is module data rather than an indexed symbol
- `src/gobby/agents/watchdog/agy.py`
- `src/gobby/agents/watchdog/models.py::*` — scope-reason: the module-level KNOWN_WATCHDOG_PROVIDERS frozenset gains the agy entry
- `src/gobby/agents/watchdog/registry.py::*` — scope-reason: the module-level reader map gains the agy reader, and the import-time guard requires provider set and map to change together
- `tests/agents/watchdog/test_agents_watchdog_registry.py::*` — scope-reason: registry parity assertions gain the agy row
- `tests/agents/watchdog/test_agents_watchdog_models.py::*` — scope-reason: provider-set assertions gain the agy entry
- `src/gobby/agents/resume_executor.py::*` — scope-reason: the module-level SUPPORTED_RESUME_PROVIDERS frozenset and the provider resume-argv seam gain agy together
- `src/gobby/adapters/plan_keystrokes.py::*` — scope-reason: the module-level DEFAULT_PLAN_KEYSTROKES registry gains the agy row when the 1.1.14 record proves a menu; an absent menu leaves the registry without an agy row
- `tests/adapters/test_plan_keystrokes.py::*` — scope-reason: the all-CLIs coverage guard gains the agy row and dispatch cases, or the explicit executable-vs-unsupported distinction
- `src/gobby/communications/native_plan_actions.py::*` — scope-reason: an absent AGY plan menu records its probe-backed negative contract in the native plan-action service
- `tests/communications/test_native_plan_actions.py::*` — scope-reason: the AGY executable-menu or explicit-unsupported case joins the native plan-action suite
- `src/gobby/servers/websocket/handlers/plan_approval.py::handle_attached_plan_approval`
- `tests/servers/websocket/test_attached_plan_approval.py::*` — scope-reason: the attached-approval path gains the same AGY executable-menu or probe-recorded refusal behavior
- `src/gobby/agents/spawners/auth_env.py::*` — scope-reason: the module-level CLI_ENV_ALLOWLIST and CLI_CREDENTIAL_KEYS maps gain their agy rows from the 1.1.15 auth record
- `src/gobby/agents/tmux/spawner.py::*` — scope-reason: the module-level _SUPPORTED_AUTH_CLIS set is reconciled with AGY's recorded auth shape
- `src/gobby/agents/spawners/base.py::*` — scope-reason: spawn-env construction copies os.environ and must strip the 1.1.15-recorded AGY ambient credentials through the shared normalized inventory
- `tests/agents/test_tmux_integration.py::*` — scope-reason: a live spawned child under a seeded ambient environment proves denied credential variables are absent and allowed variables remain
- `tests/agents/spawners/test_auth_env.py::*` — scope-reason: AGY allowlist and credential cases — or the explicit empty-row ambient-strip case — join the auth-env suite
- `tests/agents/test_resume_executor.py::*` — scope-reason: AGY daemon-restart recovery cases join the resume-executor suite
- `tests/agents/test_spawn_executor.py::*` — scope-reason: existing spawn-executor tests gain the AGY spawner, cwd-remedy, linkage, and version-gate cases
- `tests/agents/spawners/test_command_builder.py::*` — scope-reason: AGY argv cases join the builder suite
- `tests/mcp_proxy/tools/spawn_agent/test_provider_resolution.py::*` — scope-reason: AGY provider-selection cases join the resolution suite
- `src/gobby/install/shared/detection/agy.toml`
- `tests/agents/test_idle_detector.py::*` — scope-reason: an AGY pane-capture class joins the status-bar filtering suite
- `tests/agents/detection/test_agents_detection_registry.py::test_bundled_manifests_cover_supported_providers_and_rule_contract`
- `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: the agy.toml hash entry is regenerated with the detection-rule rewrite

Remove the early-return rejection in `execute_spawn` and add `_spawn_agy_terminal` alongside
the five existing spawners. Add AGY to `SPAWN_CAPABLE_PROVIDERS`; the `PROVIDER_CAPABILITIES`
row landed in 3.2 and is consumed here.

**Interactive-dispatch gate (record 1.1.17) — passed.** A spawned AGY is a tmux
terminal, and record 1.1.17 proved that `hooks.json` hooks dispatch from
interactive/tmux AGY: all five events fired with per-event key sets identical to print
mode, for a built-in tool, a shell command, and an MCP call. The deferral branch is
closed; this deliverable proceeds. The `source=agy` hooks.log line and the session row
for a spawned terminal are produced by §4.1's synthetic `SESSION_START` and are
asserted here (6.1.13) against the real adapter route. Two interactive negatives from
the same record bind the spawn lifecycle: `Stop` never fires on `C-c`, `esc`, or
process exit (so spawn teardown cannot wait for a `Stop`), and the native quit is two
`C-c`s at idle (`press ctrl+c again to exit`), after which shell children started by
the agent keep running — the tmux spawner's kill path terminates the pane's process
group, never just the CLI.

Spawn is an executable entry point that bypasses the capability registry — explicit
provider selection, inherited provider, and agent-configured provider all reach
`execute_spawn` without touching 6.2's metadata gate. `execute_spawn` therefore reads the
immutable 2.5 support record **before any side effect** — sandbox preparation, session
creation, terminal allocation, process start. Sub-floor, absent-binary, unparseable, and
pre-publication records refuse the spawn with the record's actionable upgrade message.

Pass `--dangerously-skip-permissions` under SRT, matching every other provider, and pin AGY's
own `--sandbox` off when SRT is enforcing, per the nesting precedent — both flags in the
exact forms recorded by 1.1.7. Wire project cwd,
parent/child session linkage via `ChildSessionConfig`, workflow variables through
`SessionVariableManager.merge_variables`, and the terminal env vars from
`get_terminal_env_vars`.

Apply the cwd remedy record 1.1.3 chose: pass **`--add-dir <project cwd>`** on every
launch. Without it an unregistered cwd yields `workspacePaths: []`, binds the
conversation to `default-cli-project` under AGY's application data, and `run_command`
carries no `Cwd`; with it `workspacePaths` is `[<cwd>]` and `Cwd` is injected into
every shell call. `--add-dir` is stateless (no AGY project file under the user's
config directory is written), unlike `--new-project`, which leaves one per launch. The
regression test pins `--add-dir` in the argv and `workspacePaths == [cwd]` on the
resulting hook payload.

Spawn support includes the watchdog lifecycle, not just launch.
`KNOWN_WATCHDOG_PROVIDERS` (`watchdog/models.py:17`) is `{claude, codex, droid, grok,
qwen}`, the per-provider readers live in sibling modules (`watchdog/claude.py` through
`watchdog/qwen.py`), and the registry's import-time guard requires the reader map to
match the provider set exactly — so a spawned AGY session would be classified `unknown`
and excluded from transcript-completion and recovery diagnostics. Add an AGY watchdog
reader in the new module `watchdog/agy.py`, built on the Gate 0 transcript shapes 4.2
parses, and register the provider set and reader map together.

Spawn support also includes daemon-restart recovery. `SUPPORTED_RESUME_PROVIDERS`
(`agents/resume_executor.py:46`) is a closed five-provider frozenset, so restart
reconciliation classifies every spawned AGY run as `resume_unsupported_provider` and a
daemon restart breaks the spawned-agent lifecycle this section claims. Record 1.1.1
proved conversation resume on 1.1.18 (`--conversation <id>` continues the same
conversation with `num_turns` advancing, including after SIGINT/SIGTERM), so AGY joins
`SUPPORTED_RESUME_PROVIDERS` with support-record-gated recovery using the recorded
argv (`--conversation <id> --add-dir <cwd>` plus the 1.1.7 flags), cwd, and the
preserved native conversation id; the deferral branch is closed.

Spawn parity also reaches terminal plan control. No incumbent passes a plan flag at
spawn (`build_cli_command` has none); terminal plan approval is keystroke-driven via
`DEFAULT_PLAN_KEYSTROKES` (`adapters/plan_keystrokes.py`, menu matchers in
`_register_builtin_plan_keystrokes`), and its all-CLIs coverage test enumerates exactly
the five incumbent managed CLIs, so a spawned AGY session would have no plan-menu
contract for native plan approval. Records 1.1.14 (menu + keystrokes) and 1.1.23
(`--mode` headless vs terminal) decided it: **the menu exists** and the AGY row is
keystroke-driven with exactly the recorded sequences. Mode cycling is `shift+tab`
(default → `accept-edits` → `plan`, shown as the status-line label). In plan mode AGY
writes the plan as an artifact (`brain/<id>/<name>.md`) with no inline approve/reject;
the review surface is opened with `ctrl+r` (or `/artifact`) — an "Action required"
list where `↑`/`↓` select, `y` approves, `n` rejects, `shift+a` approves all, `p`
previews, `ctrl+g` opens the editor, `esc` closes — and approving submits
`[Approved] <artifact>` as a user turn while staying in plan mode. The agy
`DEFAULT_PLAN_KEYSTROKES` row therefore maps approve → `C-r` then `y`, reject → `C-r`
then `n`, with the matcher anchored on the `Action required` header, plus end-to-end
dispatch tests; `handle_attached_plan_approval`
(`servers/websocket/handlers/plan_approval.py`) dispatches the same executable row.
The native permission prompt (only when `--dangerously-skip-permissions` is absent,
which spawn never does) is `1` yes / `2` always this conversation / `3` persist to
settings / `4` no / `esc` cancel. `--mode` is **not** added to the spawn argv: 1.1.23
showed `--mode plan` is additive in the terminal (same `shift+tab` surface), but the
incumbents pass no plan flag at spawn and the keystroke row already reaches plan mode.

Spawn health detection is copied config today. `src/gobby/install/shared/detection/agy.toml`
was copied from `claude.toml`; its `status_bar` rule
`(?i)(?:Opus|Sonnet|Haiku|bypass permissions|^\s*[⎇𖠰]|^\s*/|^\s*[─━▪▫]+)` matches Claude
Code's footer, so `IdleDetector.detect` (`src/gobby/agents/idle_detector.py`) would
misclassify an AGY pane. The AGY `status_bar`, `idle_prompt`, and `stalled_input` rules
are rewritten from the pane captures taken during the 1.1.17 interactive probe (prompt glyph `>` alone on a line between two horizontal rules; status line `? for shortcuts … <model label>`; working state shows a spinner line and the running tool; the interrupted state reads `Interrupted · What should Antigravity CLI do instead?`), with
idle-detector cases mirroring `TestStatusBarFiltering` in `tests/agents/test_idle_detector.py`;
the bundled-manifest contract test
(`tests/agents/detection/test_agents_detection_registry.py`) and the committed
bundled-content manifest parity test (`tests/test_build_backend.py`) re-run.

Spawn parity also reaches the closed authentication inventories. `CLI_ENV_ALLOWLIST`
and `CLI_CREDENTIAL_KEYS` (`agents/spawners/auth_env.py`) and `_SUPPORTED_AUTH_CLIS`
(`agents/tmux/spawner.py:36`) enumerate only the five incumbents, so a spawned AGY
session's credentials could be omitted, leaked from ambient environment, or
misclassified by generic inference. The agy rows carry exactly the credential env
vars and auth shape recorded by the authentication-footprint probe (record 1.1.15):
the credential is a macOS login Keychain item (`svce=gemini`, `acct=antigravity`)
gated on state under the real `~/.gemini/antigravity-cli/`, `env -i HOME=$HOME PATH=…`
authenticates, and the CLI ignores `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and
`GOOGLE_APPLICATION_CREDENTIALS`. So the allowlist and credential maps gain an
explicit **empty** agy row, a test proves those three ambient variables are stripped
from the spawned environment, and `_SUPPORTED_AUTH_CLIS` does not admit agy (no
in-scope caller needs auth-CLI inference; under a foreign `HOME` a headless turn prints
the OAuth URL and exits 1 after the 60 s auth timeout, an interactive launch stops at
the `You are currently not signed in` login-method menu, and `models` exits 1 at once
with `Please sign in` — records 1.1.15/1.1.20 — which the spawner surfaces as a launch
failure). The proof lives at the process boundary, not the helper:
`make_spawn_env` (`agents/spawners/base.py`) copies `os.environ` and tmux
children inherit the daemon environment, so helper-level allowlist tests cannot
establish runtime absence. Terminal stripping and sandbox credential masking
(`_PROVIDER_CREDENTIAL_ENV`, 3.2.4) consume one normalized provider/key
inventory derived from the 1.1.15 record, and a live spawned child under a
seeded ambient environment asserts the denied variables are absent while
allowed non-secret variables remain.

Check the projected line count: `spawn_executor.py` is 782 lines and each existing spawner is
roughly 100. If it projects at or above 1,000, load the `decompose-monolith` skill and
decompose within this task.

**Acceptance:**

- 6.1.1 - The AGY spawn rejection is removed and `_spawn_agy_terminal` exists. symbol: `execute_spawn`. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.2 - `build_cli_command` produces AGY argv. symbol: `build_cli_command`. file: `src/gobby/agents/spawners/command_builder.py`.
- 6.1.3 - AGY's own `--sandbox` is pinned off when SRT enforces. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.4 - A spawned AGY agent receives the intended project cwd through `--add-dir <cwd>` on every launch (record 1.1.3), and a regression test pins the argv and `workspacePaths == [cwd]` on the resulting hook payload. test: `tests/agents/test_spawn_executor.py`.
- 6.1.5 - Parent/child linkage and workflow variables are wired. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.6 - `spawn_executor.py` remains below 1,000 lines, or is decomposed via the `decompose-monolith` skill in the same task. file: `src/gobby/agents/spawn_executor.py`.
- 6.1.7 - `execute_spawn` reads the 2.5 support record before any side effect; sub-floor, absent-binary, unparseable, and pre-publication records refuse the spawn with the actionable upgrade message, across explicit, inherited, agent-configured, and default provider selection. test: `tests/mcp_proxy/tools/spawn_agent/test_provider_resolution.py`.
- 6.1.8 - AGY argv construction is pinned in the builder suite, including the 1.1.7-recorded flag forms. test: `tests/agents/spawners/test_command_builder.py`.
- 6.1.9 - A spawned AGY session is watchdog-covered: `KNOWN_WATCHDOG_PROVIDERS` and the reader map gain the agy entry together, the AGY reader classifies completion from the Gate 0 transcript shapes, and registry parity tests pin the row. test: `tests/agents/watchdog/test_agents_watchdog_registry.py`.
- 6.1.10 - Daemon-restart recovery follows the 1.1.1 record (resume proven on 1.1.18): a spawned AGY run resumes through `SUPPORTED_RESUME_PROVIDERS` with the recorded argv (`--conversation <id> --add-dir <cwd>` plus the 1.1.7 flags) and preserved conversation id — never a `resume_unsupported_provider` classification. test: `tests/agents/test_resume_executor.py`.
- 6.1.11 - Terminal plan control follows the 1.1.14 record (menu proven): the executable `DEFAULT_PLAN_KEYSTROKES` agy row maps approve → `C-r`,`y` and reject → `C-r`,`n` with the matcher anchored on the `Action required` header, the all-CLIs coverage guard enumerates agy, and both `NativePlanActionService` and `handle_attached_plan_approval` dispatch that same row with end-to-end dispatch tests — never an invented sequence or an unread registry row. test: `tests/adapters/test_plan_keystrokes.py`.
- 6.1.12 - The closed auth inventories gain their AGY classification from the 1.1.15 record (Keychain-only auth, no env var accepted): `CLI_ENV_ALLOWLIST` and `CLI_CREDENTIAL_KEYS` carry an explicit empty agy row with a test stripping ambient `GOOGLE_API_KEY`/`GEMINI_API_KEY`/`GOOGLE_APPLICATION_CREDENTIALS`, and `_SUPPORTED_AUTH_CLIS` does not admit agy; terminal stripping and sandbox masking share one normalized provider/key inventory, and a live spawned child under a seeded ambient environment proves denied variables are absent from the process while allowed variables remain. test: `tests/agents/spawners/test_auth_env.py`.
- 6.1.13 - Interactive dispatch is proven (record 1.1.17), so a tmux-spawned AGY session driven through the real adapter route produces `source=agy` hook dispatches for all five events and a session row, the spawner's kill path terminates the pane's process group (shell children outlive the CLI and `Stop` never fires on exit), and teardown never waits for a `Stop` event. test: `tests/agents/test_spawn_executor.py`.
- 6.1.14 - Plan control follows 1.1.14/1.1.23 in the keystroke-driven form: `build_cli_command` adds no `--mode` flag for agy, pinned by an argv case. test: `tests/agents/spawners/test_command_builder.py`.
- 6.1.15 - `agy.toml` `status_bar`/`idle_prompt`/`stalled_input` rules are captured from the live 1.1.18 pane captures of record 1.1.17, `IdleDetector.detect` classifies an AGY idle pane as `idle` and a working pane as `active` with the Claude footer regex removed, the bundled-manifest contract test still passes, and the committed bundled-content manifest matches. test: `tests/agents/test_idle_detector.py`.

### 6.2 Gate AGY capabilities on version 1.1.18 [category: code] (depends: 6.1, 5.3, 2.5)
`kind: deliverable`

Targets:
- `src/gobby/ai/registry_builder.py::_agy_unavailable_bindings`
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `src/gobby/ai/registry_builder.py::_tool_chat_binding`
- `src/gobby/ai/_tool_chat_builder.py::_daemon_tool_chat_adapter_factories`
- `src/gobby/ai/_tool_chat_service.py::ToolChatService`
- `src/gobby/ai/_tool_chat_agy.py`
- `src/gobby/providers/registry.py::*` — scope-reason: the module-level provider table entry for AGY and the AGY_UNAVAILABLE_REASON constant are module data rather than indexed symbols
- `tests/ai/test_tool_chat_service.py::*` — scope-reason: service fixtures and factories migrate from style-keyed to provider-aware identity
- `tests/ai/test_agy_tool_chat_contract.py`
- `web/src/lib/providerModels.ts::isHiddenProvider`
- `web/src/lib/providerModels.ts::fetchProviderModelCatalog`
- `web/src/components/activity/useSessionProviderOptions.ts::*` — scope-reason: sessions-filter provider list drops the agy exclusion
- `web/src/components/chat/useChatPageProviderState.ts::*` — scope-reason: availableProviders filter relies on server availability
- `web/src/components/chat/ProviderPicker.tsx::*` — scope-reason: picker keeps the shared helper but agy is no longer a hidden value
- `web/src/lib/__tests__/providerModels.test.ts::*` — scope-reason: `isHiddenProvider("agy")` expectations invert
- `web/src/components/activity/__tests__/SessionsTab.test.tsx::*` — scope-reason: the sessions-filter test gains agy back
- `src/gobby/ai/_text_generation_adapters.py::AgyCLITextGenerateAdapter`
- `src/gobby/ai/_text_generation_adapters.py::_validate_agy_stdout`
- `tests/ai/test_text_generation.py::*` — scope-reason: `test_agy_cli_text_generate_adapter_rejects_empty_or_error_stdout` is re-pointed from the `Error:` stdout heuristic to the nonzero-exit/stderr case
- `tests/ai/test_agy_probe.py::*` — scope-reason: the module docstring's live opt-in reference moves from the deleted test_provider_models.py to the 6.3 collector test
- `tests/providers/test_providers_registry.py::*` — scope-reason: the registry table test gains the flipped agy flags
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: agy availability/supports_web_chat assertions follow the support record

Flip `ProviderMetadata("agy")` to `supports_web_chat=True`, `supports_agent_spawn=True`,
`live_model_discovery=True` and drop `AGY_UNAVAILABLE_REASON` from it. Replace
`_agy_unavailable_bindings` with real `WEB_CHAT` and `AGENT_SPAWN` bindings, and add a
`TOOL_CHAT` binding — but **not** through the current factory map.
`_daemon_tool_chat_adapter_factories` resolves `AIAdapterStyle.CLI` globally to
`DroidSpawnToolChatAdapter` (`_tool_chat_builder.py:70`), so binding AGY as bare CLI style
would hand AGY prompts to Droid's command and JSON-RPC protocol. Make the CLI-style factory
provider-aware and add a dedicated `AgyToolChatAdapter` in the new module
`src/gobby/ai/_tool_chat_agy.py`, speaking the 5.1/5.2 stream-json transport and
implementing the full ToolRuntime contract: controlled tools, `ToolLoopLimits`, timeouts,
cancellation, and normalized results. On 1.1.18 a controlled-tool bridge is
feasible without a bespoke protocol: the persistent process (`--input-format stream-json`),
`--json-schema` for structured final output, and MCP registration (Gobby's own MCP server
is already registered with agy — `agy mcp list` shows it, from the installer's
`configure_mcp_server_json` write) let Gobby expose controlled tools as MCP tools and observe/deny calls through the native
`PreToolUse` hook. Record 1.1.12 decided it: **supported.** The recorded transport is the
`PreToolUse` hook answering `{"decision":"deny","reason":…}` — the denied call becomes a
`tool` step in `ERROR` state with `tool_info.error {type: TOOL_ERROR, message: "tool call
denied by pre-tool hook: <reason>"}`, no `PostToolUse` fires, the model sees the reason
and continues, and the turn's `result` carries `status: ERROR` with that message while
the process exits 0. Every MCP call surfaces as the single built-in
`call_mcp_tool{ServerName, ToolName, Arguments}`, so the controlled-tool set is bounded by
matching `toolCall.name == "call_mcp_tool"` plus `args.ServerName`/`args.ToolName` in the
hook payload — never by a tool name alone. `AgyToolChatAdapter` implements exactly that
with the full ToolRuntime contract; the process must carry `--dangerously-skip-permissions`
(record 1.1.7: otherwise every tool call auto-denies headless and no hook `allow` can
override it) and `--add-dir` (1.1.3). A denied controlled tool is observed by the adapter
from the stream (`ERROR` step + `result.error`), never from `PostToolUse`, which is
absent for failed tools.

The web UI hides AGY independently of the registry. Commit ca1ea53474 (#20049) added
`HIDDEN_PROVIDERS = {"agy"}` / `isHiddenProvider` in `web/src/lib/providerModels.ts` and
filtered `fetchProviderModelCatalog` (spawn form, chat model picker, reasoning
preferences, Settings providers), `useSessionProviderOptions.ts`,
`useChatPageProviderState.ts`, and `ProviderPicker.tsx`. Un-hiding lands **here**, in the
same change as the metadata flip, by emptying `HIDDEN_PROVIDERS` (the helper and its
callers stay); availability then flows from `/api/providers` `available`/`supports_web_chat`,
so sub-floor installs stay un-offered through `available=false`.

`AgyCLITextGenerateAdapter` (`src/gobby/ai/_text_generation_adapters.py`) is re-proven
against the floor here: `_validate_agy_stdout` treats stdout beginning `Error:` as failure,
contradicting record 1.1.13 (exit 1, stderr). Failure is decided by exit status and stderr;
the `Error:` prefix heuristic is removed, empty-stdout rejection stays, and
`test_agy_cli_text_generate_adapter_rejects_empty_or_error_stdout` in
`tests/ai/test_text_generation.py` is re-pointed to a nonzero-exit/stderr case.
`tests/ai/test_agy_probe.py`'s docstring references the deleted
`tests/servers/test_provider_models.py`; it is re-pointed to the 6.3 collector test's live
opt-in. `tests/servers/routes/test_servers_routes_providers.py` asserts
`providers["agy"]["available"] is False` and `supports_web_chat is False` — those cases
flip to the support-record outcome here.

The factory map is not the whole seam. `ToolChatService._adapter_for_style` caches
constructed adapters keyed solely by `AIAdapterStyle` from zero-argument factories
(`_tool_chat_service.py:240-252`), so a provider-aware factory alone still cross-routes on
first use: whichever CLI-style adapter is constructed first — Droid's or AGY's — is
returned for the other provider from then on. Adapter selection and the cache become
provider-aware: the resolved `CapabilityBinding` flows into selection and the cache is
keyed by `(adapter_style, provider)` — with both first-use orders proven in one service
instance. That identity change lands in the incumbent unit-test seam, not only the new
contract test: `tests/ai/test_tool_chat_service.py` builds every adapter map and factory
keyed solely by `AIAdapterStyle`, so its fixtures migrate to the provider-aware identity,
its non-CLI cases keep passing unchanged, and both same-instance cache-order regressions
— Droid then AGY, and AGY then Droid — live in that suite.

Gate all of it on the immutable support record from 2.5. AGY is the **first version-gated
provider CLI**, so this establishes the pattern: below the floor, capabilities stay
unavailable with the record's upgrade message naming the installed and required versions.
Registry build reads the record; it never probes.

`VISION_EXTRACT` follows 1.1.4, which is negative: AGY has no image-input flag, `@path`
mentions are plain text, and stream-input `{"type":"image"}` content blocks are rejected
(`only "text"`). The only path to an image is the model's own `view_file` on a PNG path the
prompt names, which is not an input attachment Gobby can supply. `VISION_EXTRACT` stays
unavailable with the narrow reason "AGY accepts no image input; vision requires the model
to open a file path itself" — not the current blanket "no documented machine transport"
text.

Advertisement alone is unverifiable, so one executable contract test proves the wiring:
resolve the `TOOL_CHAT` binding for a supported version and drive a scrubbed fake AGY
subprocess through init, text delta, tool lifecycle and result records, asserting prompt,
model and effort argv propagation, normalized output, non-zero-exit handling, and that
versions below 1.1.18 never advertise the binding.

**Acceptance:**

- 6.2.1 - Installed AGY 1.1.18 advertises web chat, agent spawn, and tool chat; tool chat uses the record-1.1.12 transport (`PreToolUse` deny on `call_mcp_tool` matched by `ServerName`/`ToolName`, denial observed from the stream) — never Droid-routed. symbol: `_agy_unavailable_bindings`. file: `src/gobby/ai/registry_builder.py`.
- 6.2.2 - AGY below 1.1.18 stays unavailable with a message naming installed and required versions. symbol: `ProviderMetadata`. file: `src/gobby/providers/registry.py`.
- 6.2.3 - `AGY_UNAVAILABLE_REASON` no longer gates a capable installation. file: `src/gobby/providers/registry.py`.
- 6.2.4 - `VISION_EXTRACT` stays unavailable per the negative 1.1.4 finding, with the narrow no-image-input reason. symbol: `_agy_unavailable_bindings`. file: `src/gobby/ai/registry_builder.py`.
- 6.2.5 - A registry-to-transport contract test drives the `TOOL_CHAT` binding end-to-end against a fake AGY subprocess, and sub-1.1.18 never advertises it. test: `tests/ai/test_agy_tool_chat_contract.py`.
- 6.2.6 - The AGY `TOOL_CHAT` binding resolves to `AgyToolChatAdapter` — never `DroidSpawnToolChatAdapter` — with controlled tools, `ToolLoopLimits`, timeouts and cancellation enforced. symbol: `_daemon_tool_chat_adapter_factories`. file: `src/gobby/ai/_tool_chat_builder.py`.
- 6.2.7 - Adapter selection and caching are provider-aware: the cache is keyed by adapter style plus provider, both first-use orders — Droid then AGY, and AGY then Droid — resolve the correct adapter in one service instance, and the incumbent service suite migrates to provider-aware fixtures with its non-CLI cases preserved. symbol: `ToolChatService`. file: `src/gobby/ai/_tool_chat_service.py`.
- 6.2.8 - `HIDDEN_PROVIDERS` no longer contains `agy`; `isHiddenProvider("agy")` is false; the spawn form, chat model picker, reasoning preferences, Settings providers, sessions filter, and `ProviderPicker` offer AGY when `/api/providers` reports `available: true`, and do not when it reports `available: false`. test: `web/src/lib/__tests__/providerModels.test.ts`.
- 6.2.9 - `AgyCLITextGenerateAdapter` failure is decided by nonzero exit plus stderr per record 1.1.13; an `Error:`-prefixed stdout with exit 0 is returned as text, empty stdout still raises, and the renamed test pins the nonzero-exit path. symbol: `_validate_agy_stdout`. file: `src/gobby/ai/_text_generation_adapters.py`.
- 6.2.10 - `/api/providers` and `/api/providers/models` report agy `available`/`supports_web_chat`/`supports_agent_spawn` from the 2.5 support record, with the sub-floor case naming installed and required versions. test: `tests/servers/routes/test_servers_routes_providers.py`.

### 6.3 Move the AGY model catalog to live discovery [category: code] (depends: 6.2)
`kind: deliverable`

Targets:
- `src/gobby/providers/capabilities/collectors/agy.py`
- `src/gobby/providers/capabilities/refresh.py::_default_collectors`
- `src/gobby/providers/capabilities/seed.py::*` — scope-reason: a bundled AGY cold-start snapshot generated from the record-1.1.20 fixture joins the claude/droid seeds as module-level data
- `src/gobby/servers/routes/providers.py::_agy_snapshot_payload`
- `src/gobby/servers/routes/providers.py::list_provider_models`
- `src/gobby/servers/provider_model_defaults.py::*` — scope-reason: the static AGY effort/alias table is module-level data refreshed to the 1.1.18 set and its five `availability_source` labels are retired
- `src/gobby/ai/_agy_models.py::*` — scope-reason: effort maps, defaults, and alias tables refresh together to the floor-version model set
- `src/gobby/ai/registry_builder.py::_tool_chat_adapter_style`
- `src/gobby/ai/registry_builder.py::_agy_unavailable_bindings`
- `src/gobby/sessions/context_usage.py::_context_window_for_agy_model`
- `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py`
- `tests/providers/capabilities/test_providers_capabilities_refresh.py::*` — scope-reason: collector-registration and failure-retains-prior-snapshot cases gain the agy row
- `tests/providers/capabilities/test_seed.py::*` — scope-reason: the bundled AGY seed joins the seed assertions
- `tests/ai/test_capability_registry.py::test_daemon_registry_reports_text_generate_provider_bindings`
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: the static agy snapshot assertions become live/bundled source transitions
- `tests/ai/test_text_generation.py::*` — scope-reason: default-effort normalization cases pin the fixture-recorded AGY efforts through the text-generation consumer
- `tests/fixtures/provider_contracts/agy/command-captures.json::*` — scope-reason: the record-1.1.20 `models` and `/model` entries are read as the collector fixture and the live drift baseline

Gobby has **no AGY discovery seam at all**. The legacy `ProviderModelCatalog` was retired
(c49f706479, #19632); live model facts now come from per-provider collectors in
`src/gobby/providers/capabilities/collectors/` run by `CapabilityRefreshCoordinator`
(`refresh.py`), stored via `ProviderCapabilityStore`, seeded on an empty database by
`seed.py` (bundled cold-start rows with `stale` health and `bundled` provenance), and
served by `list_provider_models` in `src/gobby/servers/routes/providers.py`.
`_default_collectors` registers five collectors; `list_provider_models` special-cases
`name == "agy"` to a static `_agy_snapshot_payload()` built from `AGY_MODELS`;
`provider_model_discovery.py` has zero AGY references and `get_cli_version` has zero
callers. The static table is labelled `agy-1.0.10-static` at eight sites
(`registry_builder.py` ×2; `provider_model_defaults.py` ×5;
`tests/ai/test_capability_registry.py`) and `tests/servers/routes/test_servers_routes_providers.py`
pins the static `refresh.sources == [{"source_key":"static","state":"ok"}]` shape.
Record 1.1.20 **disproved the flag placement**: `agy models --output-format json` exits 1
with `flags provided but not defined`; the global flag precedes the subcommand —
`agy --output-format json models` — and the JSON is the generic command envelope
`{conversation_id:"", status:"SUCCESS", response:"<TSV>", …, command:{name:"models",
data:{models:[{id, label}]}}}` (with `--output-format stream-json` a single
`{"event":"command_result","command":{…}}` line). The list carries **no default marker,
no family, no effort field, and no context window**: effort is the id suffix
(`-high`/`-medium`/`-low`, absent on `claude-sonnet-4-6`, `claude-opus-4-6-thinking`),
and the current model comes from `-p "/model"` → `command.data {id, label, effort,
is_default}`. The 1.1.18 list has 14 entries: `gemini-3.7-flash-{high,medium,low}`,
`gemini-3.6-flash-{high,medium,low}`, `gemini-3.5-flash-{high,medium,low}`,
`gemini-3.1-pro-{high,low}`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`,
`gpt-oss-120b-medium`. The unauthenticated exit **is** recorded (record 1.1.20,
`command-captures.json` entries `1.1.20 unauthenticated …`, `evidence/1.1.20-print-models.txt`;
probed under `HOME=$(mktemp -d)` so the real Keychain item and `~/.gemini` stay
untouched): `agy --output-format json models`, its `stream-json` twin, and bare
`agy models` all exit 1 **immediately** with **empty stdout** — no command envelope, no
OAuth prompt, no wait — and stderr `Error: Please sign in to view available models.
Launch the CLI without arguments to sign in.` (the `Fetching available models...`
progress line precedes it). Only an agent *turn* or `/usage` under a foreign `HOME`
prints the OAuth URL and fails after the 60 s auth timeout with the
`status: ERROR, error: "authentication failed or timed out"` envelope (record 1.1.15);
the collector never sees that shape. So the collector's unauthenticated branch keys on
**exit 1 + empty stdout** (stderr carries the reason for the recorded failure), not on
an envelope parse.

Add `AgyCollector` in the new module `src/gobby/providers/capabilities/collectors/agy.py`
(`provider = "agy"`, one `SourceSpec` keyed `agy_models_cli`), modelled on
`GrokCollector.collect` / `DroidCollector.collect`: it runs `agy --output-format json models`
under the 30 s source timeout, parses the 1.1.20 envelope (`command.data.models[].{id,
label}`) into `ModelCapability`/`ModelRoute` rows — family and effort derived from the id
(`<family>-<effort>` suffix split; no suffix means a single fixed effort), label from
`label`, context window from the bundled family table since the CLI reports none — with
per-field `FactProvenance` (`agy_models_cli` for id/label/effort, `bundled` for the
window), and raises a typed `AgySourceError` on absent binary, sub-floor
version (read from the 2.5 support record, never probed here), nonzero exit — including
the unauthenticated form: exit 1, empty stdout, stderr `Please sign in` (record 1.1.20),
surfaced as an `unauthenticated` source failure so the UI can say so — or shape
mismatch so the coordinator records a source failure and keeps the prior snapshot.
Register it in `_default_collectors`. Replace the static fallback with a bundled AGY seed
in `seed.py` generated from the 1.1.20 fixture, and delete `_agy_snapshot_payload` plus
the `name == "agy"` branch in `list_provider_models` so AGY flows through
`_matrix_snapshot_payload` like every other provider. `AGY_MODELS` in
`provider_model_defaults.py` remains only as the effort/alias table consumed by
`_agy_models.py` and `registry_builder.py`, refreshed to the 1.1.18 set;
`model_catalog_source`/`availability_source` labels become the live `source_key` when a
snapshot exists and `bundled` otherwise — the `agy-1.0.10-static` string is gone from all
eight sites, with a repository-wide absence assertion.

Staleness is a store-health concern, not a cache API:
`CapabilityRefreshCoordinator._refresh_provider` keeps the prior snapshot on failure and
records the source failure, so a bundled AGY seed survives a failed live refresh with
`stale`/`error` source state visible through `refresh.sources[]`. Compatibility with the
2.5 support record is enforced in the collector (sub-floor → typed failure, never a
"live" snapshot) and in the registry (bindings read the record). No `load_cache` exists;
do not invent one.

The fixture is the Gate 0 capture: the `agy --output-format json models` entry (and its
`stream-json` twin and the `-p "/model"` entry) in
`tests/fixtures/provider_contracts/agy/command-captures.json`, recorded on 1.1.18
(record 1.1.20); the 1.0.10 text fixture is already deleted. The live opt-in drift check
(`GOBBY_RUN_AGY_MODELS_LIVE=1`) lives in the new collector test so it exercises the
installed binary against that fixture entry rather than skipping. `_context_window_for_agy_model`
(`src/gobby/sessions/context_usage.py`) resolves AGY windows through
`resolve_context_window(provider="agy", db=…)` from the store — the collector's
context-window facts are what it reads.

Also reconcile the `GEMINI_FAMILY_MODELS` vs `AGY_MODELS` default-effort mismatch for
`gemini-3.5-flash` (`medium` vs `low`). Record 1.1.20 shows the binary reports **no**
default effort on the list (`/model` reports only the currently selected model, with
`is_default: false` for the probed `gemini-3.5-flash-high`), so the named default is
`medium`, matching the gemini family table. #19483 tracks the broader default-effort audit across providers;
this plan closes only the `gemini-3.5-flash` mismatch.

**Acceptance:**

- 6.3.1 - `agy --output-format json models` output (flag before subcommand, per record 1.1.20) is parsed by the collector at capability refresh, with the bundled seed as the cold-start fallback. symbol: `AgyCollector.collect`. file: `src/gobby/providers/capabilities/collectors/agy.py`.
- 6.3.2 - The `agy-1.0.10-static` label is removed from all eight sites across `registry_builder.py`, `provider_model_defaults.py`, and `test_capability_registry.py`, with a repository-wide absence assertion for the retired label. file: `src/gobby/servers/provider_model_defaults.py`.
- 6.3.3 - The model fixture is the record-1.1.20 `agy --output-format json models` entry captured on 1.1.18, and the collector test parses exactly that entry's 14 `{id,label}` rows. file: `tests/fixtures/provider_contracts/agy/command-captures.json`.
- 6.3.4 - The drift test exercises the installed binary instead of skipping off-version. test: `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py`.
- 6.3.5 - Focused tests cover supported live discovery, sub-floor typed failure, command-failure prior-snapshot retention, and the source/availability transitions through the provider routes. test: `tests/servers/routes/test_servers_routes_providers.py`.
- 6.3.6 - `GEMINI_FAMILY_MODELS` and `AGY_MODELS` agree on the canonical `gemini-3.5-flash` default effort, with a parity test pinning both consumers to the fixture-recorded value. test: `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py`.
- 6.3.7 - A failed live refresh retains the prior (bundled or live) AGY snapshot with the source failure recorded, and a sub-floor support record yields a typed source failure rather than a live snapshot, proven by a test seeding the bundled snapshot before a failed live refresh. test: `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py`.
- 6.3.8 - Default-effort normalization through the text-generation consumer matches the fixture-recorded AGY efforts. test: `tests/ai/test_text_generation.py`.
- 6.3.9 - `AgyCollector` is registered in `_default_collectors`, parses the 1.1.20 envelope (`command.data.models[].{id,label}`, effort from the id suffix) from the fixture into a valid `ProviderSnapshot` (passes `validate_snapshot`), and raises a typed source error on absent binary, sub-floor support record, nonzero exit (including the record-1.1.20 unauthenticated form: exit 1, empty stdout, stderr `Please sign in`, no OAuth prompt), or shape mismatch, with the coordinator retaining the prior snapshot and recording the failure. test: `tests/providers/capabilities/collectors/test_providers_capabilities_collectors_agy.py`.
- 6.3.10 - `_agy_snapshot_payload` and the `name == "agy"` branch are deleted from `list_provider_models`; AGY is served through `_matrix_snapshot_payload` with `refresh.sources[].source_key == "agy_models_cli"` when live and `bundled` when seeded. symbol: `list_provider_models`. file: `src/gobby/servers/routes/providers.py`.
- 6.3.11 - `_context_window_for_agy_model` resolves a Gemini-family AGY window from the collector-supplied store fact, proven with a seeded store. symbol: `_context_window_for_agy_model`. file: `src/gobby/sessions/context_usage.py`.

### 6.4 AGY usage-capacity reporting [category: code] (depends: 2.5, 5.1, 6.3)
`kind: deliverable`

Targets:
- `src/gobby/providers/usage.py`
- `src/gobby/servers/routes/providers.py::create_providers_router`
- `tests/providers/test_usage.py`
- `tests/servers/routes/test_servers_routes_providers.py::*` — scope-reason: the new usage route gains AGY supported and non-AGY unsupported cases
- `tests/fixtures/provider_contracts/agy/command-captures.json::*` — scope-reason: the record-1.1.19 healthy and exhausted `/usage` entries are the reporter's parse fixtures

Folds task #19364. No per-provider usage/capacity surface exists today — only
Gobby-internal token accounting (`GET /api/sessions/usage` →
`SessionTokenTracker.get_usage_summary`; the MCP metrics tool). Since 1.1.11,
`agy -p "/usage" --output-format json` answers without an agent turn or quota spend
(`num_turns: 0`, zero `usage`); record 1.1.19 captured the shapes on 1.1.18: `/usage` →
`command.data.{description, groups[].{name, description, buckets[].{id, name,
description, window, remaining_fraction, reset_time}}}` (two groups, `gemini-weekly` and
the Claude/GPT bucket, `window: "weekly"`), `/quota` is an alias that returns
`command.name: "usage"` with the same data, and `/credits` exits 1 with `"/credits
failed: retrieving credits: no credits info found"` on this account. The exhausted
state is `remaining_fraction: 0` with a "You have hit your weekly limit…" description,
and a turn attempted in that state returns `result{status:ERROR, error:"Individual
quota reached …"}` with exit 1.

Add `src/gobby/providers/usage.py` with a frozen `ProviderUsageSnapshot` dataclass
(`provider`, `observed_at`, `supported: bool`, `windows: tuple[UsageWindow, ...]` where
`UsageWindow` has `label`, `used`, `limit`, `unit`, `resets_at | None`, plus `raw: dict`),
a `ProviderUsageReporter` protocol with `async def report() -> ProviderUsageSnapshot`,
and one implementation `AgyUsageReporter` that runs `/usage` only (`/quota` is an
alias and `/credits` is a negative contract — a nonzero exit that must not mark the
whole snapshot unsupported) with a 15 s timeout under the 2.5 support record
(sub-floor/absent → `supported=False` with the record's reason; nonzero exit →
`supported=False` with the envelope's `error` or stderr detail), maps each bucket to a
`UsageWindow` (`label` = group name + bucket name, `used` = `1 - remaining_fraction`,
`limit` = `1`, `unit` = `"fraction"`, `resets_at` = `reset_time`), and caches the
snapshot for 60 s. Expose `GET /api/providers/{provider}/usage` in
`create_providers_router`: AGY returns the snapshot; every other provider returns
`{"provider": name, "supported": false, "reason": "no usage reporter"}` with 200. No web
UI work in this plan. No agent turn is ever started by this path. The fixture is the record-1.1.19 `/usage`
entries (healthy and exhausted) in
`tests/fixtures/provider_contracts/agy/command-captures.json` (numbers kept, no
account identifiers appear in the shape).

**Acceptance:**

- 6.4.1 - `AgyUsageReporter.report` parses the 1.1.19-recorded `/usage` envelope (healthy and exhausted fixtures) into one `ProviderUsageSnapshot` with one `UsageWindow` per bucket, the argv pinned to `-p "/usage" --output-format json` and no agent-turn flags, and `/credits` is never run. test: `tests/providers/test_usage.py`.
- 6.4.2 - Sub-floor, absent-binary, nonzero-exit, and timeout outcomes yield `supported=False` with a truthful reason and never raise through the route. test: `tests/providers/test_usage.py`.
- 6.4.3 - `GET /api/providers/agy/usage` returns the snapshot and a second call within 60 s spawns no process; `GET /api/providers/claude/usage` returns `supported: false`. test: `tests/servers/routes/test_servers_routes_providers.py`.

## P7: Documentation
`kind: framing`

**Goal**: Correct the record only after the gates pass.

### 7.1 Update the CLI integration matrix and AGY docs [category: docs] (depends: P5, P6)
`kind: deliverable`

Targets:
- `docs/research/cli-integration-matrix.md`
- `docs/research/cli-integration-matrix-claude-code.md`
- `docs/guides/sandboxing.md`
- `docs/guides/sandbox-compatibility.md`
- `docs/guides/adapter-fidelity.md`
- `docs/guides/ghook-user-guide.md`
- `docs/guides/hook-schemas.md`
- `docs/guides/providers-and-models.md`
- `docs/guides/configuration.md`
- `docs/guides/telegram.md`
- `docs/guides/sessions.md`

Move AGY from **Blocked** to **FULL** — only after every preceding gate passes. Rewrite "the
agy trap" and "the agy lesson" sections: the claim that "upstream must add transcripts + ACP"
is now wrong on both counts. AGY persists parseable JSONL transcripts and exposes a
stream-json subprocess transport; ACP was never required. Correct the companion matrix, which
records transcripts as "binary protobuf, no parser" — that described 1.0.11 and is stale.

The matrix row is three readiness surfaces plus a status column, and each cell is named
explicitly: Hook, Transcript, Web-chat, and Status. Every cell reflects a Gate 0-proven
surface; a surface deferred under the Constraints branch rule keeps a truthful status
instead of FULL. The rewritten rows are: the matrix row
`| **agy / Antigravity** | Full (5 events: PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop; 1.1.18 floor) | JSONL (\`brain/<id>/.system_generated/logs/transcript_full.jsonl\`) | Custom stream-json (\`AgyWebChatBackend\`) | **FULL** |`
and the classification row
`| **Supported** | **agy / Antigravity** | 1.1.18+: hooks.json dispatch, JSONL transcripts, \`--input-format stream-json\` transport; no ACP required |`.

Seven stale AGY claims in the guides are corrected with exact replacements, each owned
here unless noted. `docs/guides/adapter-fidelity.md` carries two AGY rows (one says
`updatedInput`, the code emits `overwrite`; the other says "no public live hook contract
1.0.8"): they merge into one row stating the five PascalCase events, context via
`injectSteps`, `PreToolUse` decision ∈ `allow|deny|ask|force_ask|deny_unless_prior_grant`
with `overwrite` (never `updatedInput`), `deny_unless_prior_grant`, `terminationBehavior`,
and `injectSteps` `userMessage`/`ephemeralMessage` as the honored set per record 1.1.24 —
and `permissionOverrides` and `injectSteps.toolCall` as explicitly **not** honored — web
chat via custom stream-json, and spawn per 6.1 (interactive dispatch proven).
`docs/guides/ghook-user-guide.md` (`| agy | SessionStart |` and the
SessionStart/UserPromptSubmit fail-closed claim) and `docs/guides/hook-schemas.md` ("AGY
SessionStart") list the five events and 2.3's critical set — 2.3 owns the dead-event
correction in those two guides; 7.1 owns their final matrix/fidelity rewrite.
`docs/guides/providers-and-models.md` ("AGY retains static response rows pending #18653")
states the 6.3 collector/bundled-seed model source. `docs/guides/configuration.md` ("AGY
manages its own timeout contract") states the `hooks.json` per-hook timeout (template
45 s, `AGY_HOOK_TIMEOUT_SECONDS`) and its relation to `hooks.provider_timeout` (2.6).
`docs/guides/telegram.md` ("AGY has no native plan action") is corrected to the 1.1.14
keystroke contract (`ctrl+r` review, `y`/`n`). `docs/guides/sessions.md` (`| agy | AGY CLI hooks |`) reads "AGY CLI hooks or
web-chat AGY backend". `docs/guides/cli-commands.md` is not stale.

The sandbox guides are consumers of 3.1's default flip, not bystanders:
`docs/guides/sandboxing.md` states "Web chat keeps its provider-native sandbox" and
`docs/guides/sandbox-compatibility.md` records the provider-native web-chat default
and the shared ACP backend — all stale the moment 3.1 lands. Both guides are updated
to state the `backend="srt"` bounded-network web-chat default, session-owned process
lifetimes, the explicit provider-native override, and the resume invalidation a stale
policy hash produces. (2.3 separately adds the fail-open posture to `sandboxing.md`;
this deliverable owns the default-boundary statements.)

The wiki concept page (`wiki/knowledge/concepts/agy.md`) is gitignored generated output
(`/wiki/` in `.gitignore`) — it is not a plan target. It regenerates from the corrected
durable docs via the wiki pipeline after this deliverable lands; no direct edit is made
or committed.

**Acceptance:**

- 7.1.1 - The AGY row reads Hook=Full, Transcript=JSONL, Web-chat=custom stream-json, and Status=FULL — the three readiness surfaces plus the status column, each cell named. file: `docs/research/cli-integration-matrix.md`.
- 7.1.2 - The "upstream must add transcripts + ACP" framing is corrected. behavior: "AGY exposes JSONL transcripts and a stream-json transport" in `docs/research/cli-integration-matrix.md`.
- 7.1.3 - The stale binary-protobuf transcript claim is corrected. file: `docs/research/cli-integration-matrix-claude-code.md`.
- 7.1.4 - Both sandbox guides state the web-chat `backend="srt"` bounded-network default, session-owned process lifetimes, the provider-native override, and stale-hash resume invalidation, replacing the provider-native and shared-ACP descriptions. file: `docs/guides/sandbox-compatibility.md`.
- 7.1.5 - `adapter-fidelity.md` has exactly one AGY row stating the five-event contract, `overwrite` (not `updatedInput`), the 1.1.24-recorded honored set (`deny_unless_prior_grant`, `terminationBehavior`, `injectSteps` messages) and not-honored set (`permissionOverrides`, `injectSteps.toolCall`), and the 1.1.18 floor. file: `docs/guides/adapter-fidelity.md`.
- 7.1.6 - `ghook-user-guide.md` and `hook-schemas.md` no longer claim AGY `SessionStart` or `UserPromptSubmit`; the critical-hook set matches the landed `cli_config.rs`. file: `docs/guides/ghook-user-guide.md`.
- 7.1.7 - `providers-and-models.md`, `configuration.md`, `telegram.md`, and `sessions.md` state the 6.3 collector/bundled-seed model source, the hooks.json timeout contract, the 1.1.14 plan-control outcome, and the web-chat AGY backend respectively. file: `docs/guides/providers-and-models.md`.

## V2 End-to-End Verification
`kind: verification`

End-to-end acceptance for the epic:

- Focused pytest runs, each prefixed `GOBBY_TEST_PROTECT=1`, over the AGY parser, stream
  normalizer, backend, spawn, capability registry and provider routes. **Never the full suite.**
- Scoped `uv run ruff check` and `uv run mypy` over every touched path.
- `uv run gobby test-types audit` against the baseline where test types changed.
- `cargo test -p gobby-hooks` for the `ghook` contract changes in 2.3, proving fail-open stdout is
  protojson-legal on every AGY event (`action_from_failure` no longer emits
  `{"status":"error",…}` for agy; the `contract.rs` agy Stop row asserts `{}`).
- A session integration test proving hook transcript registration, parsing, summary/digest
  eligibility and context tracking for an AGY session.
- Route and websocket tests proving an AGY session creates, streams text and tool events,
  resumes, and interrupts — interrupt behavior matching the 1.1.8/1.1.18 cancellation
  records (process-tree termination, preserved conversation id, `--conversation` reattach).
- Interactive-mode hook integration evidence: a tmux-spawned AGY 1.1.18 session produces
  `source=agy` lines in `~/.gobby/logs/hooks.log` for all five events and one session row
  with `session_type=terminal` (record 1.1.17 artifact, re-run after 6.1).
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/providers/test_usage.py tests/servers/routes/test_servers_routes_providers.py`
  for 6.4, plus one live `curl /api/providers/agy/usage`.
- Validation-evidence provider parity for AGY (4.2.9): the six-outcome case matrix through
  `ParsedToolEvent`, stored `TranscriptEvidence`, readiness, and close-time context, at
  parity with the five incumbent providers — the run that discharges superseded tasks
  #18381 and #18677.
- Cross-provider regression proving the P2 and P3 refactors did not change Claude, Codex,
  Grok, Qwen or Droid behavior — this is the main risk the consistency work carries. The
  3.1 launch-contract matrix over the five incumbents, extended by the 5.2 AGY row, is the
  concrete anchor at the launch seam; parser-dispatch and discovery regressions are pinned
  by the existing provider parser and session-start suites.
- Rebuild and **reinstall** `~/.gobby/bin/ghook` after 2.3 and again after 4.1 — the
  final receipt and `retry_kind` Rust changes land in 4.1, and staged-effect activation
  is gated on the installed binary advertising the hook-response capability (4.1.18);
  a committed Rust change is not live until the binary is reinstalled, and the
  activation check runs before any staged effect is prepared. Activation also requires
  the rule-disposition migration to have completed cleanly: the daemon-startup narrow
  rule-disposition entry point (4.1.17's ordered trigger) must report zero
  ambiguous or partially-migrated rows before the receipt capability prepares any
  staged effect.
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`
  after the 4.1 bundled-rule edits — the regenerated
  `bundled_content_manifest.json` must match the shared tree exactly (and again after
  6.1's `agy.toml` rewrite).
- Reinstall `~/.gobby/bin/ghook` as a **new inode** after each Rust change:
  `cp target/release/ghook ~/.gobby/bin/.ghook.new && mv -f ~/.gobby/bin/.ghook.new ~/.gobby/bin/ghook`;
  confirm with `~/.gobby/bin/ghook --version` (macOS kills processes that exec an
  in-place-overwritten signed binary).
- Final: `uv run gobby plans validate .gobby/plans/agy-full-integration.md` passes before
  Round 19.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: f5a39a5f-a33d-44bd-9223-d16a2c354cff
- enhancer_session: 152c20fb-ee47-43c1-9654-cc2b386d7a9b
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / AGY-native injectSteps response translation in 4.1 with an inject_context test
  - E2 / better / Gate 0 launch-security probe for --sandbox and --dangerously-skip-permissions, consumed by 3.2 and 6.1
  - E3 / better / idempotent synthetic SESSION_START at the registration boundary keyed by provider plus conversationId
  - E4 / better / parameterized cross-provider launch-contract test matrix pinning the SRT launch seam
  - E5 / better / executable registry-to-transport TOOL_CHAT contract test with sub-1.1.9 non-advertisement
  - E6 / better / upstream AGY conversation id persisted in existing chat-session metadata with reconstruction acceptance
- declined: none
- resolution_notes: All six suggestions accepted by the user and folded in. 1.1 gained
  probe question 5 and acceptance 1.1.7; 3.2 and 6.1 now consume the recorded flag forms.
  4.1 gained response-side injectSteps translation (4.1.5, 4.1.6), the statelessness
  rewording of 4.1.2, the handle_session_start idempotency target, and 4.1.7. 3.1 gained
  the launch-contract matrix (3.1.5) with the V2 cross-provider item re-anchored to it.
  6.2 gained the executable TOOL_CHAT contract test (6.2.5). 5.2 gained persistence-based
  resume (5.2.3 reworded, 5.2.9, 5.2.10).

**Round 2** `kind: verification`

- reviewer_run: 43c0dca7-e6ce-4279-8d3c-4bfbf5619f3f
- reviewer_session: d1a7f8b5-dbe3-4d43-beb9-4c1186e7b984
- verdict: needs_review
- findings:
- agy-r2-srt-network-policy / blocking / SRT default with allow_network=True is rejected by prepare_sandbox_launch preflight
- agy-r2-policy-hash-completeness / blocking / web-chat policy hash omits backend and network fields, allowing silent boundary-change resume
- agy-r2-workspace-process-ownership / blocking / warm daemon-shared Codex/ACP subprocesses cannot give per-session SRT confinement
- agy-r2-claude-executable-seam / blocking / cli_path takes one executable while SandboxLaunch yields argv; a shim is required
- agy-r2-preinvocation-dual-dispatch / blocking / remapping PreInvocation to SESSION_START suppresses its native BEFORE_AGENT dispatch
- agy-r2-agy-tool-chat-adapter / blocking / AIAdapterStyle.CLI resolves globally to DroidSpawnToolChatAdapter, misrouting AGY
- agy-r2-agy-matrix-sequencing / blocking / 3.1's AGY matrix row required the downstream 5.2 backend out of order
- agy-r2-version-gate-boundary / blocking / 5.3-6.2 gate cycle plus async get_cli_version vs sync registry construction
- agy-r2-resolver-reachability / blocking / AgySandboxResolver without get_sandbox_resolver wiring stays unreachable
- agy-r2-spawner-target-scope / blocking / _spawn_agy_terminal fell outside 6.1's declared Targets
- agy-r2-validation-evidence-parity / blocking / governing tasks require AGY outcomes through validation evidence, readiness, close-time parity
- agy-r2-live-cancellation-contract / blocking / Gate 0 lacked a live cancellation/interrupt probe consumed by 5.2/5.3
- agy-r2-critical-hook-policy / blocking / 2.3 never named the final critical-hook set per provider
- agy-r2-parser-consumer-acceptance / nit / 2.1.2 claimed three consumers but cited one file
- agy-r2-source-ceiling-inventory / blocking / acp_client.py (955) and flow.py (966) lacked line-budget gates
- agy-r2-parser-test-seam / blocking / 4.2 pinned registry membership but no parser behavior tests
- agy-r2-model-consumer-inventory / blocking / 6.3 omitted the discovery seam and static-to-live consumer tests
- resolution_notes: All 17 findings accepted by the user and repaired in place. 1.1 gained
  the cancellation probe (question 6, 1.1.8). 2.1 split consumer acceptance (2.1.2 reworded,
  2.1.4, 2.1.5). 2.3 gained the six-provider final critical-hook matrix (2.3.4, 2.3.5). New
  deliverable 2.5 breaks the version-gate cycle with an async startup probe publishing an
  immutable support record; 5.3 and 6.2 now depend on it. 3.1 was reworked: bounded network
  default, complete versioned policy hash (3.1.6), session-owned Codex/ACP processes per
  user direction (3.1.7), daemon-emitted Claude shim per user direction (3.1.8), matrix
  limited to the five incumbents with the AGY row moved to 5.2 (5.2.11), and acp_client.py
  line gate (3.1.9). 3.2 wired get_sandbox_resolver (3.2.3). 4.1 became two-phase dispatch
  preserving BEFORE_AGENT (4.1.2 reworded, 4.1.8) with the flow.py line gate (4.1.9). 4.2
  gained the focused parser test module (4.2.8) and validation-evidence parity superseding
  tasks #18381/#18677 (4.2.9), to be closed with a supersession reference at manifest
  application. 6.1 widened to a justified wildcard target per user direction. 6.2 gained
  the dedicated AgyToolChatAdapter with provider-aware CLI dispatch (6.2.6) per user
  direction. 6.3 gained the discovery seam and consumer-transition tests (6.3.5).
  Constraints now budget all four near-ceiling files via the decompose-monolith skill.

```json plan-review-round
{"evidence_id":"8f25e215-0c04-4ce8-84ea-b3513510618d","plan_hash":"1a515b37e0589a348b7044dc58f4b5bdede5039fb778878179559c45ba30016e","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"1df93d2f4b653145e69445bb0a1df84218fc3b406b5945659deea9e36bde54e8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":17,"total":23},"evidence_id":"8f25e215-0c04-4ce8-84ea-b3513510618d","lanes":[{"candidate_count":8,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"0f22da58ce27d154cbbbf353676377a976dc21b8176107269e7aa5b80df66b19","status":"valid"},"source_digest":"a45834b3c15e742dde4d116f54f6e0328bb6982098e263b28ed62189bdfa5b0f","version":1},"findings":[{"category":"unhandled-edge","check_key":"srt-unrestricted-network-policy","description":"The planned default makes every enabled web-chat SRT launch fail before a provider starts; acceptance simultaneously requires the rejected flag to remain represented.","finding_id":"agy-r2-srt-network-policy","fix":"Define a bounded default using allow_network=False plus explicit provider/API domains or scoped capabilities. If unrestricted networking is required, add a separate SRT contract change and update the existing fail-closed test before adopting the default.","location":"P3 / §3.1","prevention":"Cross-check each proposed default configuration against runtime preflight branches and focused contract tests.","principle":"A planned default must satisfy every runtime precondition of the backend it enables.","root_cause":"Section 3.1 flips web chat to SRT while retaining allow_network=True, a combination prepare_sandbox_launch rejects before launch.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"sandbox-policy-hash-completeness","description":"A provider-native session can retain the same stored hash after switching to SRT, and other permission changes can also resume silently under a different boundary.","finding_id":"agy-r2-policy-hash-completeness","fix":"Add web_chat_sandbox_policy_hash to Targets; hash the complete normalized effective policy with an explicit version, and add stale-resume tests for backend, domains, Git/package capabilities, and socket allowances.","location":"P3 / §3.1","prevention":"Enumerate every security-relevant policy field and verify that changing each field changes the persisted hash.","principle":"Resume identity must cover the complete effective security boundary.","root_cause":"The current web-chat hash omits backend and material network/socket fields, while 3.1 relies on that hash to reject stale sessions after the SRT migration.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"workspace-scoped-process-ownership","description":"Wrapping current startup cannot provide distinct filesystem confinement for concurrent projects, and create_session is downstream of the actual shared-process launch.","finding_id":"agy-r2-workspace-process-ownership","fix":"Re-plan Codex and ACP as session-owned or workspace-and-policy-keyed process pools. Add lifecycle, teardown, concurrent-session, and two-project isolation acceptance before claiming per-session SRT.","location":"P3 / §3.1","prevention":"For every sandboxed process, record launch owner, workspace/policy key, sharing rule, and teardown boundary.","principle":"A process-scoped sandbox policy must be resolved before process launch from all workspaces that process can serve.","root_cause":"Codex and ACP currently start warm daemon-shared subprocesses before a session project path is known, while SRT preparation requires that path.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"claude-srt-executable-seam","description":"The proposed cli_path assignment cannot preserve the SRT runner, settings, violation path, provider executable, and SDK-appended Claude arguments.","finding_id":"agy-r2-claude-executable-seam","fix":"Specify a private executable shim API that execs SandboxLaunch.wrap([claude, \"$@\"]) and define cleanup on disconnect and failed start. Add an argv contract test proving SDK-appended arguments pass through exactly one wrapper.","location":"P3 / §3.1","prevention":"Validate every wrapper against the consumer API's concrete input type and argument-appending behavior.","principle":"An integration plan must bridge the exact interface types at each launch boundary.","root_cause":"ClaudeAgentOptions.cli_path accepts one executable path, while SandboxLaunch exposes a wrapped argv vector and produces no executable launcher.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"native-hook-dual-dispatch","description":"The plan would suppress per-turn before-agent rules and inject_context behavior while trying to register the session.","finding_id":"agy-r2-preinvocation-dual-dispatch","fix":"Define two-phase handling: process an idempotent synthetic SESSION_START side effect, then process the original PreInvocation as BEFORE_AGENT exactly once and translate that response to injectSteps. Test first and repeated invocations.","location":"P4 / §4.1","prevention":"For every synthesized event, test both the side effect and continued execution of the original event path on first and repeated invocations.","principle":"Synthesizing lifecycle work from a native hook must preserve the native hook's original semantic dispatch.","root_cause":"The AGY adapter and HookManager process one HookEvent; remapping PreInvocation to SESSION_START replaces its existing BEFORE_AGENT path.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"provider-specific-tool-chat-adapter","description":"Advertising AGY as CLI tool chat routes it through Droid's command and JSON-RPC protocol; the AGY stream normalizer alone does not implement ToolRuntime policy or loop limits.","finding_id":"agy-r2-agy-tool-chat-adapter","fix":"Expand Targets to provider-aware CLI dispatch and a dedicated AGY tool-chat adapter covering controlled tools, ToolLoopLimits, timeouts, cancellation, and normalized results. Keep TOOL_CHAT unavailable if AGY cannot provide the controlled-tool bridge.","location":"P6 / §6.2","prevention":"Trace every new binding through service selection, factory lookup, concrete adapter, executable, wire protocol, cancellation, and tool-policy enforcement.","principle":"A capability binding must route to an adapter that implements the bound provider's executable and protocol.","root_cause":"Tool-chat factories are keyed by adapter style, and AIAdapterStyle.CLI currently resolves globally to DroidSpawnToolChatAdapter.","section_id":"6.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"consumer-before-producer-dependency","description":"Section 3.1 cannot complete its AGY matrix acceptance without implementing section 5.2 out of order.","finding_id":"agy-r2-agy-matrix-sequencing","fix":"Limit 3.1 to Claude, Codex, Droid, Grok, and Qwen. Add the AGY row to 5.2 with SRT, native-sandbox-off, permission-flag, network-policy, and stale-hash assertions.","location":"P3 / §3.1","prevention":"For each acceptance matrix row, verify its implementation target exists in the deliverable or in a completed dependency.","principle":"A deliverable cannot require acceptance against an artifact created only by a downstream dependent deliverable.","root_cause":"Section 3.1 requires an AGY launch-contract row, while 5.2 creates AgyWebChatBackend and depends on 3.1.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-gate-boundary","description":"The plan contains a semantic dependency cycle and no executable seam for resolving the AGY version once across runtime health and capability bindings.","finding_id":"agy-r2-version-gate-boundary","fix":"Create an earlier version-gate foundation after P1. Specify an async startup probe that injects an immutable support record into synchronous runtime/registry consumers, or enumerate the full async builder migration. Make 5.3 and 6.2 depend on it.","location":"P6 / §6.2","prevention":"Trace prerequisite direction and sync/async types through every consumer before fixing phase dependencies.","principle":"A shared prerequisite must precede every consumer and expose a callable interface compatible with those consumers.","root_cause":"Section 5.3 consumes the 6.2 version gate while 6.2 depends on 5.3; get_cli_version is async while registry construction is sync.","section_id":"6.2","severity":"blocking"},{"category":"traceability","check_key":"resolver-factory-reachability","description":"The provider-native path can still raise for AGY even after the resolver class exists.","finding_id":"agy-r2-resolver-reachability","fix":"Target get_sandbox_resolver and the AGY sandbox capability entry in 3.2; add acceptance that get_sandbox_resolver(\"agy\") returns AgySandboxResolver using the live-proven flag.","location":"P3 / §3.2","prevention":"Sweep constructor tables, registries, capability predicates, fakes, and exhaustive dispatch for each new implementation.","principle":"Adding an implementation must also update every closed registry or factory that makes it reachable.","root_cause":"Section 3.2 adds AgySandboxResolver while omitting get_sandbox_resolver and the provider sandbox-capability gate.","section_id":"3.2","severity":"blocking"},{"category":"gobby-format","check_key":"new-symbol-target-coverage","description":"The helper that carries most AGY spawn behavior is outside the declared exact-symbol target scope.","finding_id":"agy-r2-spawner-target-scope","fix":"Use a justified spawn_executor.py::* target for the multi-symbol edit, or place the AGY spawner in a new focused module and target that module explicitly.","location":"P6 / §6.1","prevention":"Compare every planned new or changed symbol with the Targets block before review completion.","principle":"Every changed production scope must be represented by a concrete, valid Target.","root_cause":"Section 6.1 adds _spawn_agy_terminal inside an existing symbol-bearing file but targets only execute_spawn.","section_id":"6.1","severity":"blocking"},{"category":"missing-requirement","check_key":"validation-evidence-provider-parity","description":"Definitive AGY outcomes can parse without proving the validation and close-time behavior required by the governing tasks.","finding_id":"agy-r2-validation-evidence-parity","fix":"Add 4.1/4.2 acceptance and V2 tests for success, failure, nonterminal, contradictory, unstructured, and provenance-free AGY outcomes through ParsedToolEvent, TranscriptEvidence, readiness, and close-time context.","location":"P4 / §4.2","prevention":"Trace each governing task criterion through normalization, storage, readiness, close evaluation, and parity tests.","principle":"A provider integration must satisfy its governing open requirements through every downstream evidence consumer.","root_cause":"Open tasks #18381 and #18677 require native result through stored validation evidence, readiness, and close-time parity; the plan stops at adapter provenance and transcript parsing.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"live-cancellation-contract","description":"The backend must invent interruption semantics without live evidence about partial output, child cleanup, exit behavior, or whether the last conversation id remains resumable.","finding_id":"agy-r2-live-cancellation-contract","fix":"Add a live active-turn cancellation probe recording the signal/API, process-tree exit, partial stream outcome, orphan cleanup, and resumability. Make 5.2 cancellation and 5.3 websocket interrupt acceptance consume it.","location":"P1 / §1.1","prevention":"Map every downstream live-provider assumption back to a recorded Gate 0 probe and acceptance item.","principle":"A contract gate must probe every provider behavior later treated as a correctness requirement.","root_cause":"Gate 0 omits cancellation and interrupt behavior while 5.2 and 5.3 require process termination, cleanup, and resumability.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"critical-hook-policy-matrix","description":"AGY removal, Droid short-circuit removal, and documentation can all pass while Claude, Qwen, Codex, Grok, and Droid remain inconsistent or Droid retains an empty declaration.","finding_id":"agy-r2-critical-hook-policy","fix":"State the exact final set for all six providers and add cli_config plus contract.rs assertions for each provider's critical lifecycle hooks and noncritical failure behavior.","location":"P2 / §2.3","prevention":"Record an explicit before/after provider matrix and test every row under daemon-down lifecycle and noncritical events.","principle":"A normalization task must state the canonical state it converges every variant toward.","root_cause":"Section 2.3 describes session-lifecycle-only policy but never names the final critical-hook set for each provider.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"parser-consumer-acceptance-parity","description":"summary_context.py and cli/tokens.py can escape a bounded criteria review despite being explicit targets.","finding_id":"agy-r2-parser-consumer-acceptance","fix":"Split 2.1.2 into three acceptance items, or cite transcript_processing.py, summary_context.py, and cli/tokens.py together with unknown-source tests.","location":"P2 / §2.1","prevention":"For multi-consumer acceptance, use one item per consumer or attach an artifact reference for each.","principle":"Acceptance evidence should name every consumer claimed by the criterion.","root_cause":"Acceptance 2.1.2 claims three inline chains but cites only transcript_processing.py.","section_id":"2.1","severity":"nit"},{"category":"gobby-format","check_key":"source-ceiling-all-targets","description":"The ACP launch and AGY idempotency work have only 44 and 33 lines of headroom, so the plan can violate the enforced ceiling during ordinary implementation.","finding_id":"agy-r2-source-ceiling-inventory","fix":"Add projected line-count gates for acp_client.py and flow.py to Constraints and sections 3.1/4.1, with concrete extraction targets when either projection reaches 1000.","location":"P3 / §3.1","prevention":"Measure current and projected line counts for every targeted production file and add same-task decomposition where projection reaches 1000.","principle":"The production source-size ceiling applies to every touched eligible file.","root_cause":"The plan budgets sandbox.py and spawn_executor.py while omitting acp_client.py at 955 lines and session-start flow.py at 966 lines.","section_id":"3.1","severity":"blocking"},{"category":"weak-testability","check_key":"parser-behavior-test-seam","description":"The plan can register AgyTranscriptParser while leaving its substantive parsing and recovery behavior unverified.","finding_id":"agy-r2-parser-test-seam","fix":"Add a focused AGY parser test module target and tests for all record classes, unknown MODEL tools, truncated_fields, malformed lines, stable IDs, interrupted RUNNING records, and append-only incremental reads.","location":"P4 / §4.2","prevention":"Map each parser acceptance item to a fixture-backed focused test target before expansion.","principle":"Behavior-rich parser acceptance requires focused executable tests for every record and recovery branch.","root_cause":"Section 4.2 targets only a registry-membership assertion while specifying tool variants, truncation, malformed input, stable IDs, and incremental reads.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"live-model-consumer-inventory","description":"The static catalog can be refreshed while live discovery and provider-route behavior remain stale or untested.","finding_id":"agy-r2-model-consumer-inventory","fix":"Add provider_model_discovery.py and focused provider-model and provider-route tests to Targets; cover supported live discovery, sub-floor fallback, command failure/cache fallback, and source/availability transitions.","location":"P6 / §6.3","prevention":"Sweep discovery parsers, caches, provider routes, source labels, availability flags, fixtures, and exhaustive tests for each provider-state transition.","principle":"Changing a provider from static/unavailable to live/available requires updating discovery and every downstream contract that exhaustively represents that state.","root_cause":"Section 6.3 omits the discovery implementation seam and focused model/route tests that currently encode AGY as static and unavailable.","section_id":"6.3","severity":"blocking"}],"reviewer_run":"43c0dca7-e6ce-4279-8d3c-4bfbf5619f3f","reviewer_session":"d1a7f8b5-dbe3-4d43-beb9-4c1186e7b984","round":2,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 3** `kind: verification`

- reviewer_run: 927aed1d-5481-42c8-95d1-4f9a84342380
- reviewer_session: 1e611975-e0e6-4c63-96fc-e2d05faa25eb
- verdict: needs_review
- findings:
- agy-r3-gate0-unresolved-contracts / blocking / downstream acceptance unconditionally required resume, tool-chat advertisement, and FULL status Gate 0 leaves open
- agy-r3-transcript-fallback-recovery / blocking / hook-first discovery lacked usable/pending/invalid path states and bounded disk fallback
- agy-r3-droid-timeout-contract / blocking / 2.4's read timeout named no clock, terminal event, cleanup, or reconnect semantics
- agy-r3-test-target-inventory / blocking / six acceptance-named focused test files were absent from their deliverables' Targets
- agy-r3-version-record-lifecycle / blocking / the 2.5 support record had no startup initialization owner, sentinel, or catalog injection
- agy-r3-agy-srt-policy-inventory / blocking / sandbox_policy.py provider domain/auth maps have no agy entries and were untargeted
- agy-r3-dual-dispatch-entrypoint / blocking / two-phase dispatch was anchored to translate_to_hook_event, which cannot dispatch two events
- agy-r3-agy-outcome-recovery / blocking / parity omitted live zero/nonzero shell payload capture and failure-to-success readiness recovery
- agy-r3-model-default-acceptance / blocking / the gemini-3.5-flash default-effort reconciliation had no acceptance item or named value
- agy-r3-readiness-surface-parity / nit / "FULL across all four surfaces" did not match the matrix's three-surfaces-plus-status schema
- resolution_notes: All 10 findings accepted in unattended mode; the coordinator verified
  the entrypoint, timeout-constant, startup-owner, and sandbox-map claims against the code
  index before voting. Constraints gained the explicit Gate 0 branch rule; 6.2.1 was
  rewritten to the deterministic tool-chat branch and 7.1 to enumerated matrix cells
  (7.1.1 reworded). 1.1 gained the network/state-footprint probe (question 7, 1.1.9) and
  live zero/nonzero shell capture (1.1.10). 2.2 gained usable/pending/invalid path states
  with bounded fallback (2.2.3, 2.2.4). 2.4 adopted the ACP inactivity-timeout contract
  with terminal-error, cleanup, and reconnect semantics (2.4.3 reworded, 2.4.4). 2.5 named
  the daemon lifespan as initialization owner with a fail-closed sentinel and single-probe
  guarantee (2.5.4); 6.3 now reads that record for sub-floor detection. 3.2 targeted
  sandbox_policy.py and its agy map entries (3.2.4), proven at the launch seam by the
  extended 5.2.11. 4.1 anchored two-phase dispatch to an AgyAdapter.handle_native override
  (4.1.2 reworded). 4.2 gained the failure-then-success readiness recovery case (4.2.10).
  6.3 named the canonical gemini-3.5-flash default resolution with consumer parity (6.3.6)
  and narrowed the #19483 attribution. All six acceptance-named focused test files were
  added to their deliverables' Targets (2.4, 2.5, 3.1, 4.1, 5.2, 5.3, 6.1, 6.2).

```json plan-review-round
{"evidence_id":"3ebc7865-e5a4-4c69-abd5-7e194efc2483","plan_hash":"690eb839e2524c5df6060af059ac4d22102f13251ae6523f9c89a292bda45ba3","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b62f3a97561c8909c11f12249b9ec3023446c0b323119202c5814f21ccbbe9a1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":10,"total":12},"evidence_id":"3ebc7865-e5a4-4c69-abd5-7e194efc2483","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"18ee97c0958051e891d97f1e68232245a9958234cd26fb8946dfc7b93a70d67b","status":"valid"},"source_digest":"3dd92865e89abba6f88ab5070ed139358e933d1c1011d7df82c36105fa4874ef","version":1},"findings":[{"category":"bad-sequencing","check_key":"resolved-contract-before-expansion","description":"A failed resume probe makes 5.2.2, 5.2.9, and FULL documentation impossible, while 6.2 permits TOOL_CHAT to remain unavailable but 6.2.1 requires it to be advertised.","finding_id":"agy-r3-gate0-unresolved-contracts","fix":"Run the six live probes before the next approval round, record the observed contracts in the artifact, and rewrite 5.2, 6.2, and 7.1 to one deterministic result; use typed deferrals for any unsupported surface.","location":"P1 / §1.1; consumers §5.2, §6.2, §7.1","prevention":"Resolve every live-provider branch before handoff, or represent the unavailable branch as a typed deferral with an open task.","principle":"An execution plan must make downstream acceptance satisfiable under one resolved contract.","root_cause":"Gate 0 leaves resume and controlled TOOL_CHAT feasibility open while later sections unconditionally require resumable TOOL_CHAT and FULL status.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hook-path-usability-fallback","description":"A truthy unusable hook path can suppress disk fallback indefinitely, while an unbounded fallback scan can block the hook request path.","finding_id":"agy-r3-transcript-fallback-recovery","fix":"Define usable, pending, and invalid path states; add bounded retry for delayed creation; persist only usable paths; and use direct bounded candidates or offload capped traversal before disk fallback.","location":"P2 / §2.2","prevention":"Test preferred-path usability, delayed creation, permanent absence, unreadability, traversal errors, and bounded lookup latency.","principle":"Fallback selection must test usability and bound recovery work, not merely test whether a preferred value is nonempty.","root_cause":"The plan defines hook-first selection without states for pending, absent, stale, or unreadable hook paths, and expands a blocking disk traversal into synchronous session-start handling.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ndjson-timeout-cleanup-contract","description":"Different reasonable timeout implementations can either kill active long turns, wait forever on periodic output, or leave a Droid subprocess and session handle orphaned.","finding_id":"agy-r3-droid-timeout-contract","fix":"Specify initialization and active-turn timeout values and reset rules, require one terminal error, terminate the owned process tree, remove the handle, leave the session reconnectable, and add focused timeout/cancellation tests.","location":"P2 / §2.4","prevention":"For every timeout, specify the clock boundary, caller-visible error, owned-resource cleanup, and post-timeout state, then test each transition.","principle":"A new stream timeout needs explicit clock, outcome, cleanup, and recovery semantics.","root_cause":"Section 2.4 requests a read timeout without naming its source or value, per-line versus whole-turn behavior, terminal event, process-tree cleanup, or reconnect state.","section_id":"2.4","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-all-changed-tests","description":"Six focused test paths are required by acceptance but absent from applicable Targets: test_version_gate.py, test_launch_contracts.py, test_agy.py, test_agy_backend.py, test_spawn_executor.py, and test_agy_tool_chat_contract.py.","finding_id":"agy-r3-test-target-inventory","fix":"Add each test file to every deliverable that changes it, including the shared launch-contract and AGY backend test files in all sections that add distinct cases.","location":"§2.5, §3.1, §4.1, §5.2, §5.3, §6.1, §6.2","prevention":"Compare every file and test artifact named by acceptance with each deliverable's exact Targets block before resubmission.","principle":"Every file a deliverable changes must appear in that deliverable's Targets inventory.","root_cause":"Round 2 repairs added focused test acceptance references without adding the corresponding changed test files to Targets.","section_id":"2.5","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-record-initialization","description":"The record can be read before publication or bypassed by model discovery, producing inconsistent AGY availability and duplicate version probes.","finding_id":"agy-r3-version-record-lifecycle","fix":"Target the concrete daemon startup owner, await record publication before exposing AGY, define a fail-closed uninitialized sentinel, inject the same record into every consumer including ProviderModelCatalog, and test exactly-once execution.","location":"P2 / §2.5; consumers §5.3, §6.2, §6.3","prevention":"For shared startup records, inventory every consumer and test pre-initialization, concurrent initialization, failure, and exactly-once reads.","principle":"A shared async prerequisite needs an initialization owner and barrier before synchronous or lazy consumers can read it.","root_cause":"The new module defines a once-only probe but no targeted daemon startup integration point publishes the record before health, registry, spawn, session, and model-catalog consumers execute.","section_id":"2.5","severity":"blocking"},{"category":"traceability","check_key":"srt-provider-policy-inventory","description":"AGY SRT web-chat and spawn launches can pass wrapper preflight yet lack upstream network access and access to ~/.gemini/antigravity-cli credentials, transcripts, and writable state.","finding_id":"agy-r3-agy-srt-policy-inventory","fix":"Add sandbox_policy.py to Targets, define live-proven AGY domains and exact read/write state roots, and extend launch-contract tests to prove authentication, transcript/state writes, and bounded network access.","location":"P3 / §3.1 and §3.2; consumers §5.2 and §6.1","prevention":"Sweep provider domains, credentials, read/write exceptions, executable resolution, socket grants, fakes, and launch tests for every new SRT provider.","principle":"A new sandboxed provider must be added to every closed policy inventory that supplies its network, credentials, and writable runtime state.","root_cause":"AGY is absent from sandbox_policy.py provider domain, auth-read, and auth-write maps, and that file is absent from the plan's Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"native-hook-dual-dispatch-entrypoint","description":"The repaired text preserves both SESSION_START and BEFORE_AGENT conceptually, but the current adapter contract has no executable seam that can perform both dispatches.","finding_id":"agy-r3-dual-dispatch-entrypoint","fix":"Specify and target an AgyAdapter.handle_native override, or a deliberate base-contract extension, that dispatches synthetic SESSION_START first, dispatches the original PreInvocation once, and returns only the original event's translated response.","location":"P4 / §4.1","prevention":"Trace synthesized events through the concrete adapter entrypoint, return type, dispatch count, response ownership, and first/repeated-event tests.","principle":"A two-event semantic repair must name an entrypoint whose interface can dispatch both events in order.","root_cause":"translate_to_hook_event returns one HookEvent and BaseAdapter.handle_native invokes HookManager.handle exactly once, while the plan anchors both phases to translation.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","check_key":"validation-evidence-recovery-sequence","description":"The plan can satisfy isolated success and failure cases while still violating #18381's provider-proven payload requirement and failure-to-success readiness recovery.","finding_id":"agy-r3-agy-outcome-recovery","fix":"Add scrubbed live zero- and nonzero-exit AGY shell records with exact structured fields and provenance, plus a sequential test proving failure remains fail-closed and a later correlated definitive success restores readiness and close-time context.","location":"P1 / §1.1 and P4 / §4.2","prevention":"Trace governing validation criteria through live capture, normalization, storage, readiness transitions, and close-time context, including recovery sequences.","principle":"Superseding governing tasks requires explicit acceptance for every live evidence and state-transition criterion they carry.","root_cause":"The six-outcome parity list omits live representative zero/nonzero shell payload capture and does not assert failure followed by later definitive success restoring readiness.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"model-default-acceptance-parity","description":"The model catalog can pass all five criteria while AGY_MODELS and provider_model_defaults continue to disagree; task #19483 is broader planning work and does not close this bounded gap.","finding_id":"agy-r3-model-default-acceptance","fix":"Add an acceptance item naming the canonical gemini-3.5-flash default effort and test both consumers agree; remove or precisely qualify the #19483 attribution.","location":"P6 / §6.3","prevention":"Map every body-level behavior claim to an acceptance item and focused consumer-parity test.","principle":"Every stated behavioral change needs a bounded acceptance item covering all named consumers.","root_cause":"Section 6.3 promises to reconcile gemini-3.5-flash default effort but acceptance 6.3.1 through 6.3.5 never names the chosen value or cross-consumer parity.","section_id":"6.3","severity":"blocking"},{"category":"traceability","check_key":"documentation-readiness-schema-parity","description":"An implementer cannot tell whether the fourth item means Status, spawn, or tool chat, so different edits can satisfy the wording.","finding_id":"agy-r3-readiness-surface-parity","fix":"Enumerate Hook=Full, Transcript=JSONL, Web-chat=custom stream-json, and Status=FULL, or explicitly revise the matrix schema before adding another surface.","location":"P7 / §7.1","prevention":"Compare documentation criteria with the current table schema and enumerate every changed cell.","principle":"Documentation acceptance should name exact cells in the governing schema.","root_cause":"The plan says FULL across four surfaces while the matrix defines three readiness surfaces plus a status column.","section_id":"7.1","severity":"nit"}],"reviewer_session":"1e611975-e0e6-4c63-96fc-e2d05faa25eb","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 4** `kind: verification`

- reviewer_run: fa6d2a5e-49c4-4fd8-b43a-16ea1c7d0104
- reviewer_session: d3da2185-662d-4c74-928c-b049decb0f85
- verdict: needs_review
- findings:
- agy-r4-gate0-preexpansion-contracts / blocking / the prose Gate 0 branch rule had no enforcement mechanism over the already-derived 17-leaf manifest
- agy-r4-parser-dispatch-caller-inventory / blocking / deleting _get_parser stranded direct callers in transcript index, reader, window, and two test modules
- agy-r4-transcript-classifier-entrypoint / blocking / flow.py accepts truthy hook paths before derivation, so a helper-scoped classifier never governs the primary path
- agy-r4-transcript-recovery-contract-split / blocking / find_transcript_on_disk also serves watchdog, reader, and validation-evidence recovery beyond the hook path
- agy-r4-version-record-lifecycle-order / blocking / lifespan publication lands after ToolChatService registry construction and the model catalog self-probes
- agy-r4-resolver-symbol-target-scope / blocking / AgySandboxResolver fell outside 3.2's exact-symbol Targets
- agy-r4-resolver-capability-sequencing / blocking / get_sandbox_resolver's gate reads PROVIDER_CAPABILITIES, whose AGY row was owned downstream in 6.1
- agy-r4-agy-policy-dependency / blocking / 5.2.11 consumed 3.2.4's policy entries without a 3.2 dependency edge
- agy-r4-webchat-srt-launch-boundary / blocking / synchronous create_session precedes project-path hydration; the async start seam was untargeted
- agy-r4-synthetic-start-response-merge / blocking / discarding the synthetic SESSION_START response loses startup context permanently
- agy-r4-agy-sidecar-hydration / blocking / parser-state sidecar persistence is codex-only and drops pending AGY call/result correlation across restart
- agy-r4-agy-read-timeout-contract / blocking / 5.2's read timeout named no clock, reset, terminal, cleanup, or reconnect semantics
- agy-r4-spawn-version-gate / blocking / execute_spawn bypasses the registry gate, so sub-floor AGY binaries could spawn
- agy-r4-tool-chat-provider-aware-cache / blocking / ToolChatService caches adapters by style alone, cross-routing first-use Droid/AGY
- agy-r4-model-cache-version-compatibility / blocking / a 1.0.10-era cache survives migration and overrides static fallback on failed discovery
- agy-r4-adjacent-test-target-inventory / blocking / pending-message, stream-normalizer, builder, resolution, and text-generation test targets were missing
- agy-r4-model-label-consumer-acceptance / nit / 6.3.2 cited one of the three files carrying the retired label
- agy-r4-wiki-generated-target / blocking / the wiki concept page is gitignored generated output and cannot be a durable Target
- resolution_notes: All 18 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — flow.py:337 truthy
  acceptance, runner_init/services.py:69 pre-lifespan construction, the style-keyed
  adapter cache at _tool_chat_service.py:240-252, the codex-only parser-state gate at
  processor_transcripts.py:220, provider_supports_sandbox reading PROVIDER_CAPABILITIES,
  versionless cache acceptance in provider_models.py, /wiki/ in .gitignore, _session.py
  at 988 lines, and the synchronous create_session at runtime_manager.py:244. Repairs:
  Constraints gained the enforceable Gate 0 checkpoint mechanism (1.1.11) and the
  five-file line budget including _session.py. 2.1 inventoried the direct _get_parser
  callers (2.1.6). 2.2 moved classification to the session-start caller and split the
  discovery contract for late-recovery callers (2.2.5, 2.2.6). 2.5 moved record
  publication into runner initialization before service construction and pulled the
  catalog's version read into scope (2.5.4 reworded, 2.5.5). 3.1 re-anchored
  session-owned launches to the async post-hydration seam with _session.py budgeted
  (3.1.1 and 3.1.7 reworded, 3.1.10). 3.2 took the justified wildcard sandbox.py target,
  the PROVIDER_CAPABILITIES row, and reachability tests (3.2.5). 4.1 merged the synthetic
  response into the BEFORE_AGENT reply (4.1.2 reworded, 4.1.10) and added pending-message
  coverage (4.1.11). 4.2 gained sidecar hydration (4.2.11). 5.1 gained its focused test
  module (5.1.5). 5.2 gained the 3.2 dependency edge and the restated inactivity-timeout
  contract (5.2.12). 6.1 gained the 2.5 dependency and the spawn-time record gate (6.1.7,
  6.1.8). 6.2 made adapter selection and caching provider-aware (6.2.7). 6.3 gained cache
  compatibility (6.3.7), the three-file label acceptance (6.3.2 reworded), and
  text-generation effort parity (6.3.8). 7.1 dropped the generated wiki target.

```json plan-review-round
{"evidence_id":"18ac26e4-7cb2-4d0d-a979-08b0aab7d31f","plan_hash":"144777c0e77782d5639cc8f721e3814f93c41f2eff64033f1505284149829862","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2460c1291eb0a0678256e868acc7c3b854ab58e1dd1b01e91c15dd0981ac2267","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":11,"emitted_findings":18,"total":29},"evidence_id":"18ac26e4-7cb2-4d0d-a979-08b0aab7d31f","lanes":[{"candidate_count":11,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":10,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"905a64e3dfb88ea131a89dcc9e89b07af744bdabe36bad3c73fb4b34d28f583f","status":"valid"},"source_digest":"4c88066b9c7732b1ad1f17e74e79e96bb7d2cf4715a727b64fe81126052b9851","version":1},"findings":[{"category":"bad-sequencing","check_key":"resolved-contract-before-expansion","description":"Resume, cwd, sandbox flags, cancellation, network/state, image input, and controlled-tool feasibility remain open while downstream leaves require concrete outcomes including `--conversation` resume and `Status=FULL`.","finding_id":"agy-r4-gate0-preexpansion-contracts","fix":"Run the live probes as a prerequisite outside this implementation manifest, then rewrite and re-review every consumer against the recorded results. Convert any abandoned surface to a typed deferral with an open `deferred-from` task before deriving the 17-leaf manifest.","location":"P1 / §1.1; consumers §3.2, §5.2, §6.1, §6.2, §7.1","prevention":"Before approval, verify each live-dependent acceptance item against recorded evidence or represent it as a valid typed deferral.","principle":"Every expanded leaf must have one satisfiable contract before the manifest is approved.","root_cause":"The server derives all 17 leaves in one manifest before Gate 0 runs; the prose promise to revise and re-review affected sections has no mechanism to stop those already-expanded downstream leaves.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"parser-registry-caller-inventory","description":"Deleting `_get_parser` as written leaves runtime calls and tests in `transcript_index.py`, `transcript_reader.py`, `transcript_window.py`, `tests/sessions/test_transcript_parsers.py`, and `tests/sessions/transcripts/test_droid_parser.py` on a removed symbol.","finding_id":"agy-r4-parser-dispatch-caller-inventory","fix":"Add those files to 2.1 Targets, migrate every caller to `transcripts.get_parser`, and add focused unknown-source and Droid-path regressions before deleting `_get_parser`.","location":"P2 / §2.1","prevention":"Sweep imports, call sites, re-exports, fakes, and tests before deleting a shared helper.","principle":"Deleting a shared dispatch helper requires migrating every production caller and affected test in the same deliverable.","root_cause":"Section 2.1 inventories five maps but omits direct `_get_parser` consumers in transcript index, reader, window, and their parser tests.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hook-path-usability-fallback","description":"The round-three classifier repair can be implemented inside its targeted helpers while session-start flow continues persisting reported-but-absent or unreadable paths.","finding_id":"agy-r4-transcript-classifier-entrypoint","fix":"Add `flow.py` and `tests/hooks/test_transcript_path_derivation.py` to 2.2 Targets; route every reported path through the classifier before persistence; test usable, pending, invalid, retry, and fallback. Add the same-task line-budget/decomposition gate because `flow.py` is already 966 lines.","location":"P2 / §2.2; interaction with §4.1","prevention":"Trace raw input through selection, validation, persistence, retry, and fallback at the actual caller.","principle":"A path classifier must own the value before any caller selects or persists it.","root_cause":"`handle_session_start` accepts a truthy hook path directly before invoking `derive_transcript_path`, so usable/pending/invalid classification never governs the primary AGY path.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"transcript-discovery-caller-contract","description":"A helper-only rewrite can either retain blocking traversal on the hook path or remove discovery needed by watchdog, transcript-reader, and validation-evidence recovery.","finding_id":"agy-r4-transcript-recovery-contract-split","fix":"Split bounded hook-time resolution from late recovery, or extend the resolver contract with explicit project/cwd context and update every caller. Target the watchdog, transcript reader, validation-evidence consumer, and focused tests for all provider layouts.","location":"P2 / §2.2","prevention":"Inventory every caller of a shared recovery primitive and test each caller's latency, context, and fallback behavior.","principle":"A synchronous hook lookup and asynchronous late recovery need contracts appropriate to their latency and available context.","root_cause":"`find_transcript_on_disk` is shared by session start, watchdog, transcript reader, and validation-evidence recovery, but section 2.2 replaces it with direct candidates using a signature whose callers lack project/cwd context.","section_id":"2.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"async-version-record-initialization","description":"A supported AGY installation can remain frozen unavailable in the already-built tool-chat registry, and catalog refresh can violate the exactly-one-version-subprocess acceptance.","finding_id":"agy-r4-version-record-lifecycle-order","fix":"Move AGY support-record publication before support-dependent service construction, or atomically rebuild every retained holder before serving. Move the AGY `ProviderModelCatalog` version read into 2.5, forbid its re-probe, and add a daemon-construction test covering the installed `ToolChatService` plus catalog refresh.","location":"P2 / §2.5; consumers §6.2 and §6.3","prevention":"Map startup construction order and every probe/read consumer; test pre-publication, supported startup, exactly-once execution, and retained-service visibility.","principle":"A shared support record must be published before any long-lived consumer freezes its value, and every version consumer must read that record.","root_cause":"FastAPI lifespan runs after `ToolChatService` builds and retains its registry, while `ProviderModelCatalog` still launches its own AGY version probe until downstream 6.3.","section_id":"2.5","severity":"blocking"},{"category":"gobby-format","check_key":"new-symbol-target-coverage","description":"`AgySandboxResolver` falls outside the manifest-owned edit scope, so an implementing leaf cannot add the class without an out-of-scope change.","finding_id":"agy-r4-resolver-symbol-target-scope","fix":"Use a justified `src/gobby/agents/sandbox.py::*` target for the multi-symbol edit, or place `AgySandboxResolver` in a new focused module and target that file explicitly.","location":"P3 / §3.2","prevention":"Compare every planned new or changed symbol with the exact Targets block before review completion.","principle":"Every new symbol in an existing symbol-bearing file must be inside the declared Target scope.","root_cause":"Section 3.2 creates `AgySandboxResolver` while targeting only three existing symbols in `sandbox.py`.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"resolver-factory-reachability","description":"Section 3.2 cannot satisfy `get_sandbox_resolver(\"agy\")` before a dependent leaf adds the required capability entry.","finding_id":"agy-r4-resolver-capability-sequencing","fix":"Move the AGY `PROVIDER_CAPABILITIES` entry and `tests/agents/test_sandbox.py` reachability/gate cases into 3.2; let 6.1 consume the completed capability record.","location":"P3 / §3.2; downstream §6.1","prevention":"Check factory guards, capability tables, constructors, and focused tests in the same deliverable as a new implementation.","principle":"An implementation must be reachable at the completion boundary of the deliverable that introduces it.","root_cause":"`get_sandbox_resolver` rejects providers absent from `PROVIDER_CAPABILITIES`, but the AGY row is owned by downstream 6.1 even though acceptance 3.2.3 requires the resolver to work.","section_id":"3.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"consumer-before-producer-dependency","description":"The AGY launch-contract row can execute before its required domain, credential, state-root, and environment policy entries exist.","finding_id":"agy-r4-agy-policy-dependency","fix":"Add 3.2 to 5.2's dependencies and retain the 5.2 launch-contract row as downstream proof of the completed policy inventory.","location":"P5 / §5.2; producer §3.2","prevention":"Resolve each cross-section acceptance reference to an explicit dependency edge.","principle":"Acceptance cannot consume an artifact from an unordered sibling deliverable.","root_cause":"Acceptance 5.2.11 requires the AGY sandbox-policy entries produced by 3.2.4, while 5.2 depends only on 5.1 and 3.1.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"workspace-scoped-process-ownership","description":"The plan cannot launch session-owned SRT subprocesses during `create_session` without blocking the event loop or using the wrong workspace, and changing the real caller would be out of manifest scope.","finding_id":"agy-r4-webchat-srt-launch-boundary","fix":"Make `ManagedChatSessionBase.start`/backend attach the SRT preparation owner after project hydration, or make creation async and move it after resolution. Target `ChatSessionMixin._create_chat_session_inner` and lifecycle owners, test first start/resume/failure with final worktree paths, and decompose `_session.py` in this task because it is already 988 lines.","location":"P3 / §3.1; consumers §5.2 and §5.3","prevention":"Record process owner, async boundary, workspace source, launch point, teardown point, and failed-start cleanup for every backend.","principle":"A workspace-scoped asynchronous sandbox launch must occur after the final project path is known.","root_cause":"`WebChatRuntimeManager.create_session` is synchronous and runs before `_session.py` resolves `project_path`; awaited `session.start` is the later executable seam but is absent from Targets.","section_id":"3.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"synthetic-hook-response-merge","description":"AGY receives only the original `BEFORE_AGENT` response; startup context is lost while later events correctly suppress it as already delivered.","finding_id":"agy-r4-synthetic-start-response-merge","fix":"Merge synthetic startup `context`/`system_message` and required metadata into the original `BEFORE_AGENT` HookResponse, preserve the original decision, translate once to `injectSteps`, and mark startup injected only after successful emission. Test exact-once startup payload on first and repeated `PreInvocation`.","location":"P4 / §4.1","prevention":"Trace response ownership, merge policy, persistent markers, and first/repeated-event tests for every synthesized event.","principle":"When one native hook carries two logical events, caller-visible outputs from both phases must be delivered exactly once before emission is recorded.","root_cause":"The plan discards the synthetic `SESSION_START` HookResponse while `handle_session_start` composes startup context/system message and marks that context injected.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"append-only-sidecar-hydration","description":"An AGY append after daemon restart invalidates the saved cursor or loses the pending tool-call ID when the boundary falls between `PLANNER_RESPONSE` and its result.","finding_id":"agy-r4-agy-sidecar-hydration","fix":"Target `processor_lifecycle.py` and the sidecar seam; admit verified append-only AGY growth, implement `AgyTranscriptParser.snapshot_state`/`hydrate_state`, and add a restart test that appends the result after a saved tool-call boundary.","location":"P4 / §4.2","prevention":"Test restart at every multi-record correlation boundary with an appended record after the saved cursor.","principle":"Incremental transcript parsing must preserve cursor validity and parser correlation state across daemon reconstruction.","root_cause":"Current enlarged-file sidecar hydration is Codex-only, and base parser snapshots omit the pending AGY call/result correlation state.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"ndjson-timeout-cleanup-contract","description":"Implementers can choose incompatible timeout behavior that kills active long turns, hangs on silence, duplicates terminal errors, or leaves a locked/orphaned session.","finding_id":"agy-r4-agy-read-timeout-contract","fix":"State whether AGY reuses `DEFAULT_ACP_PROMPT_TIMEOUT_SECONDS` as a per-line inactivity timeout and name its override. Add expiry/reset tests requiring one terminal error, process-tree cleanup, lock release, preserved confirmed conversation ID, and reconstructable state.","location":"P5 / §5.2","prevention":"For every timeout, specify source/value, per-line or whole-turn clock, reset events, error count, resource cleanup, and reconnect state.","principle":"A stream timeout needs an explicit clock, reset rule, terminal outcome, cleanup, and recoverable post-timeout state.","root_cause":"Section 5.2 requires a read timeout but acceptance covers only large lines, cancellation, and nonzero exit.","section_id":"5.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"spawn-support-record-gate","description":"A sub-1.1.9 or unparseable AGY binary can start even while capability metadata truthfully reports it unavailable.","finding_id":"agy-r4-spawn-version-gate","fix":"Make 6.1 depend on 2.5 and read the support record in `execute_spawn` before sandbox/session/terminal/process side effects. Add `test_provider_resolution.py`, `test_command_builder.py`, and spawn-executor cases for explicit, inherited, agent-configured, and default selection with the actionable upgrade reason.","location":"P6 / §6.1; prerequisite §2.5 and metadata §6.2","prevention":"Trace version support through every advertised and executable entry point and test supported, sub-floor, absent, unparseable, and pre-publication records.","principle":"Every executable entry point for a version-gated provider must enforce the initialized support record before side effects.","root_cause":"Section 6.1 enables static provider resolution and direct `execute_spawn`; section 6.2 gates registry metadata only, while explicit and agent-selected spawns bypass that registry.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"provider-specific-tool-chat-adapter","description":"The first CLI adapter cached can be reused for the other provider, so AGY can still route to Droid or Droid to AGY despite the dedicated adapter.","finding_id":"agy-r4-tool-chat-provider-aware-cache","fix":"Target `_tool_chat_service.py` and its tests; pass the resolved `CapabilityBinding` into selection and key factories/cache by `(adapter_style, provider)`, or add a distinct exhaustive style. Test Droid→AGY and AGY→Droid in one service instance.","location":"P6 / §6.2","prevention":"Sweep service selection, factory signature, cache key, binding propagation, and both provider orders whenever one style gains multiple protocols.","principle":"A provider-specific binding must select and cache an adapter by provider-aware identity.","root_cause":"`ToolChatService` accepts zero-argument factories and caches solely by `AIAdapterStyle`; changing only the builder factory leaves first-use Droid/AGY cross-routing.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"model-cache-version-compatibility","description":"An existing 1.0.10 cache can survive the 1.1.9 migration and override the refreshed static catalog on the first failed live discovery.","finding_id":"agy-r4-model-cache-version-compatibility","fix":"Define AGY cache compatibility, invalidate pre-1.1.9 and retired-source entries, and reuse cache only when compatible with the immutable support record. Add a migration test loading a real old-cache shape before failed refresh.","location":"P6 / §6.3","prevention":"Test upgrade, downgrade, retired source labels, old schemas, discovery failure, and cache/static selection.","principle":"A cached provider catalog may override fallback only when its version and schema are compatible with the installed support record.","root_cause":"Current refresh prefers any cached models after discovery failure and accepts old cache shapes without checking AGY CLI/catalog version.","section_id":"6.3","severity":"blocking"},{"category":"gobby-format","check_key":"target-inventory-all-changed-tests","description":"Missing Targets remain for `tests/hooks/test_pending_message_provider_contracts.py` (4.1), a focused `test_agy_stream.py` (5.1), command-builder and provider-resolution tests (6.1), and `tests/ai/test_text_generation.py` (6.3).","finding_id":"agy-r4-adjacent-test-target-inventory","fix":"Add those test paths to their owning Targets and acceptance: AGY pending-message delivery, stream init/delta/result dedupe/tool/malformed branches, AGY argv/provider selection, and fixture-driven default-effort normalization.","location":"§4.1, §5.1, §6.1, §6.3","prevention":"After each target change, sweep adjacent unit, registry, consumer, exhaustive, and integration suites and compare all changed tests with Targets.","principle":"Every changed contract and behavior-rich new parser must name its affected focused test artifacts in the owning deliverable.","root_cause":"The round-three repair added acceptance-named tests but missed adjacent suites and 5.1 has no focused stream-normalizer test target.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"model-label-consumer-acceptance-parity","description":"A bounded review can pass 6.3.2 while stale source labels remain in two of the three affected files.","finding_id":"agy-r4-model-label-consumer-acceptance","fix":"Split 6.3.2 by consumer or cite all three files and add a repository-wide absence assertion for the retired label.","location":"P6 / §6.3","prevention":"Split multi-consumer acceptance or attach an artifact reference for every claimed consumer.","principle":"Acceptance evidence should identify every consumer claimed by a repository-wide transition.","root_cause":"The eight retired `agy-1.0.10-static` occurrences span registry builder, provider defaults, and a capability test, while 6.3.2 cites one file.","section_id":"6.3","severity":"nit"},{"category":"gobby-format","check_key":"durable-doc-target","description":"Direct edits to the AGY concept page cannot be committed reliably and can be overwritten by wiki regeneration.","finding_id":"agy-r4-wiki-generated-target","fix":"Remove the generated page from direct Targets or target its durable source and specify the `gwiki` regeneration/verification command. Add acceptance for the durable AGY knowledge source.","location":"P7 / §7.1","prevention":"Check target tracking status, generator ownership, source-of-truth location, and acceptance for every generated documentation target.","principle":"A documentation leaf must target a durable source or specify the generator that owns derived output.","root_cause":"`wiki/knowledge/concepts/agy.md` is ignored generated output, and 7.1 gives it neither source/regeneration workflow nor acceptance.","section_id":"7.1","severity":"blocking"}],"reviewer_session":"d3da2185-662d-4c74-928c-b049decb0f85","round":4,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 5** `kind: verification`

- reviewer_run: f105dd0f-3786-4b87-9096-2e9e579e636c
- reviewer_session: 406738e0-e8fe-45d1-8649-f6cf641ca761
- verdict: needs_review
- findings:
- agy-r5-controlled-tool-gate0-contract / blocking / 6.2 required a live-proven controlled-tool bridge no Gate 0 probe produces
- agy-r5-session-start-order / blocking / 2.2 and 4.1 edit handle_session_start with no ordering edge, risking an unclassified persisted path
- agy-r5-transcript-recovery-owners / blocking / TranscriptReader._ensure_transcript_path and the watchdog/evidence recovery suites were outside 2.2's scope
- agy-r5-agy-sidecar-append-hydration / blocking / the codex-only allow_append admission gate in processor_lifecycle.py rejects enlarged AGY sidecars at reconstruction
- agy-r5-parser-helper-self-caller / blocking / _parse_lines directly calls _get_parser and was outside 2.1's deletion inventory
- agy-r5-version-probe-async-owner / blocking / sync init_services inside GobbyRunner.__init__ under run_gobby's active loop cannot await the version probe
- agy-r5-webchat-bootstrap-ownership / blocking / one daemon CodexAppServerClient serves both HTTPServer sync and web chat; session ownership requires a bootstrap split
- agy-r5-watchdog-provider-registry / blocking / KNOWN_WATCHDOG_PROVIDERS and the reader map omit agy, excluding spawned AGY sessions from watchdog diagnostics
- agy-r5-gate0-reconciliation-contract / blocking / 1.1.11 promised a leaf-update operation the expansion system does not provide
- agy-r5-startup-context-commit-boundary / blocking / both session-start flows mark context injected before adapter translation, stranding startup context on failure
- agy-r5-agy-dual-timeout-contract / blocking / hardcoded --print-timeout 60s preempts the 120-second inactivity contract on healthy streaming turns
- resolution_notes: All 11 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — _parse_lines calling
  _get_parser at transcript_parsing.py:60, find_transcript_on_disk thread-offloaded at
  transcript_reader.py:418, the codex-only allow_append admission at
  processor_lifecycle.py:265, KNOWN_WATCHDOG_PROVIDERS at watchdog/models.py:17 with the
  import-time reader-map guard, sync init_services called from GobbyRunner.__init__
  constructed inside async run_gobby (runner.py:263), the shared CodexAppServerClient
  wired to both consumers at runner_init/servers.py:105-118,
  mark_startup_context_injected committing at context.py:80 before adapter translation
  with handle_pre_created_session at flow.py:766, reset_expansion_output at
  tasks/expansion/_reset.py, and the hardcoded --print-timeout 60s in 5.2's argv.
  Repairs: Constraints and 1.1.11 now name the executable Gate 0 reconciliation
  transaction (reset_expansion_output plus re-expansion). 1.1 gained the controlled-tool
  bridge probe (question 8, 1.1.12) and the --print-timeout characterization (question 9,
  1.1.13). 2.1 targeted _parse_lines (2.1.1 reworded). 2.2 targeted the reader recovery
  caller and both recovery suites (2.2.6 reworded, 2.2.7, 2.2.8). 2.5 moved publication
  to awaited run_gobby before GobbyRunner construction (2.5.4 reworded). 3.1 split Codex
  client bootstrap ownership (3.1.11). 4.1 gained the 2.2 dependency edge and the
  claim-token commit boundary over both session-start flows (4.1.10 reworded). 4.2
  admitted agy at the reconstruction admission gate (4.2.12). 5.2 subordinated
  --print-timeout to the inactivity contract (5.2.13). 6.1 added the AGY watchdog reader
  with registry parity (6.1.9). 6.2 now consumes the recorded 1.1.12 outcome (6.2.1
  reworded).

```json plan-review-round
{"evidence_id":"3ef43239-50e1-4a60-850d-67540d78cb0e","plan_hash":"c2e72e26b00531f62a3db211ad8064a36e4c56620fa10694874864e64779ffe1","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"d57e1e3454ef5212269a577d4e94688a910ad6b0a7c8d9da969b706c55cc2ab7","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":11,"total":13},"evidence_id":"3ef43239-50e1-4a60-850d-67540d78cb0e","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"a118f074c3551427a6fac99d195c740d7d99c40555eb89628a0b5d71a2478a9c","status":"valid"},"source_digest":"f7c2b746868f2283733565624f7ae10472401c83f57c1706ae8804ff7ca46095","version":1},"findings":[{"category":"missing-requirement","check_key":"controlled-tool-gate0","description":"The Round 4 repair did not add the controlled-tool feasibility probe consumed by 6.2. The plan can therefore advertise TOOL_CHAT without evidence that AGY print mode can expose only Gobby-controlled tools or enforce the denial boundary.","finding_id":"agy-r5-controlled-tool-gate0-contract","fix":"Add a fixture-backed 1.1 probe recording the exact controlled-tool transport/configuration, allowed tool set, and denial behavior. Rewrite 6.2 to consume the recorded supported or unavailable outcome explicitly.","location":"Phase 1 / § 1.1","prevention":"For every conditional downstream capability, map one named Gate 0 probe and fixture field to each supported and unsupported branch before review.","principle":"Every unresolved provider contract consumed by downstream acceptance needs a live Gate 0 producer and a deterministic supported-or-unavailable branch.","root_cause":"Section 6.2 requires a live-proven Gobby-controlled tool bridge, while Section 1.1's seven probes never characterize that bridge.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"session-start-order","description":"4.1's AGY registration, transcript association, and idempotency work assumes the usable/pending/invalid classifier owned by 2.2, yet the manifest does not order them. Parallel execution can create conflicting flow.py edits and persist an unclassified path.","finding_id":"agy-r5-session-start-order","fix":"Make 4.1 depend on 2.2 and state that synthetic AGY registration consumes 2.2's completed classifier before transcript selection, persistence, and idempotency.","location":"Phase 4 / § 4.1","prevention":"Compare exact production targets across deliverables and add dependency edges wherever shared-symbol semantics compose.","principle":"Deliverables that edit the same load-bearing symbol with interacting state semantics require an explicit dependency edge.","root_cause":"Sections 2.2 and 4.1 both change handle_session_start, but 4.1 depends only on P1 and can run before or concurrently with 2.2's path classifier.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"transcript-recovery-callers","description":"The live transcript reader directly calls find_transcript_on_disk, while watchdog and validation-evidence suites patch and assert caller-specific recovery behavior. Those owners are outside the target/test scope, so the split discovery contract can strand late recovery despite acceptance 2.2.6.","finding_id":"agy-r5-transcript-recovery-owners","fix":"Target TranscriptReader._ensure_transcript_path and the watchdog and validation-evidence recovery test suites. Specify the explicit caller context each adopts and pin cache, identity-change, invalid-path, attempted-path, fallback, and persistence behavior at the real callers.","location":"Phase 2 / § 2.2","prevention":"Resolve every live caller of a changed helper with gcode and map each caller plus its focused tests into Targets before claiming caller parity.","principle":"Every production consumer and caller-specific regression seam named by a changed shared contract must appear in Targets.","root_cause":"2.2 claims reader, watchdog, and validation-evidence recovery parity, but omits TranscriptReader._ensure_transcript_path and the dedicated watchdog and validation-evidence tests.","section_id":"2.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"append-sidecar-hydration","description":"Round 4's parser-state repair still cannot rehydrate an enlarged AGY sidecar after a result is appended beyond the saved call boundary. The real reconstruction gate rejects append growth for every source except Codex and is outside 4.2 Targets.","finding_id":"agy-r5-agy-sidecar-append-hydration","fix":"Target ProcessorLifecycleMixin._hydrate_registration_from_sidecar and its processor integration tests, admit verified append-only AGY growth, and exercise reconstruction after the result is appended beyond the saved boundary.","location":"Phase 4 / § 4.2","prevention":"Trace state persistence through write, load, admission, hydration, and resumed-read seams; target and test every gate on the reconstruction path.","principle":"Restart recovery for append-only state must cover both parser snapshots and the production sidecar admission gate.","root_cause":"4.2 targets processor_transcripts.py, while processor_lifecycle.py still calls load_index_sidecar with allow_append limited to source == \"codex\".","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"parser-helper-callers","description":"The Round 4 caller-inventory repair missed the live self-caller. Deleting _get_parser within the declared exact target leaves _parse_lines outside task scope and broken.","finding_id":"agy-r5-parser-helper-self-caller","fix":"Add src/gobby/sessions/transcript_parsing.py::_parse_lines explicitly or widen the file target with a scope reason, and state that _parse_lines migrates to transcripts.get_parser.","location":"Phase 2 / § 2.1","prevention":"Run exact-symbol usage analysis and include same-file callers before finalizing a deletion target inventory.","principle":"Deleting a helper requires targeting every direct caller, including callers in the helper's own module.","root_cause":"2.1 targets transcript_parsing.py::_get_parser but omits _parse_lines, which directly calls the helper slated for deletion.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"startup-probe-owner","description":"The repaired version-record order is not executable at the declared seam. runner_init/services.py cannot await get_cli_version, and using asyncio.run there would execute inside an active loop; runner.py or another awaitable pre-construction owner is absent from Targets.","finding_id":"agy-r5-version-probe-async-owner","fix":"Target an awaitable runner initialization owner and move support-dependent service construction behind the awaited probe, or make initialization async. Add a startup-order test proving publication precedes retained registry construction without nested event-loop execution.","location":"Phase 2 / § 2.5","prevention":"Trace startup requirements from async producer through the actual constructor/lifespan call graph and name the awaited owner before assigning targets.","principle":"An async startup prerequisite must have an awaitable owner that runs before synchronous consumers freeze derived state.","root_cause":"init_services and _init_llm_service are synchronous, and GobbyRunner calls them from its constructor while run_gobby already owns the active event loop.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"webchat-client-ownership","description":"The session-owned Codex repair omits the bootstrap split. Preserving the shared client violates per-session confinement; moving or stopping it breaks daemon hook/session synchronization. Existing shared-client tests and runner lifecycle owners remain outside Targets.","finding_id":"agy-r5-webchat-bootstrap-ownership","fix":"Define a daemon-owned Codex synchronization client plus a distinct per-session web-chat client factory. Target runner_init/servers.py, runner_lifecycle.py, WebChatRuntimeManager.__init__, and the runtime-manager ownership tests with daemon-preservation and per-session confinement cases.","location":"Phase 3 / § 3.1","prevention":"For each ownership migration, enumerate constructors, injected consumers, start/stop owners, and tests that assert instance identity.","principle":"Changing subprocess ownership requires updating every bootstrap, lifecycle, and test owner that shares the current instance.","root_cause":"Current runner bootstrap creates one daemon Codex client for both hooks/synchronization and WebChatRuntimeManager, while 3.1 moves only web-chat use to session ownership.","section_id":"3.1","severity":"blocking"},{"category":"missing-requirement","check_key":"watchdog-provider-coverage","description":"A spawned AGY session can gain a transcript parser yet remain excluded from provider-specific transcript completion and recovery diagnostics because KNOWN_WATCHDOG_PROVIDERS and _READERS omit AGY.","finding_id":"agy-r5-watchdog-provider-registry","fix":"Add an AGY watchdog reader based on Gate 0 transcript shapes, register it in the provider set and reader map, and target the registry/model tests. If that lifecycle surface is intentionally deferred, add a typed deferral instead of reporting full spawn support.","location":"Phase 6 / § 6.1","prevention":"When opening a provider capability gate, inspect every exhaustive provider registry and require an implementation or typed deferral for each lifecycle consumer.","principle":"Enabling a provider for spawned-agent execution requires parity across the closed lifecycle registries that determine completion and recovery.","root_cause":"The watchdog registry and its exhaustive test still classify AGY as unsupported, while 6.1 enables AGY spawning.","section_id":"6.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"resolved-contract-before-expansion","description":"Round 4's Gate 0 repair did not land. All downstream leaves are still derived before the live probes, and acceptance 1.1.11 promises an update operation the expansion system does not provide.","finding_id":"agy-r5-gate0-reconciliation-contract","fix":"Move live probes to a prerequisite plan/task outside this implementation manifest and derive the 17 implementation leaves only after fixtures are recorded. An alternative repair must explicitly reset and delete the original expansion, revise and re-review the plan, re-derive all leaves, and prevent any downstream dispatch before that transaction completes.","location":"Phase 1 / § 1.1","prevention":"Place contract probes outside implementation expansion or document the exact reset, delete, re-review, re-derive, and redispatch transaction before any dependent leaf can run.","principle":"Evidence that can change downstream acceptance must be resolved before leaf derivation, unless the workflow defines an executable atomic re-derivation path.","root_cause":"Section 1.1 remains a leaf in the same 17-entry manifest and promises downstream leaf updates, while apply_run creates all leaves atomically and only reset_expansion_output can remove them before re-derivation.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"synthetic-hook-response-commit","description":"Acceptance 4.1.10 remains unsatisfiable on translation failure: startup context can be persisted as injected before AGY receives injectSteps, after which a retry sees prior evidence and suppresses delivery. The repeated/pre-created path has the same eager marker.","finding_id":"agy-r5-startup-context-commit-boundary","fix":"Target both session-start handlers and context.py's claim/commit seam. Define a claim token committed after successful AGY response translation/envelope storage and rolled back on failure, with first, repeated, concurrent, pre-created, and failure-then-retry tests.","location":"Phase 4 / § 4.1","prevention":"For every exactly-once payload, identify claim, compose, translate, envelope, commit, rollback, replay, and concurrent-retry boundaries in Targets and tests.","principle":"Once-only response state must commit after the final fallible translation/envelope boundary and roll back on failure.","root_cause":"Both new-session and pre-created-session flows atomically claim and mark startup context before AgyAdapter translates the merged response, while 4.1 targets neither the context claim/commit seam nor handle_pre_created_session.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"agy-dual-timeout-contract","description":"An actively streaming turn can be terminated at 60 seconds even though every line resets the intended 120-second inactivity timer. Live fixtures also show --print-timeout can return exit code zero with an error payload, bypassing the plan's nonzero-exit handling.","finding_id":"agy-r5-agy-dual-timeout-contract","fix":"Characterize --print-timeout in Gate 0 and define one coordinated policy so the CLI whole-turn limit cannot preempt the inactivity contract. Test streaming beyond 60 seconds, CLI zero-exit timeout payloads, single terminal error emission, process-tree cleanup, lock release, and conversation-id preservation.","location":"Phase 5 / § 5.2","prevention":"Inventory every upstream and wrapper timeout, characterize its clock and result shape, then test precedence with activity extending beyond the shorter threshold.","principle":"Independent timeout clocks on one operation need an explicit precedence and cleanup contract.","root_cause":"5.2 hardcodes AGY --print-timeout 60s while also specifying a resettable 120-second per-line inactivity clock, and the upstream CLI timeout is an independent whole-turn mechanism.","section_id":"5.2","severity":"blocking"}],"reviewer_session":"406738e0-e8fe-45d1-8649-f6cf641ca761","round":5,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 6** `kind: verification`

- reviewer_run: cdf10e8d-b8ff-4276-91a5-07386c985026
- reviewer_session: 747c8a55-9b1b-41c5-964e-fe417d017052
- verdict: needs_review
- findings:
- agy-r6-gate0-self-reset-deadlock / blocking / reset_expansion_output guards make the in-manifest Gate 0 reconciliation unexecutable
- agy-r6-codex-bootstrap-test-owner / blocking / the shared-codex-client identity test in test_runner_lifecycle.py was outside 3.1's Targets
- agy-r6-ndjson-target-visibility / nit / .ndjson is invisible to semantic target normalization; .jsonl is recognized
- agy-r6-startup-context-finalization-owner / blocking / envelope persistence, timeout, and claim-storage owners for 4.1.10 were outside Targets
- agy-r6-watchdog-recovery-test-owner / blocking / the idle-recovery integration suite patching the resolver seam was outside 2.2's Targets
- agy-r6-run-gobby-startup-test-owner / blocking / existing run_gobby tests would execute the live version probe and were untargeted
- agy-r6-webchat-provider-switch-gate / blocking / handle_set_provider validates a closed five-provider set without agy
- agy-r6-agent-restart-resume-registry / blocking / SUPPORTED_RESUME_PROVIDERS excludes agy, breaking restart recovery for spawned AGY runs
- agy-r6-tool-chat-service-test-owner / blocking / the style-keyed ToolChatService suite was outside 6.2's provider-aware cache repair
- agy-r6-whole-turn-timeout-preemption / blocking / a finite whole-turn --print-timeout always eventually preempts a renewable inactivity clock
- resolution_notes: All 10 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — the reset guards
  (_validate_reset_targets rejecting claimed, committed, closed, isolated, and progressed
  targets while _target_task_ids covers the whole run), the shared-client identity test
  at tests/test_runner_lifecycle.py:313 and the GobbyRunner-only patching at :2056,
  .ndjson absent from _KNOWN_FILE_SUFFIXES (semantic_lint.py:62), execute_hook's nested
  mark_processed_and_return swallowing persistence failure plus _run_adapter_hook's
  uncancellable executor thread, the resolver-seam patch at
  test_lifecycle_monitor_watchdog_idle_recovery.py:702, the closed valid_providers set in
  handle_set_provider (session_config.py), SUPPORTED_RESUME_PROVIDERS at
  resume_executor.py:45, and the style-keyed fixtures throughout
  test_tool_chat_service.py. One attribution corrected during verification: the
  startup-context claim marker persists through update_terminal_pickup_metadata
  (storage/sessions/_terminal.py), not SessionVariableManager; the repair targets the
  verified seam. Repairs: Gate 0 restructured as a pre-expansion prerequisite — 1.1
  converted to kind: framing with probe-record IDs, the prerequisite task closes and
  disproof-driven revisions pass a fresh round before the 16-leaf implementation manifest
  derives once, every depends: P1 edge dropped (2.1, 2.3, 2.4, 2.5, 4.1, 5.1), and 1.1.11
  reworded to the pre-expansion transaction. The stream fixture renamed to
  stream-json-samples.jsonl (1.1.6). 3.1 targeted the runner-lifecycle bootstrap suite
  (3.1.11 reworded). 4.1 targeted execute_hook, _run_adapter_hook, and the
  terminal-pickup claim seam with the durable-token commit boundary (4.1.10 reworded).
  2.2 targeted the idle-recovery suite (2.2.9). 2.5 targeted the run_gobby entry-point
  tests (2.5.4 reworded). 5.3 admitted agy in handle_set_provider (5.3.4). 6.1 gained
  daemon-restart recovery through resume_executor gated on the 1.1.1 record (6.1.10).
  6.2 migrated the incumbent service suite to provider-aware identity (6.2.7 reworded).
  5.2's dual-clock policy became the two-branch 1.1.13 contract — a probe-proven disabled
  or unbounded form, or an explicit maximum-turn contract (question 9, 1.1.13, and 5.2.13
  reworded).

```json plan-review-round
{"evidence_id":"f816a420-a9a5-45d6-9568-ec67fe1d6c33","plan_hash":"d5ea4ee1b43b8ece91e4783a48613676bd98aff14547eccf40cf820d1324c219","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"149c90225d3eb43aec05443dc8751ccaed15a5e1029c93dd9657f335f1ee5deb","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":10,"total":14},"evidence_id":"f816a420-a9a5-45d6-9568-ec67fe1d6c33","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":17,"manifest_digest":"6dcca93dc355ebdc0c21e5f5f716ae312dc3564e493ac65b6acd06f9dcbf4804","status":"valid"},"source_digest":"d1aa87724331bd22e77de235384874c87c240e5563cd2746d5535917f23e4492","version":1},"findings":[{"category":"bad-sequencing","check_key":"gate0-expansion-self-reset","description":"The Round 5 Gate 0 repair cannot execute. Before 1.1 closes, its required reset includes 1.1 itself and fails the claimed/progressed guard; after fixture commit it also fails the commit guard.","finding_id":"agy-r6-gate0-self-reset-deadlock","fix":"Move the live AGY probe into a prerequisite outside the 17-entry implementation expansion. Complete and review its fixtures first, revise the plan from recorded outcomes, then derive and expand the downstream manifest once.","location":"Constraints / Phase 1 / § 1.1","prevention":"Trace reset target enumeration and every state guard against the executing task before assigning a rollback step to a generated leaf.","principle":"A reconciliation transaction must be executable from the task state in which the plan requires it.","root_cause":"Expansion records the generated 1.1 leaf in created_task_ids; reset_expansion_output targets that set and rejects claimed, progressed, committed, closed, or isolated tasks, while 1.1 must be claimed and progressed to produce its fixtures before closure.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"codex-bootstrap-existing-test-owner","description":"Acceptance 3.1.11 promises to split existing shared-instance tests, yet the actual shared-instance test is outside Targets. The leaf is incomplete and the current test directly contradicts the planned factory split.","finding_id":"agy-r6-codex-bootstrap-test-owner","fix":"Add tests/test_runner_lifecycle.py::* to 3.1 Targets and replace the shared identity assertion with cases proving HTTPServer retains the daemon synchronization client and WebChatRuntimeManager receives a factory that produces distinct session-owned clients.","location":"Phase 3 / § 3.1","prevention":"Use gcode to enumerate direct bootstrap tests and identity assertions whenever constructor ownership or lifetime changes.","principle":"An ownership split must target every existing contract test that asserts the superseded identity.","root_cause":"tests/test_runner_lifecycle.py::TestInitSubsystems.test_init_servers_wires_shared_codex_client_to_chat_backends requires HTTPServer and WebChatRuntimeManager to receive the same Codex client, while 3.1 Targets include only the runtime-manager suite.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"ndjson-target-coverage-visibility","description":"The stream fixture named by Target and acceptance 1.1.6 is invisible to semantic target coverage; the snapshot counts 94 normalized files while the raw Targets contain 95 distinct paths.","finding_id":"agy-r6-ndjson-target-visibility","fix":"Rename stream-json-samples.ndjson and every 1.1 reference to stream-json-samples.jsonl, preserving its NDJSON record format.","location":"Phase 1 / § 1.1","prevention":"Check every new fixture suffix against semantic target normalization and compare raw Target paths with normalized review-complexity inventory.","principle":"Every acceptance artifact should be visible to the repository's semantic target-coverage inventory.","root_cause":"semantic_lint.normalize_file_path recognizes .jsonl and omits .ndjson, so stream-json-samples.ndjson is discarded from target and mentioned-path accounting.","section_id":"1.1","severity":"nit"},{"category":"unhandled-edge","check_key":"startup-context-finalization-owner","description":"Acceptance 4.1.10 cannot be implemented within declared Targets. Translation success does not prove envelope storage, storage failure cannot trigger rollback at the adapter seam, and a timed-out adapter worker can finish late and strand or commit context after the caller received failure.","finding_id":"agy-r6-startup-context-finalization-owner","fix":"Add src/gobby/servers/routes/mcp/hooks.py::_run_adapter_hook and ::execute_hook, the SessionVariableManager claim storage seam, and their focused tests to 4.1. Use a durable owner token; commit only after envelope persistence succeeds, compare-and-roll back every earlier failure, and invalidate timed-out tokens before late executor completion.","location":"Phase 4 / § 4.1","prevention":"Trace compose, claim, translate, envelope persistence, response selection, timeout, late completion, commit, and rollback as one state machine before assigning Targets.","principle":"Exactly-once delivery state commits after the final fallible boundary, and a returned timeout invalidates any late worker's authority to commit.","root_cause":"The plan confines ownership to adapter and session-start helpers, while envelope persistence happens later in execute_hook, mark_processed_and_return swallows persistence failure, _run_adapter_hook can time out while its executor thread continues, and SessionVariableManager currently stores only an irreversible boolean claim.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"watchdog-recovery-integration-test-owner","description":"The Round 5 recovery-owner repair still omits a live watchdog integration seam. Changing the resolver to carry explicit caller context can break or stale this test while 2.2 remains nominally complete.","finding_id":"agy-r6-watchdog-recovery-test-owner","fix":"Add tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py::* to 2.2 Targets and re-anchor its stale-session case to the split caller-context contract while preserving the assertion that discovery does not mutate the session row.","location":"Phase 2 / § 2.2","prevention":"Search all test patches and direct calls to a changed helper or resolver, including integration suites outside the helper's package.","principle":"Every caller-specific regression test that patches a changed shared contract belongs in the owning Target inventory.","root_cause":"tests/agents/test_lifecycle_monitor_watchdog_idle_recovery.py::test_stale_session_discovers_transcript_without_updating_session_row patches watchdog.transcript_resolver.find_transcript_on_disk and asserts its current two-argument call, but the repaired Targets include only test_idle_check_transcript_paths.py.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"run-gobby-existing-test-owner","description":"The Round 5 publication-owner repair leaves the existing run_gobby test outside scope. It would execute the live AGY version probe during a unit test and still cannot prove publication precedes GobbyRunner construction.","finding_id":"agy-r6-run-gobby-startup-test-owner","fix":"Add tests/test_runner_lifecycle.py::* to 2.5 Targets. Patch the async version probe in the existing run_gobby test and assert probe completion and record publication occur before GobbyRunner construction.","location":"Phase 2 / § 2.5","prevention":"Enumerate direct callers and focused tests for every startup entry point before inserting an awaited prerequisite.","principle":"Adding an awaited external probe to an entry point requires updating every existing focused entry-point test and isolating the probe.","root_cause":"TestRunGobbyFunction.test_run_gobby_creates_runner patches only GobbyRunner and calls run_gobby directly; 2.5 inserts the AGY subprocess probe before construction but omits tests/test_runner_lifecycle.py from Targets.","section_id":"2.5","severity":"blocking"},{"category":"unhandled-edge","check_key":"webchat-provider-selection-parity","description":"AGY can be advertised and created by WebChatRuntimeManager while websocket clients still receive Invalid provider when switching an existing conversation to AGY.","finding_id":"agy-r6-webchat-provider-switch-gate","fix":"Add handle_set_provider and tests/servers/websocket/test_set_provider.py to 5.3 Targets, admit agy in the existing validation source, and test new-conversation selection plus switching, cancellation, teardown, pending-provider state, and confirmation.","location":"Phase 5 / § 5.3","prevention":"Search exhaustive provider sets, match statements, validators, and switch handlers whenever provider metadata or runtime support changes.","principle":"Enabling a provider backend requires updating every exhaustive provider-selection gate that can reach that backend.","root_cause":"src/gobby/servers/websocket/handlers/session_config.py::handle_set_provider validates against a closed set containing claude, grok, qwen, codex, and droid; AGY is absent and the handler plus its tests are outside Targets.","section_id":"5.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"agent-restart-resume-provider-parity","description":"Section 6.1 adds launch and watchdog coverage while leaving daemon-restart recovery unable to resume AGY. A daemon restart therefore breaks the claimed spawned-agent lifecycle.","finding_id":"agy-r6-agent-restart-resume-registry","fix":"Add resume_executor.py and tests/agents/test_resume_executor.py to 6.1 Targets. If Gate 0 proves terminal conversation resume, implement support-record-gated AGY recovery with the recorded argv, cwd, and native conversation id; otherwise add the explicit typed deferral and narrow the lifecycle claim.","location":"Phase 6 / § 6.1","prevention":"For each spawn provider, sweep launch, cancellation, watchdog, daemon restart, resume command construction, and native-session identity recovery.","principle":"A newly spawn-capable provider needs an explicit daemon-restart recovery contract or a typed deferral.","root_cause":"src/gobby/agents/resume_executor.py::SUPPORTED_RESUME_PROVIDERS remains a closed five-provider set, so restart reconciliation classifies every spawned AGY run as resume_unsupported_provider.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"tool-chat-provider-aware-service-tests","description":"The provider-aware cache repair is incomplete at its primary unit-test seam. A new end-to-end AGY contract test does not update the incumbent service fixtures or preserve non-AGY behavior.","finding_id":"agy-r6-tool-chat-service-test-owner","fix":"Add tests/ai/test_tool_chat_service.py to 6.2 Targets, migrate its adapters and factories to provider-aware identity, preserve non-CLI cases, and place Droid→AGY plus AGY→Droid same-instance cache-order regressions in that suite.","location":"Phase 6 / § 6.2","prevention":"Use gcode to enumerate constructors, fakes, fixture maps, cache assertions, and both ordering directions before changing identity semantics.","principle":"Changing cache identity, factory keys, or adapter-selection signatures requires targeting the existing focused service tests and fakes.","root_cause":"tests/ai/test_tool_chat_service.py builds ToolChatService adapters and factories keyed solely by AIAdapterStyle, while 6.2 changes selection and caching to (adapter_style, provider) and omits that suite.","section_id":"6.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"finite-whole-turn-vs-inactivity","description":"Acceptance 5.2.13 proves only that streaming exceeds the former 60-second value. A newly derived finite CLI timeout can still terminate an actively streaming turn that the wrapper considers healthy, so the stated governing-clock invariant remains false.","finding_id":"agy-r6-whole-turn-timeout-preemption","fix":"Require 1.1.13 to establish a probe-proven disabled or unbounded --print-timeout form and have 5.2 use it while retaining the renewable inactivity clock. If AGY exposes only a finite whole-turn limit, state that maximum-turn contract explicitly and test activity through its boundary.","location":"Phase 5 / § 5.2","prevention":"Classify every timeout as absolute or renewable and prove clock ordering across the full reachable duration, including activity beyond the absolute boundary.","principle":"A finite absolute deadline cannot remain subordinate to a renewable inactivity deadline for an unbounded healthy stream.","root_cause":"The Round 5 repair treats a --print-timeout set comfortably above one inactivity window as incapable of preemption, although a stream can reset the per-line clock continuously and eventually cross any finite whole-turn limit.","section_id":"5.2","severity":"blocking"}],"reviewer_session":"747c8a55-9b1b-41c5-964e-fe417d017052","round":6,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 7** `kind: verification`

- reviewer_run: bc75b9dd-35c6-4ed9-b4d0-8ce9ca27d287
- reviewer_session: a321b662-beca-484d-a9bb-2cac67f490be
- verdict: needs_review
- findings:
- agy-r7-gate0-approval-boundary / blocking / the probe scheduled "at build handoff" runs after approval already applies the 16-leaf manifest and expansion auto-advances with no prerequisite gate
- agy-r7-startup-claim-contract / blocking / the claim's atomic owner, schema, carrier, and storage tests were outside 4.1's Targets, and compose-time claiming leaves a queue-timeout-before-claim race
- agy-r7-timeout-envelope-replay / blocking / execute_hook terminalizes adapter timeout as a processed 2xx, so ghook deletes the inbox envelope and no retry can re-deliver startup context
- agy-r7-hooks-route-line-budget / blocking / hooks.py (966 lines) gained substantial 4.1 work with no Constraints budget or decomposition acceptance
- agy-r7-acp-lifecycle-consumers / blocking / ACPSessionLifecycleService drives close/delete through the warm shared ACP backend 3.1 kills, and was outside Targets
- resolution_notes: All 5 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — expansion_work_rule
  gating only on stage state with expansion in _AUTO_ADVANCE_NON_AGENT_STAGES
  (dispatch/rules.py:251), SessionVariableManager.claim_startup_context
  compare-and-setting the boolean variable (workflows/state_manager.py:668) beside the
  context_injected BOOLEAN column (postgres_baseline_schema.sql, session_models.py),
  translate_from_hook_response emitting only decision/reason/updatedInput,
  execute_hook's TimeoutError branch returning a graceful response through
  mark_processed_and_return with mark_envelope_processed
  (hooks/envelope_dedupe.py:258) while ghook deletes the inbox envelope on 2xx
  (crates/ghook/src/transport.rs) and daemon replay lives in hooks/inbox.py, hooks.py
  at 966 lines, and ACPSessionLifecycleService._require_available_backend obtaining
  the shared runtime-manager ACP backend (sessions/acp_lifecycle.py:186). Repairs: the
  Gate 0 probe became a pre-approval prerequisite — Constraints and §1.1 now anchor the
  probe-close and fresh-round ordering to the planning approval that applies the
  manifest, since expansion auto-advances from approval with no prerequisite-task gate
  (1.1.11 reworded). 4.1 targeted the claim's atomic owner, schema, model, envelope
  claim disposition, and their focused suites; the durable claim became a generation
  allocated before executor submission, carried privately through the HookResponse,
  and never emitted to AGY (4.1.10 reworded). Adapter timeout became a retryable
  envelope outcome — released claim, retryable non-2xx, ghook retention, daemon inbox
  replay, and late-worker fencing (4.1.12). hooks.py joined the Constraints six-file
  line budget with a named extraction seam (4.1.13). 3.1 targeted
  ACPSessionLifecycleService and its route/backend suites, moving close/delete to
  operation-owned clients under the target session's path and policy (3.1.12).

```json plan-review-round
{"evidence_id":"cc283a15-842a-44c1-824b-fb168062ba6f","plan_hash":"d360092001853e2db1d8537042ec88438596329efd95db08da80c5dc8bc613f6","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"eefa3666438c26c956f686745bad686539180d06cc9bfd6ffda38cc69f1d716b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":5,"total":8},"evidence_id":"cc283a15-842a-44c1-824b-fb168062ba6f","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"1f755a5e1961fb6736a7bb711a039c19e3ddb8c591dd34f273c59c79b7b57fd0","status":"valid"},"source_digest":"25294f44d0d53df0f0e5f1bc9183862ce74bec5e191b3f192da269a3e0f8668c","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"agy-r6-gate0-self-reset-deadlock","causal_section_ids":["1.1"],"check_key":"gate0-approval-boundary","description":"The framing-section parse is valid, §1.1 is excluded, all 16 implementation leaves derive, and the empty P1 heading creates no expansion phase. The new structure still cannot execute its stated probe-revise-re-review order: current approval applies those 16 leaves before build handoff, while the required fixture record remains unfinished.","finding_id":"agy-r7-gate0-approval-boundary","fix":"Create, execute, commit, and close the standalone §1.1 task before resubmitting this implementation plan for planning approval; record the probe artifacts, revise or type-defer affected branches, and obtain a fresh reviewed round before applying the 16-entry manifest. If automated, add a first-class pre-approval stage that blocks approval until this completes.","introduced_in_round":6,"location":"PRE-EXPANSION PREREQUISITE / § 1.1","prevention":"Trace every prerequisite across approval, manifest write, stage transition, build initialization, and expansion apply; require a machine-enforced predecessor before any irreversible downstream handoff.","principle":"A contract probe that can rewrite downstream acceptance must close before the approval transaction canonically derives and applies those downstream entries.","root_cause":"The Round 6 repair schedules the standalone probe at build handoff, while planning approval already applies the implementation manifest and the registered next stage immediately auto-applies expansion; no pre-approval owner creates or waits for the probe.","section_id":"1.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r6-startup-context-finalization-owner","causal_section_ids":["4.1"],"check_key":"startup-claim-state-propagation","description":"The proposed claim cannot safely identify the exact claimant for commit, rollback, or invalidation. It also claims at compose time inside executor work, leaving a queue-timeout-before-claim race where a late worker can create a fresh claim after the caller has returned.","finding_id":"agy-r7-startup-claim-contract","fix":"Target the actual atomic state owner and define one concrete durable generation/token representation with claim, commit, compare-and-rollback, and invalidate operations. Include required migration/baseline, Session model/from-row serialization, storage API/facade, state-manager and storage tests; allocate the generation before executor submission and carry private token plus canonical session id through HookResponse/AgyAdapter to execute_hook without emitting private fields to AGY.","introduced_in_round":6,"location":"Phase 4 / § 4.1","prevention":"For each durable claim contract, enumerate schema, model serialization, storage API/facade, atomic mutation owner, token propagation, timeout owner, and concurrency/storage tests before accepting route-level behavior.","principle":"Every durable token state machine must name its representation, atomic owner, private carrier, and compare-and-set transitions across each fallible boundary.","root_cause":"Section 4.1 specifies token behavior while the actual atomic owner still stores only _startup_context_injected=true, session persistence exposes only context_injected BOOLEAN, and AGY translation discards HookResponse.metadata before execute_hook. The state owner, schema/model, carrier, and focused storage tests are absent from Targets.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r6-startup-context-finalization-owner","causal_section_ids":["4.1"],"check_key":"adapter-timeout-envelope-replay","description":"Acceptance promises timeout invalidation lets a retry re-deliver startup context, yet the surrounding envelope state machine terminalizes the only invocation. The first AGY turn can therefore proceed without startup context and without any retained retry work.","finding_id":"agy-r7-timeout-envelope-replay","fix":"Define adapter timeout as a retryable envelope outcome: invalidate the exact dispatch generation, release rather than terminally process the envelope claim, return a retryable non-2xx response so ghook retains and replays the same inbox envelope, and discard late worker output. Add an end-to-end test covering timeout, inbox retention, replay, successful envelope persistence, and token commit.","introduced_in_round":6,"location":"Phase 4 / § 4.1 acceptance 4.1.10","prevention":"For every retryable failure, trace claim disposition, persisted marker, HTTP status, sender queue retention, replay, late-result fencing, and the end-to-end test across both sides of the transport.","principle":"A retry promise must preserve the original work item and expose a transport outcome that causes its owner to retry.","root_cause":"execute_hook currently converts adapter timeout into a normal response and marks the envelope processed; ghook treats the resulting 2xx as delivered and deletes the inbox item. Token invalidation alone neither replays that PreInvocation nor guarantees another one.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r6-startup-context-finalization-owner","causal_section_ids":["4.1"],"check_key":"near-ceiling-target-inventory","description":"Envelope finalization, rollback, timeout invalidation, retry handling, and late-worker fencing have only 34 lines of headroom in hooks.py, with no plan acceptance that keeps the file below the enforced ceiling.","finding_id":"agy-r7-hooks-route-line-budget","fix":"Add src/gobby/servers/routes/mcp/hooks.py to the Constraints line-budget inventory and add a 4.1 acceptance item requiring it to remain below 1,000 lines. Name same-task extraction of the adapter execution/envelope finalization claim-lifecycle seam if projected edits cross the ceiling.","introduced_in_round":6,"location":"Constraints / Phase 4 / § 4.1","prevention":"Whenever Targets change, rerun the source-size inventory for every hand-maintained production file and add a named extraction seam for each near-ceiling target.","principle":"Every newly targeted hand-maintained production file near the 1,000-line ceiling needs an explicit measured budget and same-task decomposition acceptance.","root_cause":"The Round 6 repair added _run_adapter_hook and execute_hook as substantial §4.1 targets, but the Constraints inventory and §4.1 acceptance still cover flow.py only; hooks.py is already 966 lines.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"acp-session-owner-consumers","description":"After removing the warm shared ACP client, close/delete for inactive or post-restart sessions have no specified session- or operation-owned client carrying the target session's resolved project path and SRT policy. Preserving the current path would retain an unconfined daemon-owned process.","finding_id":"agy-r7-acp-lifecycle-consumers","fix":"Target src/gobby/sessions/acp_lifecycle.py, tests/servers/websocket/chat/test_provider_backends.py, and tests/servers/routes/test_sessions_acp_routes.py. Define how close/delete obtain an operation- or session-owned client with the target session's final project path and SRT policy, preserve capability and cleanup behavior, and prove no warm shared ACP subprocess remains.","location":"Phase 3 / § 3.1","participating_section_ids":["3.1"],"prevention":"Use gcode caller and consumer traversal for every lifetime owner; inventory constructors, lifecycle methods, route services, fakes, and focused tests before replacing shared state.","principle":"A process-ownership refactor must trace every consumer of the old owner, including operational lifecycle routes outside the primary chat-turn path.","root_cause":"Section 3.1 traces attach/send and daemon startup, while ACPSessionLifecycleService still obtains the runtime manager's shared ACP backend and invokes close/delete through it. That service and its provider-backend and route regressions are outside Targets.","section_id":"3.1","severity":"blocking"}],"reviewer_session":"a321b662-beca-484d-a9bb-2cac67f490be","round":7,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 8** `kind: verification`

- reviewer_run: bc0790b5-5219-475f-b938-16a63b796f19
- reviewer_session: de19f88b-c557-4a16-95fe-e55d0d250844
- verdict: needs_review
- findings:
- agy-r8-gate0-prerequisite-unmet / blocking / the §1.1 pre-approval probe task remains unexecuted — fixtures are 1.0.x-era with no stream-json-samples.jsonl, so approval would derive 16 leaves from unresolved contracts
- agy-r8-first-session-claim-order / blocking / the pre-submission claim had no canonical session identity on the first PreInvocation, which registers the session only inside the executor
- agy-r8-replay-response-sink / blocking / daemon inbox replay consumes only the HTTP status and discards response bodies, so a replayed injectSteps has no live consumer yet 4.1.12 committed the generation on redelivery
- agy-r8-timeout-worker-overlap / blocking / releasing the envelope claim at timeout let replay overlap the uncancellable worker's stateful effects — duplicate BEFORE_AGENT execution was reachable
- agy-r8-acp-workspace-recovery / blocking / inactive/post-restart close/delete could not reconstruct the confinement root from durable state, and operation-client finalization was unspecified
- agy-r8-webchat-default-consumers / blocking / test_daemon_sandbox.py asserts and both sandbox guides document the old provider-native default, all untargeted
- agy-r8-claim-migration-inventory / blocking / the claim column had a baseline target but no numbered migration, and incumbent hook/storage suites hardcode the eager boolean
- agy-r8-session-lifecycle-test-owners / blocking / the ACP lifecycle, Claude chat-session, and websocket session suites encode the superseded ownership and were untargeted
- agy-r8-version-probe-lock-order / blocking / the probe was not anchored after PID ownership resolution, so losing daemon invocations could launch it
- agy-r8-provider-registry-parity / blocking / the fire-lifecycle parity matrix and DEFAULT_PLAN_KEYSTROKES enumerate only the five incumbents, leaving AGY outside managed-provider parity
- resolution_notes: All 10 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — the 1.0.x fixture state
  in tests/fixtures/provider_contracts/agy/, claim_startup_context(session_id) as
  session-backed (state_manager.py:668), inbox.py consuming only response.status_code,
  resolve_session_workspace requiring session/task managers (session_changes.py:140)
  with no durable workspace field on Session, the old-default assertions at
  test_daemon_sandbox.py:17-19, migrations numbered through 366 with eager
  context_injected encodings in tests/hooks/ and tests/storage/sessions/, the PID-lock
  contention exit at runner.py:250/385, the five-provider parametrization at
  test_fire_lifecycle_parity.py:98, and DEFAULT_PLAN_KEYSTROKES
  (plan_keystrokes.py:589) with no agy row. Repairs: the Gate 0 finding required no
  artifact edit — the pre-approval ordering is already the plan's contract and the
  unmet prerequisite is execution state; the standalone §1.1 probe task is the
  coordinator's first action after review-text convergence, before any approval
  submission. 4.1's pre-submission allocation became resolve-or-register — the
  canonical session is resolved or idempotently registered by provider plus
  conversationId before executor submission, then the generation is claimed against
  the real session id (4.1.10 reworded). Timeout replay was split into two promises:
  replay is daemon-side event recovery that never commits the generation or fabricates
  a provider response; startup context re-delivers on the next live PreInvocation, and
  envelope-claim disposition moved to worker-exit finalization on the shielded executor
  future so replay cannot overlap a live worker (4.1.12 reworded). The claim schema
  gained the numbered-migration path and the incumbent hook/storage suites (4.1.14).
  3.1's ACP close/delete gained durable workspace recovery via resolve_session_workspace
  with injected managers plus every-branch client finalization (3.1.12 reworded), the
  daemon-sandbox default suite (3.1.13), and the three direct lifecycle suites (3.1.14).
  2.5 anchored the probe after PID ownership resolution with the contention-branch test
  (2.5.4 reworded). 5.3 joined the managed web lifecycle parity matrix (5.3.5). 6.1
  gained the terminal plan-menu contract on the new 1.1.14 probe record (question 10,
  6.1.11). 7.1 took ownership of both sandbox guides' default-boundary statements
  (7.1.4).

```json plan-review-round
{"evidence_id":"195816d4-5ea0-422e-a2b1-7684f4b0200e","plan_hash":"334babcf9568c5078c2e7c3e5efa1d3714ed3ea7c57aac7a2c9ca833bd896c55","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"6ca784de33a1a183cb1535a81372f3f759d890d24a1357d0edb3864a8d21ea31","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":10,"total":13},"evidence_id":"195816d4-5ea0-422e-a2b1-7684f4b0200e","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"59b36657feb6c3ce79bc8cedde7fb7187ad21b1bdc1ac9a73343242af0f82028","status":"valid"},"source_digest":"c7cf941ae7b9d71afd00b2d2c13e96191efaf98f319dee632bdd65a6208f047b","version":1},"findings":[{"category":"bad-sequencing","causal_finding_id":"agy-r7-gate0-approval-boundary","causal_section_ids":["Constraints","1.1"],"check_key":"gate0-preapproval-prerequisite-state","description":"The ordering language is internally consistent, including 1.1.11 and the expansion auto-advance boundary, but its prerequisite is unsatisfied. The repository still has older 1.0.x AGY fixtures, snake_case shape-only hook payloads, and no stream-json-samples.jsonl; read-only task lookup found no matching standalone §1.1 task. Approving this snapshot would derive all 16 implementation leaves from unresolved contracts.","finding_id":"agy-r8-gate0-prerequisite-unmet","fix":"Create, execute, commit, and close the standalone §1.1 task; commit the live 1.1.9 fixtures and outcome table; revise or type-defer every affected downstream section; then prepare a new immutable snapshot and fresh review round.","introduced_in_round":7,"location":"Gate 0 / § 1.1 and Constraints","prevention":"Before opening an approval round, verify the named prerequisite task is closed with a linked commit and every promised artifact exists at the reviewed revision.","principle":"A declared pre-approval prerequisite must be completed in repository and task state before the approval review that can materialize dependent leaves.","root_cause":"Round 7 repaired the ordering text, while the coordinator resubmitted the implementation plan before creating and closing the standalone probe task and committing its outputs.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"agy-r7-startup-claim-contract","causal_section_ids":["4.1"],"check_key":"startup-claim-pre-registration-order","description":"SessionVariableManager.claim_startup_context requires a canonical session ID, and session_variables is session-backed. On the first AGY PreInvocation, handle_session_start registers that canonical row inside AgyAdapter.handle_native after executor submission. The specified pre-submission generation therefore has no executable identity or durable owner on the first-event and queue-timeout paths.","finding_id":"agy-r8-first-session-claim-order","fix":"Choose and target one executable transition: resolve or idempotently register the canonical AGY session before executor submission and then claim its generation, or allocate an independent durable envelope/external-identity generation and atomically bind it after registration. Add explicit first-event, pre-created-session, and queue-timeout-before-worker tests.","introduced_in_round":7,"location":"Phase 4 / § 4.1","prevention":"Walk first-event identity creation, claim allocation, queue timeout, worker start, commit, rollback, and retry in temporal order before accepting an atomic-claim design.","principle":"A durable claim must have a resolvable identity and storage owner before the transition that allocates it.","root_cause":"The repair requires a session-scoped generation before executor submission even though the first AGY PreInvocation creates the canonical session only inside the executor's synthetic SESSION_START.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r7-timeout-envelope-replay","causal_section_ids":["4.1"],"check_key":"replay-provider-response-sink","description":"ghook treats the planned 503 retry response as fail-open, returns continue to AGY, and retains the inbox file. hooks/inbox.py later re-POSTs the envelope internally and uses only the HTTP status; it discards the response body. Any replayed injectSteps therefore cannot reach the departed AGY hook process, while 4.1.12 would still commit the startup generation and permanently record undelivered context as delivered. The generic TimeoutError branch also leaves this hazard open for non-AGY response-bearing hooks.","finding_id":"agy-r8-replay-response-sink","fix":"Remove daemon-only replay as proof of AGY response delivery. Either add a bounded live provider-side retry that returns injectSteps to the same hook process, wait under an explicit response-bearing hook contract, or leave the generation uncommitted and deliver on a later live PreInvocation. Add transport-level tests that assert the actual response consumer and non-AGY behavior.","introduced_in_round":7,"location":"Phase 4 / § 4.1 acceptance 4.1.12","prevention":"For every replay path, trace both the request and response to their final consumer before committing delivery state.","principle":"Delivery state may commit only after the response reaches the live provider invocation that consumes it.","root_cause":"The repair equates daemon-side envelope replay with provider response delivery.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r7-timeout-envelope-replay","causal_section_ids":["4.1"],"check_key":"timeout-worker-replay-exclusion","description":"Releasing the processing marker immediately on timeout lets inbox replay claim the same envelope while the uncancellable worker can still register the session, pulse activity, run workflow rules or webhooks, and consume pending messages. The proposed generation invalidation does not fence those effects, so duplicate BEFORE_AGENT execution remains reachable across AGY and any non-AGY source using the generic branch.","finding_id":"agy-r8-timeout-worker-overlap","fix":"Keep a distinct timed-out-worker-active marker and attach finalization to the shielded executor future so replay waits until the original worker exits, or add cooperative generation checks before every stateful hook boundary. Test a forced overlap with session registration, a non-idempotent rule, pending-message consumption, and activity mutation.","introduced_in_round":7,"location":"Phase 4 / § 4.1 acceptance 4.1.12","prevention":"For timeout replay, enumerate every stateful effect and prove mutual exclusion between the original worker and every retry.","principle":"One idempotency envelope must never be replayable while its prior execution can still perform stateful work.","root_cause":"Generation invalidation fences claim commit and returned output, while asyncio.wait_for leaves the executor thread and its other side effects running.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r7-acp-lifecycle-consumers","causal_section_ids":["3.1"],"check_key":"acp-operation-workspace-recovery","description":"ACPSessionLifecycleService receives SessionManager and WebChatRuntimeManager, while Session has project_id but no final project_path/worktree identity and the live worktree override is in-memory. The existing resolve_session_workspace helper needs task context that the route does not supply. Thus inactive or post-restart close/delete cannot satisfy the promised final-worktree SRT confinement, and cleanup after ACP or storage failures is unspecified.","finding_id":"agy-r8-acp-workspace-recovery","fix":"Define and target durable workspace recovery by reusing resolve_session_workspace with injected task/project dependencies or persisting an equivalent identity; update the ACP route constructor and tests/sessions/test_acp_lifecycle_service.py; acquire the operation client through try/finally or an async context manager and test repo/worktree success plus every failure branch.","introduced_in_round":7,"location":"Phase 3 / § 3.1 acceptance 3.1.12","prevention":"For each post-restart lifecycle operation, trace durable workspace identity, constructor dependencies, acquisition, and cleanup across success and every failure.","principle":"A post-restart operation-owned process must reconstruct its confinement root from durable state and must be torn down on every terminal branch.","root_cause":"The repair names operation-owned ACP clients without defining how inactive sessions recover final repo or worktree paths or how the temporary client is finalized.","section_id":"3.1","severity":"blocking"},{"category":"traceability","check_key":"webchat-default-consumer-inventory","description":"tests/config/test_daemon_sandbox.py directly asserts the old provider-native and network-enabled defaults. docs/guides/sandboxing.md and docs/guides/sandbox-compatibility.md document the same old boundary. None is targeted, so implementation either leaves focused tests failing or ships stale security guidance.","finding_id":"agy-r8-webchat-default-consumers","fix":"Add tests/config/test_daemon_sandbox.py to §3.1 and both sandbox guides to §7.1. Update default assertions and examples while preserving explicit provider-native override coverage.","location":"Phase 3 / § 3.1 and Phase 7 / § 7.1","prevention":"Blast-radius every changed default through tests, examples, configuration docs, and override guidance.","principle":"A security-relevant default migration must update every direct assertion and durable operator-facing statement of that default.","root_cause":"The plan changes DaemonConfig behavior while targeting the new launch matrix and AGY research docs, leaving incumbent configuration and sandbox documentation outside the inventory.","section_id":"3.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r7-startup-claim-contract","causal_section_ids":["4.1"],"check_key":"startup-claim-schema-upgrade-inventory","description":"Already-baselined PostgreSQL databases return from baseline adoption and receive later columns only through numbered migrations, yet §4.1 has no migration target. Existing tests/hooks/conftest.py, tests/hooks/test_handler_execution.py, tests/hooks/test_session_start_handlers.py, and focused session storage tests also hardcode the literal claim or eager context_injected transition and are absent.","finding_id":"agy-r8-claim-migration-inventory","fix":"Add the next numbered sessions claim-generation migration, tests/storage/test_migration_contract.py coverage, and explicit targets for the incumbent hook and session-storage suites. Keep the flattened baseline synchronized and rewrite eager-marker assertions around claim, commit, rollback, and invalidate boundaries.","introduced_in_round":7,"location":"Phase 4 / § 4.1","prevention":"For each new column, inventory baseline, numbered migration, migration contract, row model, mutation API, handler fixtures, and focused storage tests.","principle":"Every persistent schema change needs both a fresh-install baseline and a numbered upgrade migration, plus all direct storage and handler contract tests.","root_cause":"The Round 7 inventory added only postgres_baseline_schema.sql and the new workflow test, overlooking the already-baselined upgrade path and incumbent fakes that encode eager boolean behavior.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"agy-r7-acp-lifecycle-consumers","causal_section_ids":["3.1"],"check_key":"session-owned-process-test-owners","description":"tests/sessions/test_acp_lifecycle_service.py fakes runtime_manager.acp_backend, tests/servers/test_chat_session.py owns Claude start/stop and option cleanup behavior, and tests/servers/websocket/chat/test_session.py owns the hydration/start seam. All three contracts change under §3.1 and none appears in Targets.","finding_id":"agy-r8-session-lifecycle-test-owners","fix":"Add those three suites as explicit §3.1 targets and migrate the ACP operation-client fake, Claude shim cleanup assertions, and post-hydration launch-order cases alongside the new launch-contract matrix.","introduced_in_round":7,"location":"Phase 3 / § 3.1","prevention":"After blast-radius analysis, target every direct constructor fake, start/stop assertion, and launch-order test for a changed lifetime owner.","principle":"A process-ownership rewrite must migrate the focused suites and fakes that directly encode the old ownership and launch order.","root_cause":"The plan added a new cross-provider matrix and two downstream suites while omitting direct ACP lifecycle, ChatSession, and post-hydration implementation tests.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"version-probe-pid-ownership-order","description":"Both insertion before PID ownership and insertion immediately before GobbyRunner satisfy the written acceptance. The former launches AGY from losing daemon invocations and violates the intended single startup owner; tests/test_runner_lifecycle.py does not cover the contention branch in tests/test_runner_pid_file.py.","finding_id":"agy-r8-version-probe-lock-order","fix":"State the order as PID ownership resolution, AGY probe/publication, then GobbyRunner construction. Add tests/test_runner_pid_file.py to Targets and assert the probe is never called when lock acquisition loses.","location":"Phase 2 / § 2.5","prevention":"For every startup probe, test ownership acquisition, contention, probe execution, publication, constructor freeze, and shutdown ordering.","principle":"Startup side effects belong only to the process that has acquired daemon ownership.","root_cause":"The plan orders the AGY probe before GobbyRunner construction without anchoring it after run_gobby's early PID-lock contention return.","section_id":"2.5","severity":"blocking"},{"category":"missing-requirement","check_key":"managed-provider-exhaustive-parity","description":"tests/servers/test_fire_lifecycle_parity.py enumerates the five incumbent managed web providers, and DEFAULT_PLAN_KEYSTROKES plus its all-CLIs test claims exhaustive managed CLI coverage over the same five. The plan enables AGY web chat and terminal spawn without targeting either inventory or probing AGY's terminal plan-menu contract.","finding_id":"agy-r8-provider-registry-parity","fix":"Add AGY to the managed web lifecycle parity matrix. Extend Gate 0 with a terminal plan-menu probe and target DEFAULT_PLAN_KEYSTROKES plus tests/adapters/test_plan_keystrokes.py, or type-defer terminal plan control before advertising complete spawn parity.","location":"Phase 5 / § 5.3 and Phase 6 / § 6.1","prevention":"For each provider enablement, enumerate closed capability, lifecycle, keystroke, resume, watchdog, and spawn registries and test every omission or deferral.","principle":"A newly complete managed provider must join every closed provider inventory or carry an explicit typed limitation.","root_cause":"Web-chat and spawn work add AGY without sweeping lifecycle parity and terminal plan-control registries.","section_id":"6.1","severity":"blocking"}],"reviewer_run":"bc0790b5-5219-475f-b938-16a63b796f19","reviewer_session":"de19f88b-c557-4a16-95fe-e55d0d250844","round":8,"round_number":8,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 9** `kind: verification`

- reviewer_run: caed5d98-9719-46d9-be8f-f75f204031b9
- reviewer_session: 105dce1b-ed38-4da3-8550-6b6ad9184c90
- verdict: needs_review
- findings:
- agy-r9-claim-incumbent-test-untargeted / blocking / a direct handler fixture pinning eager context_injected inside handle_session_start was untargeted
- agy-r9-terminal-auth-registry-gap / blocking / CLI_ENV_ALLOWLIST, CLI_CREDENTIAL_KEYS, and _SUPPORTED_AUTH_CLIS enumerate only the five incumbents
- agy-r9-precreated-session-double-registration / blocking / a provider+conversationId lookup can miss a pre-created child whose external_id is its Gobby session id and register a duplicate row
- agy-r9-claim-owner-adoption-missing / blocking / classify_session_start_context re-claims, so the owning worker would observe its own pre-claim as live and suppress full startup context
- agy-r9-critical-timeout-fail-open / blocking / ghook's retry-backpressure branch returns continue before the criticality check, failing open on critical lifecycle hooks
- agy-r9-envelope-lease-expires-live-worker / blocking / the fixed-age processing marker lets replay reclaim an envelope while a slow live worker still mutates state
- agy-r9-timeout-live-ack-missing / blocking / no acknowledgment proves ghook emitted the response; single-turn sessions strand the claim and pending messages are marked delivered pre-transport
- agy-r9-acp-workspace-recovery-unresolved / blocking / the two-branch workspace recovery was undecided and the resolver branch depends on active task claims that terminal transitions clear
- agy-r9-plan-keystroke-limitation-overbuilt / blocking / a typed limitation row in DEFAULT_PLAN_KEYSTROKES has no consumer; the direct negative contract is simpler
- agy-r9-agy-lifecycle-parity-hollow / blocking / the parity matrix exercises only BEFORE_AGENT and _fire_lifecycle hardcodes source="claude" for PRE_COMPACT
- agy-r9-presubmission-preflight-unbounded / blocking / the synchronous preflight in async execute_hook lacked offload, bounds, and cancellation disposition
- agy-r9-parser-alias-tests-untargeted / blocking / lifecycle and tokens-CLI suites patch module-local ClaudeTranscriptParser aliases outside 2.1's inventory
- resolution_notes: All 12 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — the eager-marker
  fixture at test_session_variable_preservation.py:566, the incumbent-only auth maps
  (auth_env.py:28/:87, tmux/spawner.py:36), registration keyed by (external_id,
  machine_id, project_id, source) at _crud.py:108 with the pre-created placeholder
  external_id at agents/session.py:165-200 and rebinding at flow.py:396-405, the
  classifier's claim call at context.py:61, the documented continue-even-on-critical
  retry branch at dispatch.rs:192-200, clear_stale_envelope_processing_marker's
  fixed-age clearing in envelope_dedupe.py, mark_delivered_batch at enrich time in
  event_enrichment.py:181, _resolve_isolated_workspace listing tasks by
  claimed_by_session_id with repo-root fallback (session_changes.py:140-179) and the
  task-manager-less _service constructor (routes/sessions/acp.py:55), the
  executable-only DEFAULT_PLAN_KEYSTROKES consumers with NativePlanActionService at
  native_plan_actions.py:73, source="claude" at _lifecycle.py:313, async execute_hook
  at hooks.py:596, the ClaudeTranscriptParser patches in test_lifecycle.py and
  test_tokens_cli.py, the update_terminal_pickup_metadata signature pin at
  test_sessions_import.py:179, and migrations ending at 366. Repairs: 2.1 targeted the
  two alias-patching suites and their registry-seam migration (2.1.7). 3.1 chose
  persisted workspace identity as the sole confinement contract — recorded at first
  workspace resolution, validated at use, failing closed, migration
  367_sessions_workspace_path.sql with model/CRUD/route/storage coverage (3.1.12
  reworded, 3.1.15). 4.1's pre-submission step became a transactional
  resolve-or-adopt-or-register honoring pre-created identity and the full uniqueness
  tuple, run as one bounded shielded preflight with owner-token adoption in
  classify_session_start_context (4.1.10 reworded); the retryable timeout now carries
  the 2.3-computed criticality action with ghook retention/action decoupling (4.1.12
  reworded), the processing marker became a verifiable ownership lease (4.1.16), and
  one-shot response effects — generation commit, pending-message delivery, injected
  context — stage behind a ghook delivery receipt with terminal-undelivered
  disposition (4.1.15); the claim migration renumbered to 368 and the incumbent
  fixture suites joined Targets (4.1.14 reworded). 5.3's parity row became behavioral:
  _fire_lifecycle passes the parsed provider into compaction context and a
  fake-stream AGY contract drives all five lifecycle events (5.3.5 reworded). 6.1
  gained the auth-inventory rows from the 1.1.9 record (6.1.12) and the direct-form
  plan-menu branch — executable registry row or NativePlanActionService negative
  contract, never an unread typed-limitation row (6.1.11 reworded).

```json plan-review-round
{"evidence_id":"91bde312-ad88-4c93-83fd-135e7cab4c8d","plan_hash":"466ce9d2fe3c00d4ebccc668dce2ec59c6b6b11bf5496258acae8a1bb231e1cc","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"26f2436f2989fbef6d1bdebf1cdb9bec513b4b9cc673de26b80b8fbae69c0fe1","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":12,"total":20},"evidence_id":"91bde312-ad88-4c93-83fd-135e7cab4c8d","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":9,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"b152146cdab66c160d3b70c35453caa286f250f4d622fb74980ea4b885302ac2","status":"valid"},"source_digest":"37c3807e223b4c67b3b54e63f0c55b18729ad7726cc5fced8430670992aabff8","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r8-claim-migration-inventory","causal_section_ids":["4.1"],"check_key":"startup-claim-incumbent-fixtures","description":"The repaired two-phase generation contract conflicts with an untargeted incumbent test that pins eager delivery. Implementing §4.1 will either leave the old eager path intact or break an unplanned suite.","finding_id":"agy-r9-claim-incumbent-test-untargeted","fix":"Add tests/hooks/event_handlers/test_session_variable_preservation.py::* to §4.1 Targets. Rewrite the direct-handler case to assert an uncommitted owned generation after handle_session_start and assert _startup_context_injected plus Session.context_injected only after execute_hook confirms translation, response persistence, and the live-delivery commit. Add tests/storage/test_sessions_import.py::* if the public terminal-metadata update signature changes.","introduced_in_round":8,"location":"§4.1 Targets and 4.1.10; tests/hooks/event_handlers/test_session_variable_preservation.py::test_full_session_start_marks_startup_context_injected","prevention":"Search direct writes, mocks, and assertions for every field whose commit point moves; list each incumbent suite in Targets.","principle":"A state-transition contract change must migrate every incumbent test that directly asserts the old transition.","root_cause":"§4.1 inventories broad hook and storage suites but omits a direct handler fixture that asserts _startup_context_injected and Session.context_injected become true inside handle_session_start, before route translation and persistence.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r8-provider-registry-parity","causal_section_ids":["6.1"],"check_key":"agy-terminal-auth-closed-inventories","description":"§6.1 claims complete AGY terminal env wiring through get_terminal_env_vars, but the closed auth registries have no AGY classification. Depending on Gate 0 auth shape, credentials can be omitted, leaked from ambient env, or misclassified by generic tmux inference.","finding_id":"agy-r9-terminal-auth-registry-gap","fix":"Add src/gobby/agents/spawners/auth_env.py::*, src/gobby/agents/tmux/spawner.py::*, and tests/agents/spawners/test_auth_env.py::* to §6.1 Targets. Use probe record 1.1.9 to add the exact AGY env allowlist and credential keys; when AGY is file-only, add an explicit empty/negative AGY row and test that ambient credential env is stripped. Add AGY to _SUPPORTED_AUTH_CLIS only when auth_cli inference is required by an in-scope caller.","introduced_in_round":8,"location":"§§3.2 and 6.1 Targets; src/gobby/agents/spawners/auth_env.py; src/gobby/agents/tmux/spawner.py; tests/agents/spawners/test_auth_env.py","prevention":"For each new provider, trace the terminal environment builder through all provider-keyed allowlists, credential keys, inference sets, and parity tests.","principle":"Adding a spawn-capable provider requires parity across every closed authentication and credential-scrubbing inventory used by that spawn path.","root_cause":"The Round 8 registry sweep covered watchdog, resume, plan keystrokes, and sandbox policy, while CLI_ENV_ALLOWLIST, CLI_CREDENTIAL_KEYS, _SUPPORTED_AUTH_CLIS, and their tests still enumerate only incumbent providers.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-first-session-claim-order","causal_section_ids":["4.1"],"check_key":"resolve-register-precreated-adoption","description":"The minimal pre-submission lookup can miss a pre-created child and register a second row. The later pre-created-session branch can then collide while rebinding external_id or leave parent/child linkage on an orphan row.","finding_id":"agy-r9-precreated-session-double-registration","fix":"Define one transactional resolve-or-adopt helper for §4.1. It must first honor the internal pre-created Gobby session id carried by terminal/session context, then resolve the full persisted uniqueness tuple for ordinary sessions, bind conversationId onto the adopted row, and return that row's canonical id for the generation claim. Target both registration branches and test repeated plus concurrent first hooks for pre-created and ordinary sessions.","introduced_in_round":8,"location":"§4.1 resolve-or-register text and 4.1.10; src/gobby/agents/spawn.py; src/gobby/agents/session.py; src/gobby/hooks/event_handlers/_session_start/flow.py; src/gobby/storage/sessions/_crud.py","prevention":"Before moving registration earlier, enumerate all creators and identity mutations, then test adoption and uniqueness for every initial identity form.","principle":"Pre-registration must use the canonical identity and adoption rules of every existing session-creation path.","root_cause":"§4.1 calls provider plus conversationId the existing idempotency key, but storage uniqueness also includes machine, source, project, and session type, while a spawned pre-created child initially uses the Gobby session id as external_id and binds the native conversation id during session start.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-first-session-claim-order","causal_section_ids":["4.1"],"check_key":"preclaimed-generation-owner-adoption","description":"The owning worker's second claim observes an existing live generation and can classify itself as live, suppressing the full startup context that the generation was created to deliver.","finding_id":"agy-r9-claim-owner-adoption-missing","fix":"Pass an internal expected generation and owner token from execute_hook into the synthetic SESSION_START metadata. Change classify_session_start_context and its atomic helper so a matching owner adopts the preclaim and returns full, while nonowners attempt/observe the claim and return live. Cover ordinary, pre-created, repeated, concurrent, invalidated, and late-worker cases; keep the token out of AGY payloads.","introduced_in_round":8,"location":"§4.1 resolve-or-register text and 4.1.10; src/gobby/hooks/event_handlers/_session_start/context.py::classify_session_start_context; src/gobby/hooks/event_handlers/_session_start/flow.py","prevention":"Trace claim ownership through every wrapper and classifier; distinguish owner adoption from competitor claim attempts in the API and tests.","principle":"A two-phase claim has one owner; downstream classification must adopt that ownership instead of attempting the claim again.","root_cause":"The repaired text preclaims a generation before executor submission, while classify_session_start_context still calls claim_startup_context for both new and pre-created session-start flows.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-timeout-worker-overlap","causal_section_ids":["4.1"],"check_key":"retry-retention-critical-disposition","description":"A timeout on a critical lifecycle hook retains the envelope yet tells the provider to continue. This regresses the incumbent fail-closed safety contract for AGY and non-AGY hook sources sharing the generic route.","finding_id":"agy-r9-critical-timeout-fail-open","fix":"Specify and target the ghook dispatch change that decouples retention from action: retryable 503 retains the inbox envelope; critical hook types emit the existing blocking/fail-closed provider result; noncritical hooks emit continue. Add AGY and incumbent-provider tests for both classes, including timeout before worker start and timeout with a running worker.","introduced_in_round":8,"location":"§4.1.12 and §2.3 fail-open policy; crates/ghook/src/dispatch.rs::run_gobby_owned; src/gobby/servers/routes/mcp/hooks.py","prevention":"For every retryable transport outcome, assert both envelope retention and the provider-visible action for critical and noncritical hook classes.","principle":"Transport retry state and provider safety disposition are independent outcomes.","root_cause":"§4.1 uses a generic retryable non-2xx response to retain the envelope, and ghook's retryable-503 branch returns continue before applying the existing critical-hook fail-closed policy.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-timeout-worker-overlap","causal_section_ids":["4.1"],"check_key":"live-worker-envelope-lease","description":"After the marker age elapses, inbox replay can claim the same envelope while the original worker is still mutating session state. Worker-exit finalization then races a second execution, violating dedupe for AGY and all generic hook sources.","finding_id":"agy-r9-envelope-lease-expires-live-worker","fix":"Replace fixed-age ownership with a verifiable lease: assign a worker/daemon owner token, renew it while the shielded future is live, permit reclaim only after lease expiry plus failed owner-liveness validation, and finalize with compare-and-set on the same token. Add tests/servers/test_mcp_routes.py::* to §4.1 Targets and force a worker past the current grace period; also cover daemon-death recovery and losing-owner finalization.","introduced_in_round":8,"location":"§4.1.12; src/gobby/hooks/envelope_dedupe.py; src/gobby/hooks/inbox.py; tests/servers/test_mcp_routes.py","prevention":"Test ownership beyond every configured grace period and distinguish slow live owners from dead daemon instances.","principle":"An uncancellable worker must retain exclusive ownership until worker-exit finalization or verifiable owner death.","root_cause":"The repaired worker-exit contract still relies on a fixed-age processing marker that stale recovery can clear while a shielded worker remains alive.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-replay-response-sink","causal_section_ids":["4.1"],"check_key":"provider-visible-delivery-ack","description":"A single-turn session can time out or disconnect after the daemon persisted a late response. No next PreInvocation exists to recover startup context, while startup context, pending messages, and rule-injected context can be lost permanently and the generation/envelope can remain semantically unresolved.","finding_id":"agy-r9-timeout-live-ack-missing","fix":"Add an opaque delivery receipt to the persisted response. Have ghook acknowledge it only after successful response parse and stdout emission; stage startup-generation commit and all one-shot response effects, including pending-message delivery, behind that acknowledgment. On transport loss, release them for the next live hook; on STOP/session expiry with no next hook, record a terminal-undelivered disposition and retire the claim. Test a single-turn timeout, disconnect after server persistence, no subsequent PreInvocation, acknowledged delivery, and pending-message rollback.","introduced_in_round":8,"location":"§4.1.10-4.1.12; src/gobby/servers/routes/mcp/hooks.py; crates/ghook/src/dispatch.rs; src/gobby/hooks/event_enrichment.py::EventEnricher._inject_pending_messages","prevention":"Model produced, persisted, emitted, acknowledged, and terminal-undelivered states separately; test sessions with no later invocation.","principle":"Worker completion and server persistence cannot stand in for delivery to the live hook process.","root_cause":"§4.1 terminalizes the envelope at worker exit and relies on a later PreInvocation for startup re-delivery, yet no acknowledgment proves ghook parsed and emitted the response, and response-bound pending messages are marked delivered inside EventEnricher before transport succeeds.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r8-acp-workspace-recovery","causal_section_ids":["3.1"],"check_key":"durable-acp-confinement-root","description":"After task release or daemon restart, close/delete can fall back from the original worktree to the repository root, violating the repaired confinement guarantee. The plan also cannot be implemented deterministically because its two branches have different schemas and constructor blast radii.","finding_id":"agy-r9-acp-workspace-recovery-unresolved","fix":"Choose persisted session-owned workspace_path as the contract. Record the canonical resolved workspace when the session is created or adopted; close/delete must use that value, validate its project/worktree confinement, and fail closed when it is absent or invalid instead of falling back to repo root. Target the Session model, CRUD/hydration, ACP production route/service, storage tests, migration contract, and lifecycle suites. Allocate migration 367 to sessions.workspace_path, renumber §4.1's startup-generation migration to 368, and test closed, released, escalated, deleted-worktree, and post-restart cases with every-branch client finalization.","introduced_in_round":8,"location":"§3.1 ACP close/delete design and Targets; src/gobby/servers/session_changes.py::resolve_session_workspace; src/gobby/sessions/acp_lifecycle.py; src/gobby/servers/routes/sessions/acp.py::_service","prevention":"Resolve architectural alternatives before approval; test durable identity after release, closure, escalation, restart, and missing-path recovery.","principle":"A plan must choose one durable recovery contract, and confinement must never depend on active-only task state.","root_cause":"§3.1 leaves resolver injection or persisted workspace identity as alternatives. resolve_session_workspace derives worktrees from active task claims that terminal transitions clear, and the sole production ACP service constructor lacks the task-manager dependency required by that branch.","section_id":"3.1","severity":"blocking"},{"category":"over-engineering","causal_finding_id":"agy-r8-provider-registry-parity","causal_section_ids":["6.1"],"check_key":"agy-no-menu-simple-contract","description":"If probe 1.1.14 finds no menu, implementing a typed registry row requires a new union type and consumer branching with no runtime requirement. Leaving AGY absent under the current all-CLIs guard is indistinguishable from a missed provider.","finding_id":"agy-r9-plan-keystroke-limitation-overbuilt","fix":"Use the direct form. For a proven menu, add the executable AGY mapping and end-to-end dispatch tests. For no menu, leave AGY out of DEFAULT_PLAN_KEYSTROKES, add a probe-backed AGY negative contract in NativePlanActionService/attached approval that returns the recorded refusal reason, and update the coverage guard to distinguish executable-menu providers from explicitly unsupported AGY. Add tests/communications/test_native_plan_actions.py::* and the attached plan-approval suite to §6.1 Targets.","introduced_in_round":8,"location":"§6.1 terminal plan-control branch and 6.1.11; src/gobby/adapters/plan_keystrokes.py; src/gobby/communications/native_plan_actions.py; plan-approval handlers","prevention":"For each proposed registry variant, identify a concrete reader and asserted behavior; otherwise use a direct negative test and provider-specific refusal.","principle":"A registry row must have an executable consumer; unsupported capability needs the simplest explicit negative contract.","root_cause":"§6.1 proposes a typed limitation row inside DEFAULT_PLAN_KEYSTROKES, whose types, import-time coverage, and consumers only represent executable mappings/resolvers. No named consumer reads a limitation value or reason.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"agy-r8-provider-registry-parity","causal_section_ids":["5.3"],"check_key":"managed-provider-lifecycle-behavior","description":"The proposed AGY row can pass while AGY never fires tool/stop callbacks and while PRE_COMPACT emits Claude context. Dependency ordering is satisfiable because §5.3 follows §5.2 and §2.5; the acceptance matrix is behaviorally incomplete.","finding_id":"agy-r9-agy-lifecycle-parity-hollow","fix":"Target ChatLifecycleMixin._fire_lifecycle and the AGY backend stream tests. Pass the parsed session provider into build_compaction_context. Add a fake-stream AGY contract that fires BEFORE_AGENT, BEFORE_TOOL, AFTER_TOOL, PRE_COMPACT, and STOP and asserts block decisions, injected context, modified input, and source; retain the provider-list test as a closed-registry guard for all incumbents.","introduced_in_round":8,"location":"§5.3.5 and Targets; §5.2 backend integration; src/gobby/servers/websocket/chat/_lifecycle.py::ChatLifecycleMixin._fire_lifecycle; tests/servers/test_fire_lifecycle_parity.py","prevention":"Build parity matrices from lifecycle event branches and response effects, then run them through each real backend seam.","principle":"A provider-parity row must execute provider-sensitive lifecycle behavior and assert returned effects.","root_cause":"§5.3.5 points to a test that covers only BEFORE_AGENT metadata, while the AGY backend lifecycle loop is not exercised and ChatLifecycleMixin._fire_lifecycle still hardcodes source='claude' when building PRE_COMPACT context.","section_id":"5.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r8-first-session-claim-order","causal_section_ids":["4.1"],"check_key":"async-preflight-cancellation","description":"Direct execution can block the server event loop under SQLite contention. A naïve offload creates a second uncancellable future that can register a session and leave an owned generation after the caller has returned.","finding_id":"agy-r9-presubmission-preflight-unbounded","fix":"Specify one bounded preflight future for resolve/adopt/register plus generation claim, awaited before main adapter submission. Shield it, enforce queue and execution bounds, and attach compare-and-invalidate cleanup when the request exits before preflight completion; idempotent minimal session registration may remain, while no live claim may remain ownerless. Add event-loop responsiveness, preflight queue-timeout, cancellation, late-success, and concurrent-first-hook tests.","introduced_in_round":8,"location":"§4.1 resolve-or-register pre-submission text; src/gobby/servers/routes/mcp/hooks.py::execute_hook and _run_adapter_hook","prevention":"For each sync database phase added to an async endpoint, specify executor, queue bound, timeout, cancellation, and late-future finalization.","principle":"Blocking preflight in an async route needs bounded scheduling, cancellation semantics, and late-completion cleanup.","root_cause":"The Round 8 repair requires execute_hook to synchronously resolve/register and claim before AdapterExecutor submission, but execute_hook is async and the plan specifies neither an offload boundary nor disposition when that preflight completes after request cancellation or timeout.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"parser-registry-mock-seams","description":"Registry routing can bypass these mocks, and deleting the aliases can make patches fail before behavior is tested. The plan's parser consumer inventory is incomplete.","finding_id":"agy-r9-parser-alias-tests-untargeted","fix":"Add tests/sessions/test_lifecycle.py::* and tests/cli/test_tokens_cli.py::* to §2.1 Targets. Migrate every module-local ClaudeTranscriptParser patch to the shared get_parser/registry seam, covering transcript processing, summary context, message loading, and token CLI behavior for Claude plus AGY.","location":"§2.1 Targets; src/gobby/sessions/transcript_processing.py; src/gobby/cli/tokens.py; tests/sessions/test_lifecycle.py; tests/cli/test_tokens_cli.py","prevention":"Search constructor aliases and monkeypatch targets before centralizing construction; migrate all tests to the shared seam in the same deliverable.","principle":"Replacing inline constructors with a registry must migrate every caller test that patches the old module-local constructor.","root_cause":"§2.1 removes or bypasses inline ClaudeTranscriptParser construction, while tests/sessions/test_lifecycle.py and tests/cli/test_tokens_cli.py patch transcript_processing.ClaudeTranscriptParser and tokens_module.ClaudeTranscriptParser and are absent from Targets.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"105dce1b-ed38-4da3-8550-6b6ad9184c90","round":9,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 10** `kind: verification`

- reviewer_run: 8e7109bb-a049-4a4f-9032-d84006e43b0e
- reviewer_session: b334834d-ca7f-4ce5-8973-7318a3e77b53
- verdict: needs_review
- findings:
- agy-r10-parser-registry-adjacent-mocks / blocking / test_summarize.py and test_token_tracker_attribution.py still patch Droid/Qwen/Claude/Codex parser constructors outside 2.1's inventory
- agy-r10-shared-contract-ordering / blocking / 4.1 consumed 2.3 criticality semantics and co-owned 3.1's schema stack with no dependency edges
- agy-r10-workspace-identity-writers / blocking / the spawn and hook-adoption writers that first resolve the persisted workspace were untargeted
- agy-r10-receipt-ack-protocol / blocking / the delivery receipt had no versioned wire type, dedicated idempotent consumer, or envelope-terminalization contract
- agy-r10-receipt-crash-semantics / blocking / output.rs discards write errors and death-after-write-before-ack makes exactly-once presentation unprovable
- agy-r10-receipt-terminal-incumbents / blocking / the direct enrichment suite, Stop handler, and both expiry owners were untargeted while incumbents keep eager delivery
- agy-r10-session-uniqueness-session-type / blocking / the real uniqueness key is five-part including session_type (idx_sessions_unique)
- agy-r10-retry-class-discriminator / blocking / adapter timeout reused the ingress-backpressure 503 shape ghook must keep answering with unconditional continue
- agy-r10-auth-probe-shape / blocking / record 1.1.9 covers domains and roots, not credential env names or auth inference — the auth rows had no evidence source
- agy-r10-precompact-trigger / blocking / no managed backend invokes _on_pre_compact, so the five-event AGY parity claim had no reachable PRE_COMPACT trigger
- agy-r10-attached-plan-negative-contract / blocking / handle_attached_plan_approval reads DEFAULT_PLAN_KEYSTROKES independently and would diverge from the negative contract
- resolution_notes: All 11 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — the remaining parser
  patches at test_summarize.py:1388/1437/1741 and
  test_token_tracker_attribution.py:75/101, the 4.1 header depending only on 2.2, the
  five-part idx_sessions_unique (postgres_baseline_schema.sql:362; migration 366:454),
  let _ write-error discards at output.rs:3-8, is_retry_backpressure matching
  503+status=retry at transport.rs:65, envelope.rs and inbox-envelope.v1.schema.json
  as the only wire types, liveness_monitor.py and lifecycle.py as expiry owners with
  handle_stop at _agent.py:556, _on_pre_compact declared at backends/base.py:138 and
  only wired at _session.py:478 with no invoker, and DEFAULT_PLAN_KEYSTROKES read
  independently at plan_approval.py:15/222. Repairs: 2.1 targeted both adjacent suites
  and broadened the sweep to every module-local parser-constructor patch (2.1.7
  reworded). 3.1 targeted the three first-resolution writers — spawn preparation,
  child-session registration, hook adoption — with a spawn-time persistence case
  (3.1.15 reworded). 4.1 now depends on 2.2, 2.3, and 3.1, ordering the shared
  contract.rs, session-schema, and 367-then-368 migration surfaces. The adoption
  identity became the five-part (external_id, machine_id, source, project_id,
  session_type) key with session_type preservation and terminal-versus-web_chat
  collision tests (4.1.10 reworded). The retryable response gained the stable
  retry_kind discriminator — ingress_backpressure keeps unconditional continue,
  adapter_timeout honors the 2.3-computed action (4.1.12 reworded). The delivery
  receipt became a versioned wire type (delivery-receipt.v1 schema beside
  envelope.rs) with receipt_id/original-envelope/session identity, a dedicated
  idempotent CAS consumer in the drain, duplicate-ack no-ops, no ack-of-ack, and
  envelope terminalization without re-execution; output.rs emission-plus-flush
  returns an I/O result gating ack enqueue; the crash window is stated honestly as
  at-least-once presentation with CAS-once effect commits; staged effects traverse
  prepared/acknowledged/released/terminal-undelivered states with the Stop handler
  and both expiry owners as terminal recorders, and the incumbent enrichment and
  provider suites migrate (4.1.15 reworded, 4.1.11 reworded). Gate 0 gained the
  authentication-footprint probe (question 11, 1.1.15) consumed by 3.2.4 and 6.1.12,
  and the compaction-signaling probe (question 12, 1.1.16) deciding 5.3's
  PRE_COMPACT branch (5.3.5 reworded). 6.1 extended the negative plan-menu contract
  to handle_attached_plan_approval so both plan-control paths return the recorded
  refusal or dispatch the same executable row (6.1.11 reworded).

```json plan-review-round
{"evidence_id":"5222c55a-042d-46ca-9a01-e263421496eb","plan_hash":"a82c14f465b5f071db841d5c8fdd46e9c20ad7c668ce5ead37fe290a3d7ae8dc","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"0aa41bba0af1a4d6559fe1dfa6767046f881517a8a7546d0436aaa4b1226b74f","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":3,"emitted_findings":11,"total":14},"evidence_id":"5222c55a-042d-46ca-9a01-e263421496eb","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"2eecfea8c3c9ae39334c79ab91cd259d657a1a71bcc0c16d89613a51f9d77ef5","status":"valid"},"source_digest":"ff86425efb82a37fd1e2b64cbdafc14050d6c4562ab4e6446049e8c2bf6d0cfd","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r9-parser-alias-tests-untargeted","causal_section_ids":["2.1"],"check_key":"parser-registry-mock-seams","description":"Registry routing in _build_summary_prompt_context and _process_session_transcript will bypass the remaining untargeted patches. The summary tests can execute real parsers, while token-attribution tests patch aliases that §2.1 deletes.","finding_id":"agy-r10-parser-registry-adjacent-mocks","fix":"Add tests/sessions/test_summarize.py::* and tests/sessions/test_token_tracker_attribution.py::* to §2.1 Targets. Migrate every Claude, Droid, Qwen, and Codex constructor patch in those suites to the shared get_parser/PARSER_REGISTRY seam, and broaden 2.1.7 from Claude-only wording to every bypassed parser-constructor patch.","introduced_in_round":9,"location":"§2.1 Targets and acceptance 2.1.7; tests/sessions/test_summarize.py; tests/sessions/test_token_tracker_attribution.py","prevention":"Search all parser-constructor patch targets across tests whenever parser construction moves behind a registry, then prove each patched seam remains observable.","principle":"Replacing direct construction with registry dispatch requires migrating every test patch aimed at constructors the registry has already captured or aliases the refactor deletes.","root_cause":"The Round 9 repair enumerated the reported Claude aliases in lifecycle and tokens tests, but the repository-wide constructor-patch sweep still finds Claude, Droid, and Qwen patches in test_summarize.py plus Codex and Qwen aliases in test_token_tracker_attribution.py.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"agy-r9-acp-workspace-recovery-unresolved","causal_section_ids":["3.1","4.1"],"check_key":"shared-contract-deliverable-ordering","description":"§4.1 can expand concurrently with §2.3 and §3.1. That permits conflicting edits to crates/ghook/tests/contract.rs and the session schema stack, and can land migration 368 before 367 despite text claiming the opposite.","finding_id":"agy-r10-shared-contract-ordering","fix":"Change §4.1 to depend explicitly on 2.2, 2.3, and 3.1. Regenerate the manifest and verify those edges transitively order contract.rs, session_models.py, _crud.py, the flattened baseline, migration-contract tests, and migrations 367 then 368.","introduced_in_round":9,"location":"§4.1 header (depends: 2.2); §2.3 and §3.1 Targets; migrations 367 and 368","prevention":"After every repair, intersect target sets and semantic inputs across deliverables; add dependency edges for shared files, numbered migrations, and consumed policy contracts.","principle":"A deliverable must depend on every predecessor whose behavior it consumes and every earlier leaf that owns the same numbered schema and shared contract surfaces.","root_cause":"Round 9 added §4.1 edits that consume §2.3 criticality semantics and co-own §3.1 session model, CRUD, baseline, migration-contract, and storage-test files without updating §4.1's dependency list.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r9-acp-workspace-recovery-unresolved","causal_section_ids":["3.1"],"check_key":"persisted-workspace-first-write-seams","description":"The column can exist and close/delete can fail closed while spawned worktree sessions and hook-adopted sessions never populate it. After task release or restart, those sessions become indistinguishable from missing-identity rows.","finding_id":"agy-r10-workspace-identity-writers","fix":"Add the child-session/spawn preparation and session-start adoption writers to §3.1 Targets, plus focused persistence tests. Pass the canonical workspace into registration or a transactional update at every named first-resolution seam, and prove pre-created spawn, ordinary hook adoption, and web-chat hydration all persist the same validated identity before later close/delete.","introduced_in_round":9,"location":"§3.1 persisted workspace prose and acceptances 3.1.12/3.1.15; src/gobby/agents/session.py; src/gobby/agents/spawn_executor.py; src/gobby/hooks/event_handlers/_session_start/flow.py","prevention":"For each persisted field, trace create, adopt, hydrate, update, consume, and recovery paths; target and test each distinct writer.","principle":"A persisted identity contract must target every producer that first resolves the value, not only its schema and downstream consumers.","root_cause":"The repair names spawn-time worktree resolution and session-start adoption as workspace writers, while §3.1 Targets cover storage and web-chat seams and omit the spawn and hook-adoption owners.","section_id":"3.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r9-timeout-live-ack-missing","causal_section_ids":["4.1"],"check_key":"delivery-receipt-ack-state-machine","description":"No specified route can distinguish an ack from a hook replay. A naïve implementation either fails validation, sends the ack through ordinary adapters, recursively generates receipts, or re-applies effects on duplicate direct and replayed acknowledgments.","finding_id":"agy-r10-receipt-ack-protocol","fix":"Add envelope.rs, a versioned acknowledgment schema, and src/gobby/hooks/inbox.py to §4.1 Targets. Define receipt_id, original_envelope_id, and canonical session identity; route acks to a dedicated CAS consumer; make duplicate direct and inbox-replayed acks no-ops; prevent ack-of-ack recursion; and specify how acknowledgment terminalizes the original envelope without re-executing its hook. Add drain, duplicate, restart, and original-envelope-finalization tests.","introduced_in_round":9,"location":"§4.1 Targets and acceptance 4.1.15; crates/ghook/src/envelope.rs; crates/ghook/schemas/inbox-envelope.v1.schema.json; src/gobby/hooks/inbox.py","prevention":"For every new inbox message kind, trace producer, schema, validator, durable storage, drain dispatcher, duplicate handling, terminalization, and recursion prevention.","principle":"A durable acknowledgment needs a versioned wire type, a dedicated idempotent consumer, and explicit interaction with the original envelope's terminal state.","root_cause":"The repair says ghook writes an ack through the inbox and the drain consumes it, but targets only dispatch/transport and tests. Current Rust and JSON envelope types encode ordinary hooks, and Python inbox validation routes every envelope to /api/hooks/execute.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r9-timeout-live-ack-missing","causal_section_ids":["4.1"],"check_key":"provider-output-before-ack-crash-window","description":"A failed or partial write can incorrectly commit effects, and a successful write followed by process death causes the next live hook to redeliver already-visible startup context or pending messages. The stated exactly-once outcome is therefore unprovable with the proposed receipt.","finding_id":"agy-r10-receipt-crash-semantics","fix":"Target output.rs and make emission plus flush return an I/O result. Strip receipt metadata before provider-specific action mapping and durably enqueue the ack only after a successful full write. Narrow provider-visible delivery to at-least-once unless AGY supplies a true consumption acknowledgment, while making generation commit, pending-message delivery, and injected-context effects idempotent under duplicate presentation. Test write failure, partial/flush failure, and death after write before ack.","introduced_in_round":9,"location":"§4.1 acceptance 4.1.10 and 4.1.15; crates/ghook/src/output.rs; crates/ghook/src/dispatch.rs; crates/ghook/src/transport.rs","prevention":"Model every boundary between response parse, output write, flush, provider consumption, ack persistence, and daemon commit; state the strongest guarantee the observable boundaries support.","principle":"Exactly-once provider delivery cannot be inferred from a process-local stdout write followed by a separate acknowledgment; the residual crash window must have an honest idempotent contract.","root_cause":"The repair treats post-emission acknowledgment as proof of delivery, while ghook's output helper discards write errors and process death can occur after bytes reach stdout but before the acknowledgment is durably queued.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r9-timeout-live-ack-missing","causal_section_ids":["4.1"],"check_key":"receipt-terminal-and-provider-parity","description":"An AGY route test can pass while Claude, Codex, Qwen, Droid, and Grok keep eager pending-message marking, direct enrichment tests fail, and Stop or either expiry path strands prepared receipts indefinitely.","finding_id":"agy-r10-receipt-terminal-incumbents","fix":"Define durable prepared, acknowledged, released, and terminal-undelivered CAS states keyed by receipt, original envelope, and canonical session. Target the Stop handler, liveness_monitor.py, lifecycle.py, and tests/hooks/test_event_enrichment.py. Rewrite the provider matrix for all five incumbents plus AGY to prove prepare-without-mark, acknowledged commit, transport release, duplicate ack, daemon restart, and terminal expiry.","introduced_in_round":9,"location":"§4.1 Targets and acceptances 4.1.11/4.1.15; tests/hooks/test_event_enrichment.py; tests/hooks/test_pending_message_provider_contracts.py; src/gobby/sessions/liveness_monitor.py; src/gobby/sessions/lifecycle.py","prevention":"When a shared commit point moves, enumerate direct unit contracts, all provider consumers, Stop, each expiry implementation, replay, and restart before closing Targets and acceptance.","principle":"Moving a shared one-shot commit boundary requires all incumbent provider contracts and every terminal owner to migrate together.","root_cause":"The repair stages EventEnricher effects and names Stop/expiry terminal-undelivered behavior, yet omits the direct enrichment suite and both expiry owners; acceptance 4.1.11 merely adds AGY to a provider suite whose incumbent behavior currently expects eager delivery.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r9-precreated-session-double-registration","causal_section_ids":["4.1"],"check_key":"resolve-register-complete-uniqueness-key","description":"Using (external_id, machine_id, project_id, source) can adopt or rebind a row of the wrong session_type, or conflict after choosing a different row than register() would. Terminal and web-chat sessions sharing native identity are the concrete collision.","finding_id":"agy-r10-session-uniqueness-session-type","fix":"Revise §4.1 to use the five-part (external_id, machine_id, source, project_id, session_type) identity everywhere: preflight lookup, registration lock, adoption, conflict recovery, and tests. Preserve the pre-created row's session_type and add concurrent terminal-versus-web_chat collision cases.","introduced_in_round":9,"location":"§4.1 resolve-or-adopt prose and acceptance 4.1.10; src/gobby/storage/sessions/_crud.py; idx_sessions_unique; migration 366_sessions_machine_uuid_fk.sql","prevention":"Derive identity tuples from the live database index and lock object, then test collisions that differ in each constituent column.","principle":"Transactional adoption must match every column in the persisted uniqueness constraint and registration lock.","root_cause":"The repair copied the register docstring's four-field description, while the actual lock, lookup calls, flattened unique index, and migration also include session_type.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r9-critical-timeout-fail-open","causal_section_ids":["4.1"],"check_key":"retryable-response-classification","description":"Without a distinct retry kind, ghook either continues on critical adapter timeouts or starts honoring fail-closed actions for daemon_not_ready and agent_run_identity_pending backpressure, recreating the documented live-lock risk.","finding_id":"agy-r10-retry-class-discriminator","fix":"Add a stable retry_kind enum with ingress_backpressure and adapter_timeout. Preserve unconditional continue and envelope retention for ingress backpressure; honor the 2.3-computed action only for adapter_timeout. Target daemon response builders plus dispatch.rs, transport.rs, and contract.rs tests covering critical and noncritical hooks in both retry classes.","introduced_in_round":9,"location":"§4.1 retryable-timeout prose and acceptance 4.1.12; crates/ghook/src/transport.rs::DeliveryReport.is_retry_backpressure; crates/ghook/src/dispatch.rs::run_gobby_owned","prevention":"Inventory every response sharing a status code and status body before adding behavior; pin a stable discriminator and action for every class.","principle":"Operational ingress backpressure and completed-hook timeout retention need distinct wire classes when their provider-visible actions differ.","root_cause":"The timeout repair reuses retryable 503 semantics without specifying a discriminator from existing 503 plus status=retry ingress backpressure, which ghook handles with unconditional continue to prevent live-lock.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r9-terminal-auth-registry-gap","causal_section_ids":["6.1"],"check_key":"agy-auth-probe-contract","description":"Record 1.1.9 cannot decide which env vars AGY reads, whether authentication is file-only, which ambient credentials must be stripped, or whether tmux auth-CLI inference has an in-scope caller. The exact-row or explicit-empty branch therefore lacks evidence.","finding_id":"agy-r10-auth-probe-shape","fix":"Extend Gate 0 with a dedicated auth record that launches under a clean environment; tests every accepted credential variable; records file-only credential roots; proves ambient credential rejection/stripping; and identifies any caller requiring auth-CLI inference. Make §§3.2 and 6.1 consume that record, including an explicit empty result when authentication is file-only.","introduced_in_round":9,"location":"§1.1 question/record 1.1.9; §6.1 auth inventory prose and acceptance 6.1.12; src/gobby/agents/spawners/auth_env.py; src/gobby/agents/tmux/spawner.py","prevention":"Trace every downstream registry field to a named upstream probe observation before declaring the registry closed.","principle":"Every closed auth inventory row must be sourced by a probe that explicitly records each field needed to populate that row.","root_cause":"The Round 9 repair points CLI_ENV_ALLOWLIST, CLI_CREDENTIAL_KEYS, and _SUPPORTED_AUTH_CLIS at record 1.1.9, whose question and output cover network domains and filesystem roots rather than credential env names or auth inference.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"agy-r9-agy-lifecycle-parity-hollow","causal_section_ids":["5.3"],"check_key":"agy-precompact-reachable-trigger","description":"A test can call the wired callback directly and report all five events while real AGY web-chat turns never emit PRE_COMPACT. Provider threading into compaction context stands, but the claimed behavioral parity does not.","finding_id":"agy-r10-precompact-trigger","fix":"Add a Gate 0 observation for AGY compaction signaling. When a real stream transition exists, specify it in §5.2, invoke _on_pre_compact from AgyManagedChatSession, and test that input end to end. When AGY exposes no trigger, remove PRE_COMPACT from the AGY backend-parity claim and retain a focused _fire_lifecycle unit test for parsed-provider compaction context.","introduced_in_round":9,"location":"§5.2 AGY backend contract; §5.3 prose and acceptance 5.3.5; src/gobby/servers/websocket/chat/backends/base.py; src/gobby/servers/websocket/chat/_session.py; tests/servers/test_fire_lifecycle_parity.py","prevention":"For each lifecycle row, trace the provider input or local state transition that fires it before counting the event as end-to-end coverage.","principle":"A behavioral lifecycle contract must be driven through a reachable provider transition rather than by invoking its callback directly.","root_cause":"The repair added PRE_COMPACT to the fake-stream parity claim and fixed provider threading, while no managed backend currently invokes _on_pre_compact and §5.2 defines no AGY record or threshold that would do so.","section_id":"5.3","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r9-plan-keystroke-limitation-overbuilt","causal_section_ids":["6.1"],"check_key":"plan-menu-negative-consumer-parity","description":"In the absent-menu branch, native plan actions can return the probe-recorded AGY refusal while attached web approval still returns generic PLAN_KEYSTROKES_UNMAPPED. Provider behavior remains inconsistent across two user-facing plan-control paths.","finding_id":"agy-r10-attached-plan-negative-contract","fix":"Add handle_attached_plan_approval and tests/servers/websocket/test_attached_plan_approval.py::* to §6.1 Targets. In the absent-menu branch, return the same probe-recorded AGY refusal from attached approval; in the proven-menu branch, retain executable DEFAULT_PLAN_KEYSTROKES dispatch and cover both paths.","introduced_in_round":9,"location":"§6.1 plan-control Targets and acceptance 6.1.11; src/gobby/servers/websocket/handlers/plan_approval.py::handle_attached_plan_approval; tests/servers/websocket/test_attached_plan_approval.py","prevention":"When a closed registry gains a supported/unsupported split, enumerate every direct lookup and update positive and negative behavior at each consumer.","principle":"A probe-backed negative contract must reach every direct consumer of the executable registry it replaces.","root_cause":"The repair targets NativePlanActionService and registry coverage but misses handle_attached_plan_approval, which independently reads DEFAULT_PLAN_KEYSTROKES and owns its own generic unmapped-provider response.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"b334834d-ca7f-4ce5-8973-7318a3e77b53","round":10,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 11** `kind: verification`

- reviewer_run: d37f404c-4fac-4ed6-b461-4cc945ae87a8
- reviewer_session: 96707755-e6b9-43e2-95a7-119484326044
- verdict: needs_review
- findings:
- agy-r11-receipt-state-storage-owner / blocking / the durable receipt states had no storage authority — no table, module, migration, or transition API owned them
- agy-r11-rule-context-receipt-bypass / blocking / rule one-shot guards, sibling variables, staged-memory IDs, and the agent preamble guard persist eagerly before transport
- agy-r11-ghook-protocol-rollout / blocking / retry_kind/receipt responses could reach an installed ghook that cannot honor them; the V2 reinstall sat after 2.3
- agy-r11-receipt-ack-durable-handoff / blocking / ack-enqueue failure vs envelope deletion, the identity-less direct-POST fallback, and ack-vs-Stop/expiry precedence were unspecified
- agy-r11-workspace-mutation-writers / blocking / project/worktree switches and worktree deletion mutate confinement with no workspace-identity update or tombstone contract
- agy-r11-migration-number-collision / blocking / 367/368 are reserved by gcore-schema-authority and 369 by m0-shared-datastores-bridge
- agy-r11-webchat-identity-ordering / blocking / 5.2 persisted provider-native identity without depending on 4.1's five-part adoption contract
- agy-r11-auth-live-process-stripping / blocking / make_spawn_env copies os.environ; helper-level allowlist tests cannot prove child-process credential absence
- resolution_notes: All 8 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — 367/368 reserved at
  gcore-schema-authority.md:1100/2159 and 369 at m0-shared-datastores-bridge.md:187
  with 370+ unreserved, the identity-less direct-POST fallback as the
  direct_post_after_enqueue_failure closure in dispatch.rs::run_gobby_owned (the
  adversary's inbox_fallback.rs filename does not exist; the behavior does), eager
  sibling set_variable application at effects.py:132 with the variable manager at
  workflows/hooks.py:154, injected_memory_ids recorded at format time in
  delivery_formatting.py:118-179 with finalize_staged_memory_delivery defined at :98
  and called from hook_manager.py:529, the first-before_agent preamble guard at
  _agent.py:250-310, runtime_compat.py pinning schema_version plus a minimum ghook
  version (the natural capability-floor seam), handle_set_project and
  handle_set_worktree at session_config.py:337/:415 with teardown preceding any
  workspace persistence, LocalWorktreeManager.delete at storage/worktrees.py:340,
  os.environ.copy() at spawners/base.py:46, and 5.2's header depending only on
  5.1/3.1/3.2. Repairs: the migration chain renumbered to the contiguous unreserved
  370–372 range — workspace 370 (3.1), startup-claim 371 and receipt-effects 372
  (4.1) — with the reservation inventory recorded in 3.1's prose (3.1.15 and 4.1.14
  reworded). 4.1 gained the relational receipt-effects storage authority
  (storage/hook_receipts.py, migration 372, baseline-synchronized, focused storage
  tests), the atomic acknowledgment handoff — envelope retained until the ack is
  durably enqueued, no staged effects on identity-less direct-POST invocations,
  terminal owners CAS only from prepared (4.1.15 reworded) — the response-visible
  producer sweep staging rule guards, sibling variables, staged-memory finalization,
  and the agent-preamble guard behind the receipt (4.1.17), and the strict-versioned
  hook-response capability floor through runtime_compat.py with both skews tested and
  the V2 reinstall gate moved after 4.1 (4.1.18). 3.1 gained the workspace-identity
  mutation writers — handle_set_project, handle_set_worktree, worktree-deletion
  tombstones — with switch, deletion, and restart cases (3.1.16). 5.2 gained the 4.1
  dependency edge and the end-to-end terminal-versus-web_chat same-conversationId
  coexistence acceptance (5.2.14). 6.1's auth proof moved to the process boundary:
  one normalized inventory shared by terminal stripping and sandbox masking, with a
  live seeded-environment child test (6.1.12 reworded).

```json plan-review-round
{"evidence_id":"8ae8347a-7879-48d5-90df-2f80d847dc93","plan_hash":"bd859983d2b95dfdc86c19828db967eb6c28231279618da0cc4b8c229e4b2835","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"3a9d740283c57ea4528054db2e11f3ced2dfa7ffdf02564a968b4547cc0042d8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":8,"total":10},"evidence_id":"8ae8347a-7879-48d5-90df-2f80d847dc93","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"46643be56805546d5ecec42714d31b54d9e6739c4bf035f391f88704c20d731a","status":"valid"},"source_digest":"ba0524dd83a5903a289c4c567febb9e9859343c229c2eb2ea3ccb2f05e78f180","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r10-receipt-ack-protocol","causal_section_ids":["4.1"],"check_key":"delivery-receipt-state-storage-owner","description":"The receipt effects have no implementation-addressable durable home. Stop and expiry are told to record terminal-undelivered, and inbox acknowledgments are told to CAS, but no table, model, storage module, indexed atomic-file contract, or migration acceptance can store or query those states. Migration 368 is scoped only to startup claim generation.","finding_id":"agy-r11-receipt-state-storage-owner","fix":"Choose the receipt-state authority explicitly. Prefer a relational receipt-effects table in the baseline and the final renumbered §4.1 migration, with receipt_id primary key, original envelope/session identity, state, staged payload, timestamps, transition guards, recovery and cleanup APIs. Target its storage module and focused tests; make inbox ack, Stop, both expiry owners, release, restart recovery, and duplicate CAS use that authority.","introduced_in_round":10,"location":"§4.1 Targets, receipt-state prose, 4.1.14-4.1.15; postgres baseline; migration 368; hooks/envelope_dedupe.py","prevention":"For every new durable state machine, trace schema or file layout, model, CRUD/CAS transitions, migration allocation, restart recovery, cleanup, and focused storage tests before accepting behavioral prose.","principle":"A durable state machine must name its storage authority, schema owner, transition API, and recovery tests.","root_cause":"Round 10 added durable prepared, acknowledged, released, and terminal-undelivered states while §4.1 still inventories only the sessions startup-claim migration and the existing file-based envelope marker; no target owns receipt rows keyed by receipt, original envelope, and session.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r10-receipt-terminal-incumbents","causal_section_ids":["4.1"],"check_key":"receipt-rule-effect-commit-boundary","description":"A one-shot rule can persist its guard or success variable and return injected context before ghook emits it. A write or flush failure then suppresses the next injection even though the provider never saw the context. Agent preamble and staged-memory delivery expose the same bypass outside EventEnricher.","finding_id":"agy-r11-rule-context-receipt-bypass","fix":"Add WorkflowHookHandler rule persistence, EffectsMixin effect application, delivery formatting/HookManager staged-memory finalization, and agent-preamble injection to §4.1 Targets with focused suites. Store each response-visible payload together with its one-shot guards and sibling state changes in the receipt effect record; commit by acknowledgment CAS, release on transport loss, and terminalize on Stop/expiry. Preserve deliberately per-turn unguarded context.","introduced_in_round":10,"location":"§4.1 Targets and 4.1.15; workflows/hooks.py; workflows/engine/effects.py; workflows/engine/delivery_formatting.py; hooks/hook_manager.py; event_handlers/_agent.py","prevention":"Inventory every context producer and each sibling guard or variable mutation; test write failure, release, restart, acknowledgment, duplicate acknowledgment, and terminal expiry at the real persistence seam.","principle":"Every mutation that suppresses future delivery of response-visible context must commit atomically with proof that the context was delivered.","root_cause":"§4.1 targets EventEnricher and startup claims, while live rule evaluation eagerly persists set_variable and mcp_call success variables beside inject_context, staged-memory formatting records injected IDs, and the agent preamble writes its one-shot guard before transport.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"agy-r10-retry-class-discriminator","causal_section_ids":["4.1"],"check_key":"ghook-daemon-receipt-protocol-rollout","description":"A new daemon can emit adapter_timeout or prepared receipt responses to the installed old ghook. That binary continues critical timeouts and cannot acknowledge prepared effects. The reverse skew receives neither discriminator nor receipt. Reinstalling after §2.3 leaves the final §4.1 Rust changes absent from the live binary.","finding_id":"agy-r11-ghook-protocol-rollout","fix":"Define one strict post-change hook-response capability/version and reject mismatches before adapter execution or effect preparation. Pin it in the request/runtime diagnostic, update the daemon response builder and ghook parser, test old-ghook/new-daemon and new-ghook/old-daemon rejection, and move rebuild/reinstall plus activation verification after §4.1. A coordinated ghook-first rollout or equivalent activation gate must prevent legacy clients from receiving adapter_timeout or receipt-bearing responses.","introduced_in_round":10,"location":"§4.1 retry/receipt contract and Targets; V2 ghook reinstall item; ghook dispatch.rs/transport.rs; hooks/runtime_compat.py; hooks route response builder","prevention":"For every bidirectional wire change, enumerate old/new endpoint pairs, define strict mismatch behavior, gate server activation on the client capability, and place install/restart verification after the last producer and consumer change.","principle":"A wire change that alters fail-open/fail-closed behavior and delivery commitment must be activated only after both endpoints share one strict protocol.","root_cause":"Round 10 added retry_kind and receipt metadata in §4.1, while V2 still rebuilds and reinstalls ghook after §2.3. Current ghook treats every 503 status=retry as ingress backpressure before criticality and cannot acknowledge receipts; runtime compatibility checks only the existing envelope protocol.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r10-receipt-ack-protocol","causal_section_ids":["4.1"],"check_key":"receipt-ack-durable-handoff","description":"After output succeeds, ack enqueue can fail or remain queued while the original envelope is deleted and Stop or expiry terminalizes the prepared effects. The direct-post-after-enqueue-failure path can also produce a response without a durable original identity. Those executions have neither a reliable ack nor a replayable original, so provider-visible context can be recorded terminal-undelivered or stranded.","finding_id":"agy-r11-receipt-ack-durable-handoff","fix":"Specify an atomic durable handoff: retain the original envelope until the receipt ack is durably enqueued, or atomically replace it with the ack. Define the blocked-inbox direct-POST branch so staged effects require durable identity. Define ack precedence against later Stop/expiry for successfully emitted responses. Target inbox_fallback.rs and test ack-write disk/permission failure, original-versus-ack drain ordering, queued-ack then Stop, restart, and duplicate replay.","introduced_in_round":10,"location":"§4.1 delivery-receipt prose and 4.1.15; ghook dispatch.rs/transport.rs/output.rs; inbox_fallback.rs; hooks/inbox.py","prevention":"Trace output, flush, ack write, original cleanup, drain, Stop, expiry, restart, disk-full, permission, and duplicate branches; require an owned durable artifact at every boundary.","principle":"A delivered response must retain a durable recovery artifact until acknowledgment is durably accepted, across every filesystem and lifecycle race.","root_cause":"§4.1 gates ack creation on successful output but leaves ack-enqueue failure, original-envelope deletion, direct-POST fallback without a durable envelope id, and ack-versus-Stop/expiry ordering unspecified.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r10-workspace-identity-writers","causal_section_ids":["3.1"],"check_key":"persisted-workspace-mutation-seams","description":"A project or worktree switch can leave a paused session with a path from the old confinement root. ACP close/delete can run before a new hydration repairs it; use-time validation then either fails an otherwise valid lifecycle operation or consumes stale identity. Deleted worktrees are covered only at the consumer, with no mutation/tombstone contract for persisted references.","finding_id":"agy-r11-workspace-mutation-writers","fix":"Add handle_set_project, handle_set_worktree, their focused websocket suites, and worktree-removal invalidators to §3.1. Atomically update or invalidate workspace_path before teardown, define tombstone/fail-closed semantics for deletion, and test project switch, worktree switch, deletion before close/delete, restart before rehydration, and successful operation-owned ACP cleanup.","introduced_in_round":10,"location":"§3.1 persisted workspace contract and acceptance 3.1.12/3.1.15; session_config.py::handle_set_project and handle_set_worktree; worktree removal owners","prevention":"For persisted identity fields, trace create, adopt, hydrate, mutate, delete/tombstone, consume, and restart paths, then target every distinct writer and invalidator.","principle":"A persisted confinement identity must be updated or invalidated at every operation that changes its effective project or worktree.","root_cause":"The repaired inventory covers first resolution only. handle_set_project changes project_id on the paused session row, and handle_set_worktree stores the replacement path only in an in-memory pending map; neither owns workspace_path.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"repository-wide-migration-allocation","description":"Both AGY migration numbers are already reserved by another canonical plan. The two plans cannot land their reviewed filenames or startup order together, and the receipt-state authority may require another schema allocation.","finding_id":"agy-r11-migration-number-collision","fix":"Coordinate the repository-wide slot chain and renumber this plan's workspace and startup/receipt migrations to an unreserved contiguous range after all existing reservations. Update every target, prose reference, migration-contract expectation, V1 current-round note, and dependency edge; keep workspace before startup/receipt storage.","location":"§3.1 migration 367 and §4.1 migration 368; .gobby/plans/gcore-schema-authority.md migrations 367_dream_check_tighten.sql and 368_bm25_disposition.sql","prevention":"Before assigning migration filenames, scan current migrations and every canonical plan for reserved numbers, then reserve one contiguous dependency-ordered range.","principle":"Numbered migrations need repository-wide unique reservations, including reviewed plans awaiting implementation.","root_cause":"The AGY plan checked the current migration directory and its own changelog, but omitted the active gcore-schema-authority plan from the reservation inventory.","section_id":"3.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"agy-r10-session-uniqueness-session-type","causal_section_ids":["4.1","5.2","5.3"],"check_key":"agy-webchat-hook-identity-composition","description":"§5.2 can implement concurrently with §4.1 and persist an AGY conversationId before the five-part adoption contract exists. Existing §4.1 collision tests pin the storage helper in isolation; no AGY backend acceptance proves a web_chat row and terminal row sharing conversationId remain distinct through persistence, reconstruction, and hook resolution.","finding_id":"agy-r11-webchat-identity-ordering","fix":"Add 4.1 to §5.2 dependencies. Add §5.2/§5.3 integration acceptance that starts AGY web chat, persists and reconstructs its provider-native id, creates a terminal session with the same id, and proves web-chat hooks resolve only the web_chat row, terminal hooks resolve only the terminal row, and neither path mints, rebinds, or resumes the other.","introduced_in_round":10,"location":"§4.1 five-part adoption identity and 4.1.10; §5.2 dependency header and AGY metadata persistence; §5.3 websocket acceptance","prevention":"After changing an identity key, find every downstream writer and resolver, add explicit dependency edges, and exercise collisions at the highest integration seam.","principle":"A consumer that persists provider-native identity must depend on the uniqueness/adoption contract it relies on and test the composed collision path.","root_cause":"Round 10 qualified §4.1 adoption by session_type, while §5.2 still depends only on 5.1, 3.1, and 3.2. Manifest phases do not serialize leaves, and §5.2/§5.3 never exercise the AGY terminal-versus-web_chat same-id collision through the real backend.","section_id":"5.2","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"agy-r10-auth-probe-shape","causal_section_ids":["3.2","6.1"],"check_key":"agy-auth-live-process-stripping","description":"An explicit empty AGY credential row can pass helper tests while an unlisted ambient AGY credential remains visible inside the tmux child. Sandbox masking uses a separate inventory, so equal casing and provider normalization at helper lookup do not establish runtime absence.","finding_id":"agy-r11-auth-live-process-stripping","fix":"Derive explicit AGY ambient deny/unset names from 1.1.15, use one normalized provider/key inventory for terminal stripping and sandbox masking, target the tmux spawn seam and its focused tests, and launch a real child under a seeded ambient environment to assert denied variables are absent while allowed non-secret variables remain.","introduced_in_round":10,"location":"Gate 0 record 1.1.15; §3.2.4; §6.1.12; agents/spawners/base.py; auth_env.py; tmux/spawner.py; sandbox_policy.py","prevention":"For every credential allow/deny inventory, test exact normalized names at helper, sandbox-mask, process-launch, and child-observation boundaries.","principle":"Credential-deny requirements must be proven at the spawned process boundary that can inherit ambient daemon state.","root_cause":"Round 10 added probe-backed closed auth inventories and an ambient-strip helper test, while make_spawn_env copies os.environ and tmux inherits the daemon environment. §6.1 targets only auth_env helper tests and does not observe the live child.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"96707755-e6b9-43e2-95a7-119484326044","round":11,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 12** `kind: verification`

- reviewer_run: 430df839-e6c5-4233-916d-96e0200a0adb
- reviewer_session: 0d83a4e4-547f-40b8-88b0-2c8f712c671a
- verdict: needs_review
- findings:
- agy-r12-webchat-hook-identity-handoff / blocking / 5.2.14 required terminal/web_chat coexistence with no production handoff of canonical identity into the AGY subprocess and ghook envelope
- agy-r12-capability-stamp-and-replay-seams / blocking / runtime.rs writes only schema_version and ghook_version — no targeted producer could publish the receipt capability, and the transport-path matrix was unpinned
- agy-r12-response-visible-producer-sweep-incomplete / blocking / WorkflowRuleEvaluator memory/skill dedupe claims and InjectionTrackingMixin review-lesson tracking persist eagerly before transport, outside the receipt sweep
- agy-r12-receipt-terminal-retention / blocking / the receipt-effects table had no per-state retention lifecycle, maintenance owner, or bounded pruning contract
- agy-r12-response-effect-delivery-classification / blocking / RuleEffect cannot express eager-versus-on_receipt delivery, so staged-commit code would have to infer one-shot status from sibling variable diffs
- agy-r12-identityless-fallback-delivery-mode / blocking / the identity-less direct-POST branch had no defined delivery mode — fallback-only sessions could lose startup context permanently or present it under no stated contract
- resolution_notes: All 6 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — `write_runtime_stamp`
  emitting only schema_version and ghook_version (runtime.rs:4-16) with
  tests/hooks/test_runtime_compat.py present and statusline returning locally at
  dispatch.rs:87-88 before any transport; `dedup_memory_results` claiming
  injected_memory_ids via claim_set_variable_values (rule_evaluator.py:277-306) and
  `dedup_skill_results` claiming suggested_skill_names (:308-338), both invoked from
  hook_manager.py:706-710, plus `_filter_and_track_new_review_lessons` appending
  injected_review_lesson_ids (injection_tracking.py:14-58), with
  tests/hooks/test_hook_extracted_helpers.py as the direct evaluator suite; `RuleEffect`
  (definitions.py:119-308, file at 911 lines) carrying no delivery-disposition or
  grouping fields; the ghook session context GOBBY_SESSION_ID/GOBBY_PROJECT_ID
  recognized at dispatch.rs:252/:302 with `db_session_id` on managed chat sessions
  (_session.py:652); `direct_post_after_enqueue_failure` results emitted via
  emit_action (dispatch.rs:150-162) and enqueue-only success emitting bare continue
  (:166-167); and the periodic-maintenance precedent at
  runner_lifecycle_periodic.py::start_periodic_tasks with workflow_audit_cleanup_loop
  (runner_maintenance_audit.py:45 lines). One adversary claim refined during
  verification: tests/hooks/test_inbox.py has been targeted since Round 9 — only
  runtime.rs and test_runtime_compat.py were missing. Repairs: 4.1 targeted
  `write_runtime_stamp`, test_runtime_compat.py, rule_evaluator.py,
  `InjectionTrackingMixin._filter_and_track_new_review_lessons`,
  test_hook_extracted_helpers.py, `RuleEffect`, the representative one-shot-guard
  template, and runner_lifecycle_periodic.py; the strict-protocol paragraph gained
  the stamp producer and the pinned transport-path matrix — direct, detached,
  enqueue-only-at-drain, identity-less, statusline (4.1.18 reworded); the producer
  sweep gained the discovery-dedupe claims (4.1.17 reworded); `RuleEffect` gained the
  producer-declared eager/on_receipt disposition with single-rule receipt grouping
  and the bundled-template sweep (new classification prose, 4.1.17); the receipt
  authority gained the per-state retention lifecycle owned by a periodic
  receipt-retention loop (new 4.1.19); the identity-less fallback became explicit
  at-least-once presentation with fallback-only Stop/expiry terminalization (4.1.15
  reworded); workflows/definitions.py joined the line budget; and 5.2 gained the
  canonical-identity spawn-environment handoff through GOBBY_SESSION_ID with 4.1
  preflight validation by session_type (5.2.14 reworded).

```json plan-review-round
{"evidence_id":"ed650d95-1b9e-4f8f-af0f-0e93d8620e02","plan_hash":"5f555fada0d76d6e76b6d22a81e243ebf333d7e81fe4e0ad44ee91b030a9eaf5","round_number":12,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"752550caf43ac64014fe6a0682c7bb57cb929094672dec7e9e025809e3489b20","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":6,"total":8},"evidence_id":"ed650d95-1b9e-4f8f-af0f-0e93d8620e02","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"cca788400e267eca0c0751e3b85a41c466d023e832055b109d306c74d292365d","status":"valid"},"source_digest":"0725643a06bf10b8b1a6603a8ee7c8db77fa910fbbc851c04626b88f575b71a0","version":1},"findings":[{"category":"missing-requirement","causal_finding_id":"agy-r11-webchat-identity-ordering","causal_section_ids":["5.2"],"check_key":"webchat-canonical-hook-identity-handoff","description":"Acceptance 5.2.14 requires terminal and web-chat rows sharing one AGY conversationId to coexist and receive only their own hooks, yet the backend contract never exports the canonical web-chat row identity to ghook. The real-backend test therefore has no specified mechanism for reaching the intended web_chat row instead of external-id fallback.","finding_id":"agy-r12-webchat-hook-identity-handoff","fix":"Add a 5.2 production requirement and Target coverage so every AGY web-chat subprocess exports the canonical db_session_id, project id, and source through the ghook-recognized session context. Require 4.1 to resolve and validate that canonical id as session_type=web_chat before any external-id fallback. Make 5.2.14 capture the real backend launch environment and drive the actual adapter/hook route beside a terminal process using the same conversationId.","introduced_in_round":11,"location":"§5.2 Targets and acceptance 5.2.14; §4.1 five-part adoption preflight","prevention":"For each pre-created session path, trace canonical id, project, source, and session_type through process launch, hook envelope, route preflight, storage resolution, reconstruction, and same-native-id collision tests.","principle":"When a provider-native identifier is intentionally non-unique across session types, the subprocess must carry the canonical pre-created session identity into hook resolution.","root_cause":"Round 11 added the 4.1 dependency and end-to-end coexistence assertion, but it did not specify the production handoff from AgyManagedChatSession.db_session_id and project identity into the AGY process and ghook envelope. ConversationId alone cannot select between terminal and web_chat rows.","section_id":"5.2","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r11-ghook-protocol-rollout","causal_section_ids":["4.1","V2"],"check_key":"hook-capability-stamp-producer-and-path-matrix","description":"The plan requires the installed ghook to advertise the receipt capability, but no targeted Rust producer can add that capability to the runtime stamp. The stated both-skew proof also does not pin direct, detached, and enqueue-only/replayed invocation reachability; statusline is a legitimate daemon-free non-effect bypass and needs an explicit assertion.","finding_id":"agy-r12-capability-stamp-and-replay-seams","fix":"Add crates/ghook/src/runtime.rs, tests/hooks/test_runtime_compat.py, and tests/hooks/test_inbox.py to 4.1 Targets. Require the stamp writer to publish the receipt capability and test that direct and detached POSTs gate before execution, enqueue-only requests gate when drained, identity-less fallback cannot stage effects, and statusline remains a local non-effect path.","introduced_in_round":11,"location":"§4.1 Targets and acceptance 4.1.18; crates/ghook/src/runtime.rs; tests/hooks/test_runtime_compat.py; tests/hooks/test_inbox.py","prevention":"For each bidirectional protocol capability, inventory the stamp writer, parser, activation check, direct path, detached path, enqueue-and-replay path, local bypasses, and focused tests before accepting version-skew coverage.","principle":"A strict capability gate must target both the stamp producer and consumer and prove every response-bearing transport path reaches the same gate.","root_cause":"Round 11 anchored the repair only to runtime_compat.py, while crates/ghook/src/runtime.rs currently writes only schema_version and ghook_version and is absent from 4.1 Targets. Focused runtime-compatibility and inbox-replay owners are also absent.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r11-rule-context-receipt-bypass","causal_section_ids":["4.1"],"check_key":"response-visible-dedupe-producer-sweep","description":"Memory results, skill suggestions, and review lessons can still mark their IDs delivered before ghook emits the response. A write or fallback failure can therefore suppress those payloads on later hooks despite 4.1.17 claiming every response-visible one-shot producer is receipt-staged.","finding_id":"agy-r12-response-visible-producer-sweep-incomplete","fix":"Add rule_evaluator.py, injection_tracking.py, and their incumbent focused suites to 4.1 Targets. Store memory IDs, suggested skill names, and review-lesson IDs in the receipt effect payload and update their dedupe sets only on acknowledgment, with release and terminal-undelivered cases.","introduced_in_round":11,"location":"§4.1 Targets and acceptance 4.1.17; src/gobby/hooks/rule_evaluator.py; src/gobby/workflows/engine/injection_tracking.py","prevention":"Search every response formatter, result deduper, injection tracker, and sibling variable mutation; classify each future-delivery suppressor and test write failure, release, acknowledgment, duplicate acknowledgment, restart, and terminal expiry.","principle":"Every mutation that suppresses future response-visible content must commit through the same delivery receipt as that content.","root_cause":"Round 11 enumerated rule-effect persistence, staged-memory formatting, and the agent preamble, but missed WorkflowRuleEvaluator's eager injected_memory_ids and suggested_skill_names claims and InjectionTrackingMixin's eager injected_review_lesson_ids append.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r11-receipt-state-storage-owner","causal_section_ids":["4.1"],"check_key":"receipt-effects-retention-lifecycle","description":"Acknowledged and terminal-undelivered receipt rows have no bounded growth policy. Merely naming cleanup APIs leaves no executable owner or safety contract, so normal one-shot hook traffic can grow the receipt table indefinitely and an implementer cannot know when duplicate acknowledgments or recovery records may be discarded.","finding_id":"agy-r12-receipt-terminal-retention","fix":"Define retention for every state: prepared rows are never pruned; released rows remain until re-prepared or terminalized; acknowledged and terminal-undelivered rows become eligible only after an explicit recovery/idempotency window. Name the existing daemon maintenance or lifecycle caller, prune in bounded indexed batches, and add restart plus concurrent acknowledgment, Stop/expiry, duplicate-ack, and pruning tests.","introduced_in_round":11,"location":"§4.1 receipt-effects prose and acceptance 4.1.15; migration 372; storage/hook_receipts.py","prevention":"For every durable protocol table, enumerate create, transition, recovery, terminal retention, idempotency horizon, maintenance owner, indexed batch pruning, restart, and concurrent-prune tests.","principle":"A durable high-frequency protocol table needs a bounded lifecycle for every terminal state while preserving nonterminal recovery and the duplicate-idempotency window.","root_cause":"Round 11 added a relational receipt authority and named cleanup APIs, but supplied no retention window, state eligibility, maintenance caller, bounded batch, pruning index, or prune-versus-transition tests.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r11-rule-context-receipt-bypass","causal_section_ids":["4.1"],"check_key":"response-effect-delivery-classification","description":"The plan says guard-suppressed payloads stage while deliberately per-turn context stays eager, but the current effect schema cannot express that distinction or link inject_context to its guard, set_variable, or mcp_call success mutation. Implementation would have to guess from unrelated variable diffs and could delay ordinary state or eagerly commit a one-shot guard.","finding_id":"agy-r12-response-effect-delivery-classification","fix":"Add an explicit producer-side delivery disposition such as eager or on_receipt and the smallest structured grouping that links each response payload to its suppressing guard and sibling mutations. Target RuleEffect definition/validation and installed producer records or an equivalent typed context-contribution record, then test mixed rules, observer mutations, sibling variables, release, duplicate acknowledgment, and terminalization.","introduced_in_round":11,"location":"§4.1 Targets and acceptance 4.1.17; src/gobby/workflows/definitions.py; workflows/engine/effects.py; workflows/hooks.py","prevention":"Require every response-producing effect record to declare eager or on-receipt delivery and identify its grouped guard or sibling mutations; test mixed eager and staged effects in one evaluation.","principle":"A commit-boundary distinction must be explicit in producer data and must group a response payload with the mutations that suppress its future delivery.","root_cause":"Round 11 describes eager per-turn context and receipt-staged one-shot context only in prose. RuleEffect has no delivery disposition, and the effect/persistence seams expose independent effects plus a whole-variable diff, so one-shot status cannot be derived reliably from an arbitrary sibling mutation or condition shape.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r11-receipt-ack-durable-handoff","causal_section_ids":["4.1"],"check_key":"identityless-fallback-terminal-semantics","description":"A fallback-only session has no defined delivery contract. Omitting one-shot content makes startup context permanently unavailable during persistent enqueue failure; emitting it without stating the mode permits repeated provider-visible presentation with no receipt commit. Stop and expiry behavior for that never-durable path is also unspecified.","finding_id":"agy-r12-identityless-fallback-delivery-mode","fix":"Define identity-less direct POST as an explicit at-least-once presentation: include the one-shot payload while leaving its effects uncommitted, permit repeated presentation during persistent fallback, create and commit a receipt on a later durable hook, and record terminal-undelivered on Stop or expiry. Add tests for repeated enqueue failure with no durable successor and for later recovery.","introduced_in_round":11,"location":"§4.1 receipt handoff prose and acceptance 4.1.15; crates/ghook/src/dispatch.rs::run_gobby_owned","prevention":"Enumerate durable POST, identity-less POST, repeated enqueue failure, no durable successor, eventual recovery, Stop, and expiry; assert provider-visible output and durable effect state for each branch.","principle":"Every reachable response path needs explicit presentation, commitment, duplicate, recovery, and terminal semantics, including operation without durable envelope identity.","root_cause":"Round 11 forbids staging effects on direct_post_after_enqueue_failure and says they wait for the next durable hook, but never states whether the identity-less response includes the one-shot payload or what happens when every hook remains on that branch.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"0d83a4e4-547f-40b8-88b0-2c8f712c671a","round":12,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 13** `kind: verification`

- reviewer_run: c99d2f23-3c23-4f40-b90b-810c609ac089
- reviewer_session: 4c7a6ecf-fb82-48e4-965c-1c8dad5223e4
- verdict: needs_review
- findings:
- agy-r13-rule-disposition-propagation / blocking / the disposition sweep missed three bundled one-shot templates, RuleEffect's direct model suite, and the preserved user/project-owned rows template sync never refreshes
- agy-r13-receipt-retention-state-contract / blocking / the retention window had no named constant, clock, or boundary, and released rows had no reachable re-prepare or terminal transition
- agy-r13-webchat-lifecycle-double-dispatch / blocking / 5.2.14's native hook route plus 5.3.5's _fire_lifecycle parity would execute every rule, MCP call, handler, message, webhook, and broadcast twice per AGY web-chat event
- agy-r13-srt-final-env-composition / blocking / no requirement made the AGY backend merge launch.provider_env with the identity env, and the shim/lifetime symbols exceeded the prepare_sandbox_launch-only target
- agy-r13-runtime-capability-request-provenance / blocking / capability gating read the mutable machine-global stamp, misclassifying old envelopes and old processes after a reinstall
- agy-r13-enqueue-floor-disposition / blocking / a below-floor enqueue-only envelope had no terminal state — the drain retains every non-2xx forever
- agy-r13-canonical-session-preflight-validation / blocking / the pre-created hint was validated by session_type only, admitting rows with wrong project, source, machine, or tombstoned workspace
- resolution_notes: All 7 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — the three bundled
  templates confirmed as one-shot producers (memory-capture-nudge.yaml inject_context +
  set_variable guard; handle-plan-mode-entry.yaml both rules; discover-skill-hubs-on-turn-start.yaml
  mcp_call inject_result + success_variable), `_is_sync_managed_rule`
  (workflows/sync_rules.py) managing only source=="installed" global rows with
  user/custom and project rows preserved, tests/workflows/test_rule_models.py and
  tests/agents/test_srt_runtime.py both existing, `_fire_lifecycle`
  (websocket/chat/_lifecycle.py:107-344) as a documented HookManager.handle mirror
  running rules, blocking webhooks, mcp_call dispatch, event handlers, pending-message
  piggyback, and broadcasts, Droid's child env setting GOBBY_HOOKS_DISABLED=1
  (backends/droid.py:477-478), `SandboxLaunch.provider_env` (srt_runtime.py:49) merged
  by callers via env.update(launch.provider_env) (spawn_executor.py:111) and
  {**env, **launch.provider_env} preflight (:386), the hook envelope carrying only
  schema_version (envelope.rs:25), and the inbox drain retaining every non-2xx
  indefinitely with the `_quarantine_or_warn` precedent for missing-envelope-id
  (inbox.py:243-272). Repairs: 4.1's bundled sweep became an enumeration of all four
  one-shot templates plus sync_rules.py's typed data-migration/validation owner for
  preserved user/project rows with test_rule_models.py round-trips (4.1.17 reworded);
  the retention lifecycle gained the named HOOK_RECEIPT_IDEMPOTENCY_WINDOW constant
  with wall-clock semantics, strict-after boundary, released-to-prepared re-prepare
  with envelope lineage, and released-to-terminal CAS for terminal owners (4.1.19
  reworded; 4.1.15 and the prose "only from prepared" corrected to prepared-or-released);
  5.3 adopted the single-authority contract — native ghook owns AGY BEFORE_AGENT,
  tool, and STOP workflow effects, stream parsing limited to UI events, exactly-once
  side-effect assertions through the adapter/hook route, PRE_COMPACT separately keyed
  and deduplicated (5.3.5 reworded); 3.1 widened srt_runtime.py to justified module
  scope with tests/agents/test_srt_runtime.py and stated the single env-composition
  algorithm — identity env first, prepare_sandbox_launch, provider_env merge, wrapped
  argv — with 5.2.11 gaining the joint argv-plus-final-child-env capture; 4.1.18 moved
  capability gating to an immutable request-carried response-capability field beside
  schema_version, preserved through replay, with the stamp demoted to diagnostics and
  reinstall-boundary provenance races tested; below-floor enqueue-only envelopes gained
  terminal quarantine at drain per the _quarantine_or_warn precedent with bounded
  retention, startup-barrier settlement, and receipt terminalization; and the
  pre-created hint validation extended to the full row identity — project, source,
  machine, session_type, persisted workspace/tombstone state — with mismatch rejection
  and no pre-validation mutation (4.1.10 and 5.2.14 reworded).

```json plan-review-round
{"evidence_id":"ef49bf38-5266-4562-9099-a449d860de8b","plan_hash":"459a5b2f9e62c6b72b6f292447e02f348344c84456cb6f2858f652bd3fb15bdb","round_number":13,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c765910e2dcbf4d2bfcf5e8d0e96031c46b7fec4525832a5d9574287fabfb9f0","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":8,"emitted_findings":7,"total":15},"evidence_id":"ef49bf38-5266-4562-9099-a449d860de8b","lanes":[{"candidate_count":4,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"79b02d9c674b661d62371fd4217671febfbc7b36fc0973c11d330875a05a33e5","status":"valid"},"source_digest":"d93b0040789fb5042bb0ebc6d854cad870009dfd19cb2ddeb978b408c3b79687","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r12-response-effect-delivery-classification","causal_section_ids":["4.1"],"check_key":"response-effect-delivery-classification","description":"Acceptance 4.1.17 says every response-visible one-shot producer declares on_receipt, yet memory-capture-nudge.yaml, handle-plan-mode-entry.yaml, and discover-skill-hubs-on-turn-start.yaml are untargeted. Existing user/project rows without the new field deserialize eager because sync_rules.py intentionally does not refresh them, so their guards can still commit before delivery.","finding_id":"agy-r13-rule-disposition-propagation","fix":"Add the three YAML files, their direct rule suites, and tests/workflows/test_rule_models.py to §4.1 Targets. Define a typed data-migration or validation owner for existing user/project one-shot definitions that writes an explicit disposition while preserving ownership and enabled toggles; ambiguous rows must produce an actionable validation failure. Test installed-global, user-global, and project-scoped rows plus JSON round trips, release, acknowledgment, and terminalization.","introduced_in_round":12,"location":"§4.1 Targets and acceptance 4.1.17; sync_rules.py; workflows/definitions.py; bundled memory-lifecycle, plan-mode, and skill-discovery rules","prevention":"Inventory all bundled definitions containing inject_result or response context plus acknowledge_variable, success_variable, or suppressing set_variable; then trace installed-global, user-global, and project-owned refresh semantics and direct schema round trips.","principle":"A producer-declared delivery contract must reach every installed producer and every persisted ownership class that can suppress future response-visible content.","root_cause":"Round 12 added eager/on_receipt to RuleEffect and named one representative bundled template, while the actual registry refreshes only sync-managed installed global rows and preserves user/custom and project-owned definitions. The target sweep also omits three confirmed bundled one-shot producers and RuleEffect's direct model suite.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r12-receipt-terminal-retention","causal_section_ids":["4.1"],"check_key":"receipt-effects-retention-lifecycle","description":"Acknowledged and terminal-undelivered pruning is not implementable deterministically, and a released row can remain permanently non-prunable because the plan never says how it is re-prepared or terminalized. The required late-duplicate test covers only an unnamed inside-window case.","finding_id":"agy-r13-receipt-retention-state-contract","fix":"Add an explicit numeric HOOK_RECEIPT_IDEMPOTENCY_WINDOW constant or named configuration with fixed default, monotonic/wall-clock choice, cutoff timestamp, and inclusive/exclusive boundary. Re-prepare the same receipt atomically when its payload moves to the next durable envelope, record envelope lineage, and allow Stop/expiry CAS from released to terminal-undelivered. Test just-inside, exact-boundary, just-outside, released re-prepare, released terminalization, duplicate ack after pruning, and ack/terminal/prune races.","introduced_in_round":12,"location":"§4.1 receipt state prose and acceptance 4.1.19; storage/hook_receipts.py; periodic receipt-retention loop","prevention":"For each receipt state, enumerate create, acknowledgment, release, re-prepare, supersession, Stop/expiry, prune eligibility, timestamp owner, boundary, concurrent CAS, and duplicate behavior on both sides of the window.","principle":"Every durable state needs a reachable terminal or reuse transition, and every time-based prune rule needs a named horizon and exact boundary semantics.","root_cause":"Round 12 called the recovery/idempotency window explicit without assigning a numeric value, constant/config owner, clock, or cutoff. It also retained released rows until re-prepared or terminalized while Stop/expiry terminalization remains constrained to prepared rows and no released-to-reprepared lineage is defined.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r12-webchat-hook-identity-handoff","causal_section_ids":["4.1","5.2"],"check_key":"webchat-lifecycle-single-owner","description":"A real AGY web-chat turn can execute rules, MCP calls, event handlers, pending-message delivery, webhooks, and broadcasts once through AgyAdapter/HookManager and again through the managed stream callbacks. Droid avoids this collision by disabling native hooks in its child environment; the AGY plan enables both paths.","finding_id":"agy-r13-webchat-lifecycle-double-dispatch","fix":"Declare native ghook as the AGY web-chat authority for BEFORE_AGENT, tool, and STOP events because §5.2.14 requires the actual hook route. Keep stream parsing limited to UI text/tool-state events, rework §5.3.5 to drive lifecycle parity through the adapter/hook route, and assert every side effect exactly once. Treat any stream-only PRE_COMPACT signal as a separately keyed, deduplicated event.","introduced_in_round":12,"location":"§§4.1, 5.2.14, and 5.3.5; ChatLifecycleMixin._fire_lifecycle; ManagedChatSessionBase lifecycle callbacks","prevention":"For every managed provider, map each lifecycle event from provider signal to its single evaluator and assert exactly-once rule, MCP, handler, message, webhook, and broadcast effects.","principle":"One provider lifecycle event must have exactly one workflow-effect authority.","root_cause":"Round 12 made the real AGY web-chat subprocess and native ghook route observable end to end, while §5.3 still requires the same BEFORE_AGENT, BEFORE_TOOL, AFTER_TOOL, and STOP events to run through ChatLifecycleMixin._fire_lifecycle. That method explicitly mirrors HookManager.","section_id":"5.3","severity":"blocking"},{"category":"weak-testability","causal_finding_id":"agy-r12-webchat-hook-identity-handoff","causal_section_ids":["4.1","5.2"],"check_key":"srt-final-child-env-composition","description":"§5.2.11 can pass for SRT argv/policy and §5.2.14 can pass for GOBBY_SESSION_ID/GOBBY_PROJECT_ID in an unwrapped launch while the production SRT child drops either identity or TMPDIR/provider variables. The planned shim and cleanup symbols also lack exact target and direct-suite coverage.","finding_id":"agy-r13-srt-final-env-composition","fix":"Specify one algorithm: construct the identity/base env first, pass that same mapping to prepare_sandbox_launch, merge launch.provider_env into it, and pass the merged mapping with launch.wrap(argv) to create_subprocess_exec. Expand §3.1's srt_runtime target to justified module scope or enumerate SandboxLaunch plus every shim/lifetime symbol, target tests/agents/test_srt_runtime.py, and add one real AGY backend test capturing the wrapped argv and final child env together.","introduced_in_round":12,"location":"§3.1 SRT shim targets; §5.2.11 and 5.2.14; agents/srt_runtime.py::prepare_sandbox_launch","prevention":"For each wrapped subprocess, trace base identity env, executable resolution, sandbox preflight, provider_env merge, wrapper argv, credential masking, child creation, cleanup, and one boundary capture that observes them together.","principle":"A wrapped launch contract must observe one final argv/environment pair at the child boundary.","root_cause":"Round 12 added real-launch identity capture separately from the SRT launch matrix. prepare_sandbox_launch consumes the base env and returns provider_env separately, while established callers merge it; the plan never requires the AGY backend to perform or jointly test that merge. §3.1 also targets only prepare_sandbox_launch although its shim/lifetime design changes SandboxLaunch and adds symbols.","section_id":"5.2","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r12-capability-stamp-and-replay-seams","causal_section_ids":["4.1"],"check_key":"hook-capability-stamp-producer-and-path-matrix","description":"An old queued envelope drained after a reinstall can be classified using the new stamp and reach response/receipt behavior its originating ghook did not support. The transport-path matrix proves where gating occurs, yet it does not bind the gate input to the originating request.","finding_id":"agy-r13-runtime-capability-request-provenance","fix":"Add an immutable ghook protocol version or response-capabilities field to every direct, detached, and enqueue-only envelope and preserve it through inbox replay. Gate before adapter execution and effect preparation using the request-carried value; retain the runtime stamp for installation-health diagnostics. Add reinstall-boundary and envelope-replay cases while keeping statusline outside envelope construction.","introduced_in_round":12,"location":"§4.1 runtime capability prose and acceptance 4.1.18; crates/ghook/src/runtime.rs and envelope.rs; hooks/runtime_compat.py","prevention":"Test capability provenance across old-process-after-reinstall, old-envelope-after-reinstall, direct, detached, enqueue-only replay, new-client/old-daemon, and local bypass paths.","principle":"Protocol compatibility must be attributable to the process and immutable envelope that produced a request.","root_cause":"Round 12 placed receipt capability only in the mutable machine-global .ghook-runtime.json stamp. The transported envelope carries schema_version but no producer version/capability, so reinstalling ghook can change the global answer for an already-running process or queued old envelope.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r12-capability-stamp-and-replay-seams","causal_section_ids":["4.1"],"check_key":"enqueue-only-below-floor-disposition","description":"A below-floor enqueue-only envelope can be redelivered forever, keeping the inbox unsettled and repeatedly hitting the compatibility gate. ghook already returned continue locally, so no live process exists to consume receipt-bound output even if the envelope later executes.","finding_id":"agy-r13-enqueue-floor-disposition","fix":"Make below-floor enqueue-only envelopes terminally quarantined: perform no adapter or effect execution, atomically move them out of the active drain set with bounded retention, release any lease, emit an actionable protocol diagnostic, and treat the item as settled for startup barriers. Terminalize any associated prepared receipt as undelivered. Add repeated-drain, restart, quarantine-retention, and zero-effect assertions in tests/hooks/test_inbox.py.","introduced_in_round":12,"location":"§4.1.18; crates/ghook/src/dispatch.rs::run_gobby_owned enqueue-only branch; hooks/inbox.py::drain_hook_inbox_once","prevention":"For each drain rejection, assert envelope file/lease state, retry count/backoff, adapter/effect non-execution, receipt state, diagnostics, startup-barrier settlement, expiry, and restart recovery.","principle":"Every durable queue rejection needs a terminal, retry, or quarantine state with bounded ownership.","root_cause":"Round 12 required enqueue-only capability gating at drain without defining the rejected envelope's durable state. The current drain deletes on success and retains every non-2xx indefinitely.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r12-webchat-hook-identity-handoff","causal_section_ids":["4.1","5.2"],"check_key":"webchat-canonical-hook-identity-handoff","description":"A stale or wrong web_chat session ID with the expected session_type can be adopted even when its project, source, machine, or persisted workspace disagrees with the hook envelope. The pre-created flow can then bind conversationId or mutate transcript/workspace metadata before §2.2 classification and §3.1 workspace ownership are protected.","finding_id":"agy-r13-canonical-session-preflight-validation","fix":"State each identity producer/channel explicitly, then atomically validate the selected row's project_id, source=agy, machine_id, session_type=web_chat, and persisted workspace/tombstone state before startup claim, conversation binding, transcript classification, or metadata mutation. Preserve the pre-created row's workspace unless an explicit project/worktree-switch writer changes it. Add wrong-project/source/machine/worktree, tombstoned-workspace, pending-transcript, and concurrent-switch cases.","introduced_in_round":12,"location":"§§2.2, 3.1, 4.1 resolve-or-adopt preflight, and 5.2.14 production identity handoff","prevention":"For every pre-created session path, test canonical-id mismatch for project, source, machine, session_type, workspace/worktree, and tombstone before claim allocation, conversation binding, transcript classification, or metadata writes.","principle":"A canonical-session hint must be fully validated before it binds provider identity or mutates durable session metadata.","root_cause":"Round 12 made GOBBY_SESSION_ID authoritative before external-id fallback but required validation only of session_type. GOBBY_PROJECT_ID travels separately, source comes from ghook configuration, machine identity is injected independently, and §3.1 makes workspace identity durable.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"4c7a6ecf-fb82-48e4-965c-1c8dad5223e4","round":13,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 14** `kind: verification`

- reviewer_run: 981df30a-7d39-4c18-b55f-5e1faffdfe85
- reviewer_session: b3761f07-3230-4da5-9144-a87a9ff710ce
- verdict: needs_review
- findings:
- agy-r14-rule-direct-suite-targets / blocking / the direct behavioral suites for the three edited rule templates were untargeted; test_rule_models.py covers serialization only
- agy-r14-bundled-rule-manifest-target / blocking / the four edited YAMLs have SHA-256 entries in bundled_content_manifest.json enforced by a committed-manifest parity test, and the manifest was untargeted
- agy-r14-rule-migration-activation-contract / blocking / the preserved-row disposition migration named no production entrypoint, transaction/idempotency contract, or ordering before receipt-capability activation
- agy-r14-envelope-terminalization-retention / blocking / the post-prune duplicate-ack answer depended on an undefined envelope-terminalization record; the processed-envelope markers are durable and unpruned
- agy-r14-legacy-capability-envelope-contract / blocking / inbox-envelope.v1.schema.json is additionalProperties:false and untargeted, and pre-field envelopes had no absence semantics
- agy-r14-hook-quarantine-retention-owner / blocking / _quarantine_or_warn writes envelope plus .meta.json sidecar and nothing prunes either; "bounded retention" had no owner, horizon, or batch contract
- agy-r14-session-start-single-authority / blocking / _session.py fires SESSION_START through _fire_lifecycle fire-and-forget for every provider, double-dispatching against 4.1's synthetic SESSION_START
- resolution_notes: All 7 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — all three direct rule
  suites exist (tests/workflows/test_memory_lifecycle_rules.py, test_plan_mode_rules.py,
  test_skill_discovery_rules.py), the manifest entry at
  bundled_content_manifest.json:346 with the parity test at
  tests/test_build_backend.py:507, sync_bundled_rules reached from the installer
  registry (cli/installers/shared.py:271/366), the direct CLI
  (cli/workflows/manage.py:95), and MCP reload (mcp_proxy/tools/workflows/_import.py:170)
  with the daemon-startup owner at runner_init/storage.py:163 via
  sync_bundled_content_to_db, every unlink in envelope_dedupe.py confined to
  processing-claim flows and atomic-write temp cleanup (terminal processed markers
  never age-pruned), additionalProperties:false at inbox-envelope.v1.schema.json:16,
  _quarantine_file writing the .meta.json sidecar at inbox.py:58-63 with no
  retention/prune reference anywhere in the module, and the fire-and-forget
  SESSION_START pre-fire at _session.py:931-946. Repairs: §4.1 Targets gained the
  three direct rule suites, bundled_content_manifest.json,
  inbox-envelope.v1.schema.json, runner_init/storage.py, sync_bundled_content_to_db,
  manage.py, _import.py, and test_rule_yaml_sync.py; the classification prose and
  4.1.17 gained the direct-suite grouping assertions, manifest regeneration under the
  parity test, and the ordered daemon-startup migration trigger — definition-version
  CAS, zero-write second run, every caller propagating the diagnostic, activation
  blocked on ambiguous or partial rows; the retention prose and 4.1.19 replaced the
  envelope-terminalization record with a terminal idempotent no-op in the dedicated
  receipt consumer (absent receipt_id executes nothing and writes no record); the
  strict-protocol prose and 4.1.18 gained pre-field absence semantics admitted by the
  v1 schema (absence means legacy/below-floor, never malformed, tested through all
  three transport paths) and the named HOOK_QUARANTINE_RETENTION_WINDOW lifecycle —
  sidecar-persisted timestamp, strict-after cutoff, coherent payload+sidecar
  bounded-batch pruning with orphan-pair recovery in the start_periodic_tasks loop;
  §5.3 extended the single-authority contract to SESSION_START with the
  provider-conditional _session.py pre-fire suppressed for AGY and the end-to-end
  first-PreInvocation exactly-once case (5.3.5 reworded, _session.py joined 5.3
  Targets ordered after 3.1); V2 gained the committed-manifest parity run and the
  disposition-migration activation precondition.

```json plan-review-round
{"evidence_id":"c5f75af5-69de-4a3b-b299-ad71e1571a34","plan_hash":"9368e1a4fe35a59318d4127ecef3fcd254ef900cd1e48c0f5fcffb8963972e41","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f1a960fe151b280a3701a684b475b21f4815a115de5e867de207b9de931bdca8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":7,"total":11},"evidence_id":"c5f75af5-69de-4a3b-b299-ad71e1571a34","lanes":[{"candidate_count":2,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"856b2ce4a5d4b170bb160e9e30fda5d46b0c9d01a56fe14766530ba8e43cc7d4","status":"valid"},"source_digest":"a16e885dd84241dfa005b5e713690875bab6119d7af98be2cbb6e2be3d962fb0","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r13-rule-disposition-propagation","causal_section_ids":["4.1"],"check_key":"rule-disposition-direct-suite-target-closure","description":"The disposition sweep edits memory-capture-nudge, handle-plan-mode-entry, and discover-skill-hubs-on-turn-start, yet §4.1 omits tests/workflows/test_memory_lifecycle_rules.py, test_plan_mode_rules.py, and test_skill_discovery_rules.py. Those suites directly load and assert the affected rules; test_rule_models.py covers serialization only.","finding_id":"agy-r14-rule-direct-suite-targets","fix":"Add the three direct suites to §4.1 Targets with scope reasons, and extend them to assert each edited rule's eager/on_receipt payload grouping, including both handle-plan-mode-entry rules.","introduced_in_round":13,"location":"§4.1 Targets and acceptance 4.1.17","prevention":"For every edited bundled rule, trace the YAML to its direct sync/behavior suite and require that suite in the same deliverable's Targets.","principle":"A producer-level wire or persistence change must target the direct behavioral suites for every edited producer.","root_cause":"Round 13 added the four bundled YAML producers and the RuleEffect model suite, while omitting the existing suites that load and assert those specific rules.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r13-rule-disposition-propagation","causal_section_ids":["4.1"],"check_key":"bundled-content-manifest-target-closure","description":"All four edited one-shot YAMLs have entries in src/gobby/install/bundled_content_manifest.json, and test_committed_bundled_content_manifest_matches_shared_tree enforces exact parity. The manifest must change when those YAML hashes change, but it is absent from §4.1 Targets.","finding_id":"agy-r14-bundled-rule-manifest-target","fix":"Add src/gobby/install/bundled_content_manifest.json to §4.1 Targets, require regeneration after the YAML edits, and add the focused committed-manifest parity test to V2 validation.","introduced_in_round":13,"location":"§4.1 Targets; src/gobby/install/bundled_content_manifest.json","prevention":"For each changed path under install/shared, check bundled_content_manifest.json and the committed-manifest parity test before finalizing Targets.","principle":"Every committed generated artifact whose exact contents change with a targeted source must be owned by the same deliverable and validated by its parity check.","root_cause":"Round 13 targeted four bundled YAML files without including their committed SHA-256 manifest.","section_id":"4.1","severity":"blocking"},{"category":"bad-sequencing","causal_finding_id":"agy-r13-rule-disposition-propagation","causal_section_ids":["4.1"],"check_key":"rule-disposition-migration-activation-order","description":"The plan does not say which production path runs the user/project-row migration or guarantees it completes before the receipt capability activates. Existing installer, CLI sync, and MCP reload callers handle sync results differently, and no idempotent or atomic failure contract prevents partial writes or lost concurrent edits.","finding_id":"agy-r14-rule-migration-activation-contract","fix":"Name one production migration entrypoint that runs before V2 receipt-capability activation; preflight the full candidate set, apply writes transactionally or by definition-version CAS, make a second run perform zero writes, and block activation on ambiguous or partial failure with one propagated diagnostic. Target the affected installer/CLI/MCP callers plus tests/workflows/test_rule_yaml_sync.py and focused ownership-row cases.","introduced_in_round":13,"location":"§4.1 disposition migration prose, Targets, and V2 activation ordering","prevention":"For each protocol-coupled migration, name its production caller, order it before activation, and test first run, repeated run, ambiguous rollback, partial failure, and concurrent edits.","principle":"A data migration that makes persisted rows safe for a new runtime protocol must have one deterministic production trigger and complete before protocol activation.","root_cause":"Round 13 assigned the preserved-row rewrite to the generic sync path without choosing an entrypoint, transaction boundary, concurrent-edit fence, or repeat-run contract.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-receipt-retention-state-contract","causal_section_ids":["4.1"],"check_key":"receipt-post-prune-terminal-authority","description":"The plan relies on an envelope-terminalization record after a receipt row is pruned, but names no representation, key, lookup owner, or retention policy. The existing processed-envelope markers in envelope_dedupe.py are durable and unpruned, so treating them as this record would merely transfer unbounded growth.","finding_id":"agy-r14-envelope-terminalization-retention","fix":"Remove the secondary-record dependency: define an acknowledgment whose receipt_id is absent after pruning as a terminal idempotent no-op in the dedicated receipt consumer, with no adapter or effect execution and no new record. Test late duplicate acknowledgment after pruning, restart, and acknowledgment-versus-prune races.","introduced_in_round":13,"location":"§4.1.19 receipt-retention prose and acceptance","prevention":"For every pruned idempotency row, define behavior for a late duplicate after the primary row is gone and verify that no unbounded secondary tombstone is required.","principle":"Post-prune idempotency must terminate at a named bounded authority; cleanup cannot depend on an unspecified second durable record.","root_cause":"Round 13 answered duplicate acknowledgments after receipt pruning with an undefined envelope-terminalization record.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-runtime-capability-request-provenance","causal_section_ids":["4.1"],"check_key":"request-capability-absent-envelope","description":"An envelope written by a pre-field ghook necessarily lacks response-capability. The plan never states whether absence parses as below-floor or fails as malformed, and inbox-envelope.v1.schema.json has additionalProperties=false with a fixed field set yet is absent from Targets. The promised old-envelope-after-reinstall test can therefore fail before reaching quarantine or accidentally bypass the gate.","finding_id":"agy-r14-legacy-capability-envelope-contract","fix":"Add crates/ghook/schemas/inbox-envelope.v1.schema.json to §4.1 Targets; admit the optional response-capability property for v1 replay, define absence as legacy/below-floor, reject direct and detached requests before adapter execution, and terminally quarantine enqueue-only requests. Test missing-field parsing and all three paths.","introduced_in_round":13,"location":"§4.1.18 and §4.1 Targets; crates/ghook/schemas/inbox-envelope.v1.schema.json","prevention":"For each added envelope field, inspect the wire schema and test missing, malformed, below-floor, current, and replayed values through every transport path.","principle":"A request-carried compatibility field needs explicit semantics for persisted requests created before the field existed, including the schema that admits them.","root_cause":"Round 13 required every new envelope to carry the capability and an old-envelope-after-reinstall race, while leaving pre-field envelope parsing and the v1 JSON schema unchanged in the plan.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-enqueue-floor-disposition","causal_section_ids":["4.1"],"check_key":"hook-quarantine-retention-owner","description":"The existing quarantine path writes the envelope and a .meta.json sidecar and never prunes either. §4.1 promises bounded retention but assigns no constant, clock, cutoff boundary, purge operation, schedule, batch limit, or orphan-pair recovery; runner_lifecycle_periodic.py is targeted only for receipt-row pruning.","finding_id":"agy-r14-hook-quarantine-retention-owner","fix":"Define a HOOK_QUARANTINE_RETENTION_WINDOW fixed default on wall-clock time with an exact cutoff, persist the quarantine timestamp, add a bounded pruner that coherently removes payload and sidecar, and register its explicit periodic/startup owner. Test inside/exact/outside cutoff, restart, orphan files, bounded batches, and concurrent quarantine versus prune.","introduced_in_round":13,"location":"§4.1.18; src/gobby/hooks/inbox.py quarantine path","prevention":"Whenever quarantine is introduced, enumerate timestamp creation, exact cutoff, periodic/startup owner, batch limit, payload-sidecar consistency, restart, and concurrent quarantine/prune behavior.","principle":"A bounded quarantine requires a named horizon, timestamp authority, pruning owner, batch bound, and coherent payload/metadata deletion.","root_cause":"Round 13 added the phrase bounded retention to the terminal quarantine branch without specifying any cleanup lifecycle.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-webchat-lifecycle-double-dispatch","causal_section_ids":["5.2","5.3"],"check_key":"webchat-session-start-single-authority","description":"Web-chat creation currently fires SESSION_START through _fire_lifecycle for every provider, while §4.1 requires AgyAdapter.handle_native to synthesize SESSION_START from the first native PreInvocation. Because §5.3 grants native authority only for four later event types, AGY can evaluate startup twice, and the fire-and-forget managed path can mark startup context delivered before the receipt-bearing native response reaches the child.","finding_id":"agy-r14-session-start-single-authority","fix":"Extend the §5.3 native-authority contract to AGY SESSION_START, target the provider-conditional fire-and-forget seam in websocket/chat/_session.py under §5.3, and suppress that managed pre-fire for AGY. Add an end-to-end first-PreInvocation case proving exactly one synthetic SESSION_START, exactly one BEFORE_AGENT, and startup context/system message delivered and committed only through the native receipt path.","introduced_in_round":13,"location":"§§4.1.2, 4.1.10, 5.2.14, and 5.3.5; websocket/chat/_session.py","prevention":"Map every provider lifecycle event, including create/start/end and synthesized events, from source signal to one evaluator and assert response ownership plus exactly-once effects.","principle":"Every lifecycle event for one provider session needs one workflow-effect authority, including synthesized startup events.","root_cause":"Round 13 limited the single-authority repair to BEFORE_AGENT, BEFORE_TOOL, AFTER_TOOL, and STOP while §4.1 already synthesizes SESSION_START from PreInvocation.","section_id":"5.3","severity":"blocking"}],"reviewer_session":"b3761f07-3230-4da5-9144-a87a9ff710ce","round":14,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 15** `kind: verification`

- reviewer_run: 7448a853-d026-4036-82de-2c46a9dcac70
- reviewer_session: 9172608a-8583-4bba-9d68-84b79723b16e
- verdict: needs_review
- findings:
- agy-r15-rule-migration-startup-activation-test / blocking / the daemon-startup migration trigger is dev-mode-only today and no startup-seam test owned the ordering, abort, and repeat outcomes
- agy-r15-retention-loop-registration-test / blocking / receipt and quarantine pruning had loop-body tests but no scheduler-boundary registration proof in the injected-loop harness
- agy-r15-rule-migration-caller-propagation-tests / blocking / reinstall_workflows reduces sync results to counts and always notifies reload; reload_cache clears the cache before syncing — both direct caller suites were untargeted
- agy-r15-diagnose-criticality-matrix-target / blocking / diagnose.rs's module-local criticality matrix pins Codex and Qwen Stop as critical and was outside 2.3's Targets
- agy-r15-envelope-diagnostics-literal-target / blocking / the exhaustive Envelope literal in diagnostics.rs is a compile-time consumer of the new capability field and was untargeted
- agy-r15-capability-fixture-blast-radius / blocking / success-path raw-envelope fixtures across route, E2E, and server suites omit the capability field and would fail the new gate
- resolution_notes: All 6 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting —
  sync_bundled_content_to_db invoked only under the runner._dev_mode branch of
  init_storage_and_config (runner_init/storage.py), the injected-loop harness at
  test_runner_maintenance_startup.py:190-225, reinstall_workflows (manage.py:37-84)
  summing counts and unconditionally calling _notify_daemon_reload, reload_cache
  (_import.py:130-200) clearing the cache before sync with errors only annotated,
  the criticality matrix at diagnose.rs:186-202 asserting ("codex","Stop",true) and
  ("qwen","Stop",true), the exhaustive Envelope literal at diagnostics.rs:310-320,
  and the raw-envelope sweep confirming untargeted success-path constructors in
  test_hook_session_metadata.py (nine sites), test_hooks_droid_dispatch.py,
  e2e/conftest.py:825-833, e2e/test_daemon_auth.py,
  test_execution_session_end_cleanup.py, test_hold_open_gate.py,
  test_http_endpoints.py, and test_http_server.py:109-116. Repairs: 2.3 targeted
  diagnose.rs with the wholesale matrix update (new 2.3.6). 4.1 Targets gained the
  startup-seam suite (test_runner_lifecycle.py), the retention-registration harness
  (test_runner_maintenance_startup.py), both caller-propagation suites
  (tests/cli/test_cli_workflows.py, tests/workflows/test_sync.py), the diagnostics
  Envelope fixture (crates/ghook/src/diagnostics.rs), and the eight raw-envelope
  fixture suites. The migration-trigger prose states the dev-mode-only reality and
  the unconditional widening with startup-seam cases; the caller-propagation prose
  names both callers' current behavior and the typed-failure/suppression contract;
  4.1.17 gained the startup-order and caller-seam clauses; 4.1.18 gained the
  registration proof, the diagnostics-literal ownership, and the fixture
  blast-radius enumeration; 4.1.19 gained the registration proof.

```json plan-review-round
{"evidence_id":"42ba83a3-863b-4d0b-9da1-a406598c5a84","plan_hash":"a8ea3dd7075469561c7702f8b218ea004517bd5ccba3496f9eebfd3481ac0a66","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"8a7aa72af979df56fd5ecfa93a7861ebe7122230130318058ecce20c3c3b7ea3","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":6,"total":13},"evidence_id":"42ba83a3-863b-4d0b-9da1-a406598c5a84","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"bba96011f5742b4cabb8286e5bd0f9aa877a47b7e56573bcec5671265dcf7f67","status":"valid"},"source_digest":"ab19ccd8e04e8154e7dbe49c90d89d029b73747d5f80f92cc84d1df942ce3506","version":1},"findings":[{"category":"weak-testability","check_key":"rule-migration-startup-order-test","description":"Section 4.1 says daemon-startup sync_bundled_content_to_db in src/gobby/runner_init/storage.py completes before hook service and receipt activation. Current init_storage_and_config invokes that sync only in the runner._dev_mode branch at lines 154-166, so this is a real control-flow change. The section targets storage.py and tests/workflows/test_rule_yaml_sync.py, but no startup test; tests/test_runner_lifecycle.py::TestRunGobbyFunction is the existing run_gobby/init seam and currently patches init_storage_and_config.","finding_id":"agy-r15-rule-migration-startup-activation-test","fix":"Add tests/test_runner_lifecycle.py::* or a focused runner-init storage test module to §4.1 Targets and acceptance. Prove an ordinary non-dev daemon runs the migration exactly once, an ambiguous/partial diagnostic aborts before hook service, and a clean or zero-write repeat proceeds in the required order.","location":"§4.1 Targets and acceptance 4.1.17; src/gobby/runner_init/storage.py::init_storage_and_config","prevention":"Whenever a migration is declared an activation barrier, target a startup-level test that covers clean, repeated, and blocking outcomes before service availability.","principle":"A protocol activation precondition must be proven at its concrete startup owner, including the fail-closed branch and ordering boundary.","root_cause":"Section 4.1 owns the production startup edit and inner migration cases, while no same-deliverable test owns the daemon startup seam.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"retention-loop-registration-test","description":"Acceptance 4.1.18 and 4.1.19 require quarantine and receipt pruning to run from src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks. The existing direct harness tests/test_runner_maintenance_startup.py::test_periodic_start_schedules_test_schema_sweep_loop (lines 190-225) injects the loop registry and verifies scheduling, yet that file is absent from §4.1 Targets. Inbox and receipt-storage tests can pass while neither maintenance loop is registered.","finding_id":"agy-r15-retention-loop-registration-test","fix":"Add tests/test_runner_maintenance_startup.py::* to §4.1 Targets and acceptance. Extend its injected-loop harness to prove both retention owners register and schedule exactly once with the intended database/shutdown inputs and join the tracked periodic-task set.","location":"§4.1 Targets and acceptance 4.1.18-4.1.19; src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks","prevention":"For every new periodic owner, target the direct injected-loop harness and assert registration, dependencies, task tracking, and shutdown behavior.","principle":"Lifecycle behavior requires a test at the scheduler/registrar boundary in addition to tests of the loop body.","root_cause":"Section 4.1 targets start_periodic_tasks but assigns all receipt and quarantine pruning tests to storage/inbox suites, leaving registration and shutdown tracking unowned.","section_id":"4.1","severity":"blocking"},{"category":"weak-testability","check_key":"rule-migration-caller-propagation-test","description":"Current src/gobby/cli/workflows/manage.py::reinstall_workflows (lines 37-84) reduces _run_sync results to counts and always calls _notify_daemon_reload; src/gobby/mcp_proxy/tools/workflows/_import.py::reload_cache (lines 130-200) clears the cache first and ignores returned sync_result errors unless an exception is raised. Section 4.1 promises both callers propagate the blocking disposition diagnostic, yet omits their direct suites tests/cli/test_cli_workflows.py and tests/workflows/test_sync.py.","finding_id":"agy-r15-rule-migration-caller-propagation-tests","fix":"Add the direct CLI manage and MCP reload suites to §4.1 Targets and acceptance. Inject ambiguous and partial rule-sync results; assert a typed failure is returned, reload/notification or stale-cache activation is suppressed, and the rule/effect diagnostic is preserved. Include the shared installer/CLI-sync caller tests if their aggregation logic changes.","location":"§4.1 Targets and acceptance 4.1.17; src/gobby/cli/workflows/manage.py::reinstall_workflows; src/gobby/mcp_proxy/tools/workflows/_import.py::reload_cache","prevention":"When a plan claims identical diagnostic propagation, inventory each result-translating caller and target its direct test seam with the same injected failure.","principle":"A blocking diagnostic contract must be asserted at every user-visible caller that translates or aggregates the inner result.","root_cause":"The repair targets manage.py and _import.py production paths but only assigns tests to the inner rule-sync suite.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"critical-hook-matrix-test-target","description":"crates/ghook/src/diagnose.rs::terminal_hook_criticality_matches_supported_cli_contracts (lines 186-202) currently asserts Codex Stop and Qwen Stop are critical. Section 2.3 changes both to noncritical and adds lifecycle rows, but diagnose.rs is absent from Targets, so the Rust suite will fail or retain a stale partial matrix.","finding_id":"agy-r15-diagnose-criticality-matrix-target","fix":"Add crates/ghook/src/diagnose.rs::* to §2.3 Targets and update terminal_hook_criticality_matches_supported_cli_contracts to the final six-provider matrix, including noncritical Codex/Qwen Stop and the new critical lifecycle rows.","location":"§2.3 Targets; crates/ghook/src/diagnose.rs::terminal_hook_criticality_matches_supported_cli_contracts","prevention":"For every revised enum or policy matrix, search all literals and table-driven assertions, including unit tests embedded in production modules.","principle":"A closed policy matrix and every executable copy of that matrix must change in the same deliverable.","root_cause":"The blast-radius inventory stops at CliConfig/action and contract.rs, omitting a module-local diagnose test with hard-coded criticality values.","section_id":"2.3","severity":"blocking"},{"category":"traceability","check_key":"envelope-struct-literal-closure","description":"crates/ghook/src/diagnostics.rs::envelope (lines 310-320) constructs Envelope with every current field and no struct-update syntax. Adding the planned response-capability member in crates/ghook/src/envelope.rs::Envelope therefore creates a Rust compile error, while diagnostics.rs is absent from §4.1 Targets.","finding_id":"agy-r15-envelope-diagnostics-literal-target","fix":"Add crates/ghook/src/diagnostics.rs::* to §4.1 Targets. Migrate its fixture through Envelope::new or explicitly populate the response-capability field, and assert diagnostics preserve/report the new provenance as appropriate.","location":"§4.1 Targets; crates/ghook/src/diagnostics.rs::envelope","prevention":"Before adding a struct field, inventory all constructors and exhaustive literals and target each compile-time consumer.","principle":"Adding a field to a statically constructed wire type requires every exhaustive struct literal in the repository to be owned by the change.","root_cause":"Section 4.1 targets the Envelope definition and transport tests while omitting the diagnostics module's exhaustive fixture.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"hook-envelope-capability-fixture-closure","description":"Section 4.1 makes an absent response-capability field legacy/below-floor and rejects direct POSTs before adapter execution. Existing untargeted success fixtures still omit it: tests/servers/routes/mcp/test_hook_session_metadata.py::_post_claude_hook (lines 24-52), tests/servers/routes/test_hooks_droid_dispatch.py::test_execute_hook_dispatches_droid_adapter (lines 20-57), and tests/e2e/conftest.py::CLIEventSimulator._hook_envelope (lines 825-833). Their current 200/adapter-execution expectations conflict with the planned gate.","finding_id":"agy-r15-capability-fixture-blast-radius","fix":"Add those direct fixtures and the remaining affected raw-envelope constructors from the verified repository inventory to §4.1 Targets. Give success paths the supported response-capability value; keep focused missing-field cases that assert pre-execution rejection or terminal enqueue-only quarantine.","location":"§4.1 Targets and acceptance 4.1.18; direct-hook and E2E envelope fixtures","prevention":"For every added envelope gate, inventory raw schema_version envelope constructors across unit, integration, and E2E tests and classify each as current-capability success or legacy rejection.","principle":"A new request gate must update every success-path fixture that constructs the gated wire object, while retaining explicit legacy rejection cases.","root_cause":"Section 4.1 targets the new AGY and inbox matrices but omits incumbent direct-hook and shared E2E envelope constructors.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"9172608a-8583-4bba-9d68-84b79723b16e","round":15,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 16** `kind: verification`

- reviewer_run: 56954b45-b298-42af-8472-7d53e9114d53
- reviewer_session: 4704d7b9-f258-4628-b977-62e28a889e24
- verdict: needs_review
- findings:
- agy-r16-cli-criticality-test-closure / blocking / cli_config.rs's module-local codex_stop_is_critical and qwen_current_critical_hooks pin Stop critical and were outside 2.3's Targets
- agy-r16-retention-task-shutdown-ownership / blocking / the two retention loops were registered but absent from GobbyRunner's typed task inventory and _cancel_periodic_tasks' exhaustive cancellation tuple
- agy-r16-startup-sync-scope / blocking / widening the whole sync_bundled_content_to_db aggregator to unconditional startup double-syncs build profiles and drags a Path.cwd() user-template import across the lifecycle gate for a migration needing neither
- agy-r16-reinstall-cli-exit-contract / blocking / reinstall_workflows is a Click command declared -> None, so a returned typed failure still exits 0
- agy-r16-reinstall-failure-atomicity / blocking / reinstall_workflows commits the Gobby-owned deletion before _run_sync, so a propagated disposition failure strands a damaged registry
- resolution_notes: All 5 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting — `codex_stop_is_critical`
  (cli_config.rs:92-98) and `qwen_current_critical_hooks` (:101-111) both asserting
  `is_critical_hook("Stop")` with the Qwen test also pinning
  `malformed_input_exit_code("Stop") == 2`; the exhaustive 23-attribute
  `periodic_task_attrs` tuple in `_cancel_periodic_tasks`
  (runner_lifecycle_shutdown.py:253-345) beside the typed task declarations on
  `GobbyRunner` (runner.py:94-126) and the existing tests/test_runner_shutdown.py;
  `sync_bundled_content_to_db` (installers/shared.py:241-337) syncing eight domains
  with its docstring pinning install as the single import point and
  `_sync_user_templates_to_db` reading `Path.cwd()` (shared.py:358) in non-dev
  production, while `init_storage_and_config` already syncs build profiles
  unconditionally (storage.py:174-178); and `reinstall_workflows` (manage.py:37-84)
  declared `-> None` under Click with its Gobby-owned DELETE committed in a
  `db.transaction()` block before `_run_sync` runs. Repairs: 2.3 targeted both
  module-local criticality tests with the noncritical-Stop and malformed-input-exit
  assertions folded into 2.3.4. 4.1 gained the shutdown-lifecycle contract — typed
  `GobbyRunner` attributes, initialization in `runner_init/storage.py`, failure
  tracking, and cancel-and-await in `_cancel_periodic_tasks` before hook storage and
  database teardown, with `tests/test_runner_shutdown.py` targeted and 4.1.18/4.1.19
  extended. The startup trigger narrowed: the full aggregator stays
  install/CLI/dev-mode-only and `init_storage_and_config` instead calls a narrow
  rule-disposition migration/validation entry point in `workflows/sync_rules.py`
  unconditionally before hook service, with non-invocation of the other bundled
  domains and user-template import proven at the startup seam (prose, 4.1.17, V2
  activation item, and the storage.py scope-reason reworded). The CLI caller contract
  gained Click semantics — blocking failure raises `click.ClickException` with
  `CliRunner` exit code 1 asserted for both injected shapes — and reinstall
  atomicity: disposition validation preflights before the committed deletion (or
  delete-plus-reinstall runs atomically), with seeded-row byte-for-byte state,
  suppressed notification, and safe retry asserted after both failure shapes
  (prose and 4.1.17 reworded).

```json plan-review-round
{"evidence_id":"21fb0ca3-e90a-44cc-8391-95a2ae71f32f","plan_hash":"c9ae5c717e0a092c8738a728b85ff424cfe89604bfed10dbb40aaf8c689685c0","round_number":16,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"e6db62028e6c43f7289893bfd2925d36ea4f23213bf3d8e55bf776c288f9ff9b","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":5,"total":6},"evidence_id":"21fb0ca3-e90a-44cc-8391-95a2ae71f32f","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"33169f010a94f28507a0377eae1237fc9ee4e17072a9158548c1c78c42666780","status":"valid"},"source_digest":"0c81ee99047848fab913039a4f8b9e9891eafa7b9decacb3d8a6d09c6b58ecab","version":1},"findings":[{"category":"traceability","check_key":"critical-hook-matrix-local-test-closure","description":"crates/ghook/src/cli_config.rs::codex_stop_is_critical and ::qwen_current_critical_hooks both assert Stop is critical. Section 2.3 changes both rows to noncritical and promises a per-provider assertion, yet neither test symbol is targeted. The Round 15 diagnose.rs wildcard does reach its other module-local AGY loop, so the remaining verified gap is cli_config.rs.","finding_id":"agy-r16-cli-criticality-test-closure","fix":"Add crates/ghook/src/cli_config.rs::codex_stop_is_critical and ::qwen_current_critical_hooks to §2.3 Targets, and require both tests to assert the final noncritical Stop rows and corresponding malformed-input exit behavior.","location":"§2.3 Targets and acceptance 2.3.4; crates/ghook/src/cli_config.rs:92-111","prevention":"For each closed provider matrix, inventory production rows plus every module-local, integration, and contract assertion; target every literal whose expected row changes.","principle":"Every executable copy of a changed provider-policy matrix must be owned and revised by the same deliverable.","root_cause":"The target sweep owns CliConfig::for_cli and two provider tests, then repairs diagnose.rs wholesale, while leaving the Codex and Qwen module-local assertions outside §2.3 Targets.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"retention-task-shutdown-lifecycle","description":"The receipt-retention and quarantine-pruning loops can be registered yet remain absent from GobbyRunner's typed task inventory and runner_lifecycle_shutdown.py::_cancel_periodic_tasks. tests/test_runner_maintenance_startup.py proves scheduling only; it cannot detect a task still running when hook storage or the database closes.","finding_id":"agy-r16-retention-task-shutdown-ownership","fix":"Add the two task attributes to src/gobby/runner.py and initialize them in runner_init/storage.py; target runner_lifecycle_shutdown.py::_cancel_periodic_tasks and tests/test_runner_shutdown.py. Require both tasks to join failure tracking, be cancelled and awaited exactly once, and finish before hook/database teardown, including idempotent shutdown and non-terminating loop doubles.","location":"§4.1 Targets and acceptance 4.1.18-4.1.19; src/gobby/runner.py:87-137; src/gobby/runner_lifecycle_shutdown.py:253-345","prevention":"For each new periodic loop, trace declare, initialize, create, track, failure callback, cancel, await, clear, idempotent shutdown, and dependency-close ordering.","principle":"Every daemon periodic task needs typed declaration, initialization, failure tracking, cancellation, and awaiting before its dependencies close.","root_cause":"The plan owns the two retention loops only at _default_loops/start_periodic_tasks and the registration harness, while GobbyRunner task attributes and _cancel_periodic_tasks are separate exhaustive inventories.","section_id":"4.1","severity":"blocking"},{"category":"over-engineering","causal_finding_id":"agy-r15-rule-migration-startup-activation-test","causal_section_ids":["4.1"],"check_key":"startup-rule-migration-scope","description":"sync_bundled_content_to_db synchronizes eight bundled domains and, in production, imports project/global user rule and variable files from Path.cwd(); init_storage_and_config then syncs build profiles again. Section 4.1 needs only the persisted rule-disposition migration before receipt activation. It names no startup consumer for the other seven bundled domains or filesystem import, and its tests cover only rule migration outcomes.","finding_id":"agy-r16-startup-sync-scope","fix":"Keep the full aggregator on install, explicit CLI sync, and the existing dev-mode startup path. Add or reuse a narrow rule-disposition migration/validation entry point for unconditional production startup before hook activation, and make tests prove unrelated bundled sync functions and user-template import are not invoked while first-run, zero-write repeat, and abort ordering remain covered.","introduced_in_round":15,"location":"§4.1 startup-trigger prose and acceptance 4.1.17; src/gobby/runner_init/storage.py:154-166; src/gobby/cli/installers/shared.py:241-399","prevention":"Before moving a helper across a lifecycle or mode gate, enumerate every operation behind it and require a named consumer and focused test for each; otherwise introduce a narrower call.","principle":"A startup activation barrier should execute only mutation domains with a concrete startup consumer.","root_cause":"Round 15 crossed the dev-mode gate with the installer-wide aggregator because it contains the needed rule sync, without inventorying the aggregator's other production side effects.","section_id":"4.1","severity":"blocking"},{"category":"missing-requirement","causal_finding_id":"agy-r15-rule-migration-caller-propagation-tests","causal_section_ids":["4.1"],"check_key":"rule-reinstall-cli-failure-exit","description":"reinstall_workflows is a Click command declared -> None. Preserving a typed rule diagnostic and suppressing _notify_daemon_reload can still leave CliRunner.exit_code at 0 because an ordinary callback return does not establish a failing process status. Acceptance 4.1.17 does not require a nonzero CLI exit.","finding_id":"agy-r16-reinstall-cli-exit-contract","fix":"Require ambiguous or partial migration failure to raise click.ClickException or an equivalent Click failure with the preserved diagnostic, skip daemon notification, and exit 1. Add CliRunner assertions for exit_code == 1 for both injected failure shapes.","introduced_in_round":15,"location":"§4.1 acceptance 4.1.17; src/gobby/cli/workflows/manage.py:27-84; tests/cli/test_cli_workflows.py","prevention":"Trace each typed failure through CLI, HTTP, and MCP wrappers to its observable status, body, or exit code, and assert that boundary directly.","principle":"A typed failure must survive framework wrappers and become the user-visible process failure contract.","root_cause":"Round 15 specified a typed failure returned from reinstall_workflows but did not account for Click treating ordinary callback returns as a successful command invocation.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r15-rule-migration-caller-propagation-tests","causal_section_ids":["4.1"],"check_key":"rule-reinstall-failure-atomicity","description":"reinstall_workflows deletes the selected Gobby-owned definitions in a committed transaction before calling _run_sync. Under the newly tested ambiguous or partial rule failure, the command can return failure with notification suppressed after definitions were permanently removed or only partly restored, leaving stale daemon cache until restart and a damaged database afterward.","finding_id":"agy-r16-reinstall-failure-atomicity","fix":"Preflight disposition validation before deletion or make deletion plus the complete reinstall atomic. Seed a complete installed row set in both ambiguous and partial failure tests and assert byte-for-byte equivalent definition state afterward, no daemon notification, the preserved diagnostic, and safe retry.","introduced_in_round":15,"location":"§4.1 acceptance 4.1.17; src/gobby/cli/workflows/manage.py:37-112; tests/cli/test_cli_workflows.py","prevention":"For every propagated failure, walk all preceding committed mutations and test database state, external notifications, cache state, and retry behavior at the caller boundary.","principle":"A destructive replace operation must preserve its complete pre-call state when validation or replacement fails.","root_cause":"Round 15 added post-sync failure propagation and notification suppression, while the current command commits deletion before _run_sync can expose an ambiguous or partial result.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"4704d7b9-f258-4628-b977-62e28a889e24","round":16,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 17** `kind: verification`

- reviewer_run: 6055274f-4350-4bec-bc89-78532ee20eb3
- reviewer_session: d7d7b2d5-20ed-4021-9dc7-a360bcf04c10
- verdict: needs_review
- findings:
- agy-r17-codex-malformed-exit-owner / blocking / CliConfig::malformed_input_exit_code special-cases criticality only for Qwen while Codex inherits provider-wide exit 2, and the method was outside 2.3's Targets
- agy-r17-rule-writer-disposition / blocking / MCP create/update, HTTP create/full-replacement, CLI rule-file import, and sync_imported_definition persist rules post-activation with the eager default, recreating the commit-before-delivery hole the migration cleared
- agy-r17-dedupe-direct-suite / blocking / the eager dedupe contract lives in test_hook_manager_extra.py's TestDedupMemoryResults/TestDedupSkillResults, not the attributed test_hook_extracted_helpers.py, and was untargeted
- agy-r17-agent-preamble-direct-suite / blocking / test_agent_events_coverage.py asserts eager _agent_context_injected writes across first-prompt, persona-switch, stale-repair, and rehydration cases and was untargeted
- agy-r17-inbox-schema-mirror / blocking / the tracked root mirror schemas/inbox-envelope.v1.schema.json is byte-identical to the crate copy and was untargeted, leaving the public mirror stale
- agy-r17-receipt-ack-attempt-lineage / blocking / the ack wire carried only receipt_id plus original envelope id, so a delayed ack from an earlier carrying envelope could commit a re-prepared row before its current envelope is emitted
- agy-r17-precompact-supported-branch / blocking / 5.3 delegated the supported compaction branch to 5.2, which never specified parsing, callback, or dedupe, leaving the branch with no production owner
- resolution_notes: All 7 findings accepted in unattended mode; the coordinator verified
  every load-bearing claim against the code index before voting —
  `malformed_input_exit_code` (cli_config.rs:67-73) special-casing only
  `qwen && is_critical_hook` with Codex at provider-wide `json_error_exit_code: 2`
  and `run_gobby_owned` delegating malformed input to it; the four live rule
  ingresses (`create_rule`/`update_rule` in `_rules.py:173-318`,
  `create_rule_endpoint`/`update_rule_endpoint` in routes/rules.py:200-216/312-389,
  `import_rules` in cli/rules.py:248-275, `sync_imported_definition` in
  imports.py:29-69) none routing through the disposition migration;
  `TestDedupMemoryResults` (155-251) and `TestDedupSkillResults` (254-275) in
  test_hook_manager_extra.py asserting eager `claim_set_variable_values` while
  test_hook_extracted_helpers.py has zero dedupe cases; thirteen eager
  `_agent_context_injected` assertions at test_agent_events_coverage.py:210-463,
  with `_inject_agent_instructions_if_needed` (_agent.py:247-319) showing the
  prior-activity branch marks the guard and delivers no payload; the root
  `schemas/inbox-envelope.v1.schema.json` tracked and SHA-256-identical to the
  crate copy; the plan's own ack wire (receipt_id + original envelope id +
  session identity) against 4.1.19's same-row re-prepare; and 5.1/5.2 carrying
  no compaction parsing or callback requirement while 5.3 delegated to them.
  Repairs: 2.3 targeted `CliConfig::malformed_input_exit_code` with the
  criticality-driven split folded into 2.3.4 (critical lifecycle malformed
  input exits 2, noncritical Stop exits 1, pinned through `run_gobby_owned`).
  4.1 gained the shared write-time disposition classifier over all four
  post-activation rule ingresses with their four direct suites targeted and the
  prose/4.1.17 write-preservation clause; the dedupe direct-suite attribution
  corrected (extracted-helpers scope-reason narrowed to evaluator-result
  merging, test_hook_manager_extra.py targeted with staged-boundary cases);
  the preamble suite targeted with the prior-activity stale-repair branch
  classified eager (marks from prior-session evidence, no payload, no
  receipt); the root schema mirror targeted with a byte-parity assertion in
  4.1.18; and the delivery receipt gained current-attempt identity — a
  monotonic delivery generation stamped at every prepare/re-prepare, echoed in
  the ack, CAS on (receipt_id, generation, prepared), stale-generation acks
  terminal no-ops — across the wire prose, the receipt-effects table columns,
  4.1.15, and 4.1.19. The supported PRE_COMPACT branch gained production
  owners: 5.1 parses the recorded signal to a distinct module-level stream
  event (5.1.6), 5.2 invokes the separately keyed, deduplicated callback with
  a parse-to-`_fire_lifecycle` exactly-once test (5.2.15), and 5.3's prose and
  5.3.5 re-anchor to those owners.

```json plan-review-round
{"evidence_id":"ccd1587f-b0aa-4b67-a1d6-332b6a6d7718","plan_hash":"91e429bb3c524e695965fce7e9fd6c77323050ae38d8acf807761ec3de28ad5e","round_number":17,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f8d62a0b2aa0888fea19057e70b8b8f306fb15683e41922dbe2f25055ed2b61d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":7,"total":8},"evidence_id":"ccd1587f-b0aa-4b67-a1d6-332b6a6d7718","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"a026a01f252bf69b1caa4b0390068e2e1bf98130c48e6d7cb8570dc3b4082614","status":"valid"},"source_digest":"ec51e20367a2f54d1a8eaeb4ddf21bfe4b438fe9c7cba9e9cc8f08b2b3971306","version":1},"findings":[{"category":"traceability","causal_finding_id":"agy-r16-cli-criticality-test-closure","causal_section_ids":["2.3"],"check_key":"codex-malformed-input-policy-owner","description":"Section 2.3 now requires Codex Stop to be noncritical and to assert its corresponding noncritical malformed-input exit behavior, but its Targets stop at CliConfig::for_cli and the tests. Live CliConfig::malformed_input_exit_code special-cases criticality only for Qwen; Codex inherits provider-wide json_error_exit_code=2 for both Stop and lifecycle hooks, and run_gobby_owned delegates malformed input to this method. Editing for_cli alone cannot produce Stop=1 while preserving critical Codex lifecycle=2.","finding_id":"agy-r17-codex-malformed-exit-owner","fix":"Add crates/ghook/src/cli_config.rs::CliConfig::malformed_input_exit_code to §2.3 Targets. Specify critical Codex/Qwen lifecycle malformed input as exit 2 and noncritical Stop as exit 1, and extend both module-local tests plus crates/ghook/tests/contract.rs::malformed_stdin_uses_cli_specific_json_error_contract through run_gobby_owned.","introduced_in_round":16,"location":"§2.3 Targets and 2.3.4; crates/ghook/src/cli_config.rs:21-73,92-111; crates/ghook/src/dispatch.rs:68-74; crates/ghook/tests/contract.rs:18-53","prevention":"For each policy-matrix assertion, trace the runtime dispatch to the method that computes the observable exit/action and target that method plus its end-to-end row.","principle":"A behavioral acceptance that changes an executable branch must target the production owner and its boundary test.","root_cause":"Round 16 added Codex Stop malformed-input behavior to 2.3.4 and targeted the two module-local tests, while leaving CliConfig::malformed_input_exit_code outside the deliverable.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-rule-disposition-propagation","causal_section_ids":["4.1"],"check_key":"post-activation-rule-write-disposition","description":"The startup migration can make existing user/project one-shot rules receipt-safe, then a live writer can immediately recreate an unsafe eager rule. MCP create/update, HTTP create/full replacement, CLI sync_rule_file import, and generic sync_imported_definition all persist definitions independently of the planned startup trigger. Because missing disposition deserializes as eager, a newly written acknowledge_variable/success_variable or grouped one-shot guard can again commit before delivery.","finding_id":"agy-r17-rule-writer-disposition","fix":"Add one shared write-time disposition classifier/validator to §4.1 and target every rule create, full-definition update, and import ingress. Recognizable delivery suppressors must persist explicit on_receipt grouping before commit; ambiguous definitions must fail with the same rule/effect diagnostic. Add MCP, HTTP, CLI-file, and generic-import tests that create and replace affected rules after startup and prove no eager guard activates.","introduced_in_round":13,"location":"§4.1 disposition migration prose and 4.1.17; src/gobby/workflows/definitions.py:119-308; src/gobby/mcp_proxy/tools/workflows/_rules.py:173-318; src/gobby/servers/routes/rules.py:200-216,312-389; src/gobby/cli/rules.py:248-275; src/gobby/workflows/imports.py:29-69; src/gobby/workflows/sync_rules.py:59-79","prevention":"After defining a data migration, inventory every create, full-replacement, and import ingress reachable after activation and apply the same validation before commit.","principle":"A protocol-safety invariant established by migration must also be enforced on every future write that can recreate the unsafe state.","root_cause":"The preserved-row repair classifies definitions only during startup/bundled sync while RuleEffect deliberately retains an eager default and live rule writers remain outside the migration contract.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r12-response-visible-producer-sweep-incomplete","causal_section_ids":["4.1"],"check_key":"discovery-dedupe-direct-suite-owner","description":"Section 4.1 moves dedup_memory_results and dedup_skill_results from eager claim_set_variable_values to receipt staging. tests/hooks/test_hook_manager_extra.py:155-275 directly calls both wrappers and asserts those eager claims, while targeted tests/hooks/test_hook_extracted_helpers.py:209-275 covers different evaluator-result merging behavior. The direct old-contract suite is absent from Targets.","finding_id":"agy-r17-dedupe-direct-suite","fix":"Add tests/hooks/test_hook_manager_extra.py::* to §4.1 Targets and correct 4.1.17's direct-suite attribution. Rewrite TestDedupMemoryResults and TestDedupSkillResults to prove prepare-without-claim, acknowledgment commit, transport-loss release/retry, duplicate-ack no-op, and terminalization while retaining ID-less and fail-open filtering cases.","introduced_in_round":12,"location":"§4.1 Targets and 4.1.17; src/gobby/hooks/rule_evaluator.py:277-338; tests/hooks/test_hook_manager_extra.py:155-275; tests/hooks/test_hook_extracted_helpers.py:209-275","prevention":"For every moved writer, search direct calls and mock assertions on the exact storage mutation, then include each owning suite in Targets.","principle":"Moving a persistence boundary requires migrating every incumbent direct test that asserts the old boundary.","root_cause":"The receipt-staging repair names test_hook_extracted_helpers.py as direct coverage, but the memory/skill dedupe contract is exercised in untargeted test_hook_manager_extra.py.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r11-rule-context-receipt-bypass","causal_section_ids":["4.1"],"check_key":"agent-preamble-direct-suite-owner","description":"Section 4.1 targets _agent.py and requires the first-turn preamble guard to commit on acknowledgment, but tests/hooks/test_agent_events_coverage.py:192-469 is untargeted. That suite directly asserts eager _agent_context_injected writes for the first prompt, stale-state repair, persona switch, and explicit rehydration, so implementation either violates 4.1.17 or breaks an unowned incumbent contract.","finding_id":"agy-r17-agent-preamble-direct-suite","fix":"Add tests/hooks/test_agent_events_coverage.py::* to §4.1 Targets. Re-anchor first-turn, persona-switch, and rehydration cases to staged guard creation, acknowledgment-only commit, and transport-loss re-presentation; explicitly classify and test whether the prior-activity stale-repair branch remains eager or becomes receipt-staged.","introduced_in_round":11,"location":"§4.1 Targets and 4.1.17; src/gobby/hooks/event_handlers/_agent.py:247-319; tests/hooks/test_agent_events_coverage.py:192-469","prevention":"For each staged guard, enumerate first delivery, reinjection/reset, stale repair, retry, and terminal branches and target the suite asserting each current write.","principle":"A one-shot guard moved behind acknowledgment must migrate the direct tests for every branch that currently commits it eagerly.","root_cause":"Round 11 targeted the preamble production module but omitted its incumbent direct suite while broad acceptance text claimed the behavioral suites would migrate.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r14-legacy-capability-envelope-contract","causal_section_ids":["4.1"],"check_key":"inbox-schema-mirror-target-closure","description":"The repository tracks crates/ghook/schemas/inbox-envelope.v1.schema.json and schemas/inbox-envelope.v1.schema.json as byte-identical 68-line copies with the same SHA-256. Section 4.1 changes the v1 allowed-property set to admit response capability but targets only the crate copy, leaving the root public mirror stale.","finding_id":"agy-r17-inbox-schema-mirror","fix":"Add schemas/inbox-envelope.v1.schema.json to §4.1 Targets, require the optional response-capability property in both copies, and add a focused byte-for-byte parity assertion to 4.1.18 or V2.","introduced_in_round":14,"location":"§4.1 Targets and 4.1.18; crates/ghook/schemas/inbox-envelope.v1.schema.json:1-68; schemas/inbox-envelope.v1.schema.json:1-68","prevention":"For every wire-schema edit, inventory tracked byte-identical copies and add a focused parity check before closing Targets.","principle":"Every tracked public mirror of a changed wire schema must be updated and parity-validated with its canonical source.","root_cause":"The Round 14 repair added only the crate-local schema target and missed the tracked root schema mirror.","section_id":"4.1","severity":"blocking"},{"category":"unhandled-edge","causal_finding_id":"agy-r13-receipt-retention-state-contract","causal_section_ids":["4.1"],"check_key":"receipt-ack-current-attempt-lineage","description":"The repaired state machine re-prepares the same receipt row when its payload moves to a new durable envelope and updates current envelope lineage. The delivery-receipt wire contract still carries only receipt_id, original envelope id, and session identity. A delayed acknowledgment from the earlier carrying envelope is therefore indistinguishable from the current attempt and can CAS the newly prepared row to acknowledged before the current envelope is emitted.","finding_id":"agy-r17-receipt-ack-attempt-lineage","fix":"Add current-attempt identity—current envelope id or monotonic delivery generation—to the receipt and acknowledgment schemas. Require acknowledgment CAS on receipt_id, attempt identity, and state=prepared; define stale-attempt acknowledgments as terminal no-ops. Test old ack after release/reprepare, current ack, restart/replay, and concurrent Stop/expiry.","introduced_in_round":13,"location":"§4.1 receipt prose and 4.1.15/4.1.19; crates/ghook/src/transport.rs:137-143; crates/ghook/src/envelope.rs:24-51; src/gobby/hooks/inbox.py:101-134,187-274","prevention":"For every released-to-prepared retry, model delayed messages from every prior attempt and require compare-and-set identity to distinguish current from stale acknowledgments.","principle":"An acknowledgment may commit only the exact delivery attempt whose output generated it.","root_cause":"Round 13 repaired released-row liveness by reusing one receipt_id across envelopes and recording current-envelope lineage, but the acknowledgment wire identity remained receipt_id plus original envelope id.","section_id":"4.1","severity":"blocking"},{"category":"traceability","causal_finding_id":"agy-r10-precompact-trigger","causal_section_ids":["5.1","5.2","5.3"],"check_key":"agy-precompact-supported-branch-owner","description":"Section 5.3 says a supported 1.1.16 stream compaction signal is specified in 5.2 and invoked by AgyManagedChatSession. Section 5.1 normalizes only init/content/result/error and has no compaction record requirement; section 5.2 has no PRE_COMPACT callback or dedupe requirement; section 5.3 targets neither agy_stream.py nor backends/agy.py. If the probe proves a signal, the plan can legally tolerate it as unknown or never forward it while 5.3.5 still appears satisfied.","finding_id":"agy-r17-precompact-supported-branch","fix":"Assign the supported branch explicitly: make §5.1 parse and normalize the recorded compaction signal and §5.2 invoke a separately keyed, deduplicated PRE_COMPACT callback, with both production targets and parse-to-_fire_lifecycle exactly-once tests. Equivalently, move both production owners into §5.3. Retain the absent-signal branch that removes PRE_COMPACT from the AGY claim.","introduced_in_round":10,"location":"§1.1.16; §5.1 Targets and 5.1.1-5.1.5; §5.2 Targets and 5.2.1-5.2.14; §5.3 prose and 5.3.5; src/gobby/adapters/acp_stream.py:19-28; src/gobby/servers/websocket/chat/_session.py:469-479; src/gobby/servers/websocket/chat/_lifecycle.py:305-321","prevention":"For each conditional probe outcome, trace both branches from recorded input through parser, runtime callback, deduplication, consumer, and direct test before approval.","principle":"Every probe-dependent supported branch needs a targeted parser, runtime producer, and end-to-end acceptance path.","root_cause":"The PRE_COMPACT repair added a Gate 0 branch and later prose delegated the supported outcome to §5.2, but §5.1/§5.2 never acquired the required parsing, callback, dedupe, or acceptance text and §5.3 lacks those production targets.","section_id":"5.3","severity":"blocking"}],"reviewer_session":"d7d7b2d5-20ed-4021-9dc7-a360bcf04c10","round":17,"verdict":"needs_review"},"session_id":"90c58f08-5f2b-4785-8602-061ce5df5933"}
```

**Round 18** `kind: verification`

- reviewer_run: 886d6c97-ad01-4591-8bd6-fdbaf7a7c0e1
- reviewer_session: 37841a08-7c86-4f86-8955-23012d84118d
- verdict: needs_review
- findings: 7 (6 blocking, 1 nit; 6 of the 7 in §4.1)
- **unfinalized** — no evidence fence was written, no finding was verified against the
  code index, and no repair was applied. The round is recorded here for provenance only.
- resolution_notes: The campaign was stopped after this round. Finding counts across the
  last five rounds — R14:7, R15:6, R16:5, R17:7, R18:7 — show no convergence, and §4.1
  had accumulated the great majority of open findings. More decisively, the standalone
  Gate 0 probe task (#19563) ran during this round and returned a result no review round
  could resolve: AGY registers Gobby's hooks and never dispatches them, matching open
  upstream issue `google-antigravity/antigravity-cli` #222. Adversarial review cannot
  repair an upstream defect, so the plan was parked instead. This revision adds the
  **Status** and **Upstream Blocker Gate** sections and §1.2's Gate 0 execution record,
  raises the version floor to 1.1.10 throughout, applies the two disproof-driven repairs
  the probe already produced (§5.1's nested stream-json record shape and wider `step_type`
  vocabulary; §1.1.13/§5.2's `--print-timeout` contract, which disproves the committed
  1.0.11 fixture), and renumbers the reserved migrations from 370-372 to 371-373 because
  367-370 are now applied on disk. The plan is **not** submitted for planning approval and
  derives no manifest; the Upstream Blocker Gate states the four resume conditions.

**Re-baseline 2026-08-20 (pre-Round 19)** `kind: verification`

- verdict: not a review round — prose-only re-baseline, no evidence fence
- resolution_notes: Installed AGY is **1.1.16** and a print-mode probe on it dispatched
  `PreInvocation`, `PreToolUse`, `PostInvocation`, and `Stop` through Gobby's registered
  hook (upstream #222 still Open; the local observation governs). The plan is unparked
  and re-baselined against 1.1.16 and HEAD. **Status** records the proof, the outstanding
  items, and the 1.1.10 history; **Upstream Blocker Gate** becomes the **Dispatch Evidence
  Gate** with a per-deliverable "records it embeds" table and five pre-approval
  conditions. The floor moves to 1.1.16 everywhere; historical `Recorded (1.1.10)` notes
  stay. §1.1 is retitled, its 1.1.1–1.1.16 records tagged `[re-confirm on 1.1.16]` /
  `[open]`, and eight records 1.1.17–1.1.24 added (interactive dispatch, `--input-format
  stream-json` semantics, `/usage|/quota|/credits` JSON, `agy models` JSON, `/hooks` JSON,
  transcript layout, `--mode`, response-field acceptance), with a rewritten fixture list,
  terminal-mode probe mechanics (raw tmux), a capture-hook recipe, and scrubbing rules.
  §1.2 becomes a cumulative Run 1 / Run 2 record. §2.1 adds the two missed Claude-fallback
  sites (`TranscriptAnalyzer.__init__`, `HookManagerFactory.create`) and corrects the test
  sweep (2.1.8–2.1.9). §2.2 specifies the AGY pending-path behavior and
  `_detect_source_from_path` (2.2.10–2.2.12). §2.3 routes the fail-open tail through
  `skip_stdout_json` for agy, corrects the diagnose test and guides, and owns the
  `EVENT_TYPE_CLI_SUPPORT` agy rows (2.3.7–2.3.11). §2.5's premise is rewritten
  (`get_cli_version` has zero callers; the catalog is gone) and 2.5.5 re-pointed. New
  **§2.6** (installer timeout propagation, `/hooks` verification, `gobby status` truth).
  §3.1 re-points the workspace-identity migration into the gcore embedded-asset set and
  makes the `sandbox.py` → `sandbox_resolvers.py` extraction mandatory, composing with
  `_refresh_sandbox_config` and the `sensitive_path_enforcement` gate (3.1.17–3.1.19).
  §3.2 lands `AgySandboxResolver` in the extracted module with the `--sandbox=false`
  boolean form (3.2.6–3.2.7). §4.1 keeps the full delivery-receipt protocol: AGY-local
  camelCase aliasing, the four-column `idx_sessions_unique` + machine ownership identity,
  `overwrite` not `updatedInput`, `ContextChannel.INJECT_STEPS`, the 1.1.16 response
  fields mapped or fail-closed per 1.1.24, persistent-mode `PreInvocation` cardinality,
  and the `cli/sync.py --reinstall` owner replacing the deleted `manage.py`
  (4.1.20–4.1.25). §4.2 adds the file-identity rule, positional-only pairing, the three
  parser-capability gates, and usage ownership (4.2.13–4.2.16). §5.1 adds the shared
  snake_case tool-name table, `usage` passthrough, and the turn-boundary contract
  (5.1.7–5.1.10); §5.2 the 1.1.18-decided transport branch rule and Droid mirror
  (5.2.16–5.2.19); §5.3 defers the web-UI un-hide to 6.2. §6.1 gains the
  interactive-dispatch gate, keystroke-driven plan control, and `agy.toml` detection
  rules (6.1.13–6.1.15); §6.2 the web-UI un-hide, `_validate_agy_stdout` fix, and
  route-test flip (6.2.8–6.2.10); §6.3 is rewritten onto the capability collectors
  (`AgyCollector`, bundled seed; 6.3.9–6.3.11); new **§6.4** folds #19364 usage-capacity
  reporting. §7.1 enumerates the seven stale doc claims (7.1.5–7.1.7); V2 gains the
  fail-open legality, interactive evidence, usage, new-inode reinstall, and final
  `plans validate` items. Migration drift found during this revision: 399–401 were
  consumed between the 2026-08-20 draft and this edit and the 399 hop reverted an
  in-place baseline edit, so the reservations are **402/403/404** and the baseline is
  declared sealed; the `schema_contract.rs` pin is current (401), not stale. All 31
  baseline `plans validate` target errors are resolved; no acceptance ID was renumbered
  or removed. Round 18 stays unfinalized. Adversarial review resumes at Round 19 after
  §1.2's Run-2 table is filled by #19563.
