# Reviewer Landing Through land_commit (M3)

Plan artifact: `.gobby/plans/reviewer-landing.md`

**Plan ID:** reviewer-landing

## Overview
`kind: framing`

Task #23527 (Plan reviewer landing through land_commit (M3)) under the Lane 7
planning epic. Source research: #23513 (Investigate retiring
task-close-reviewer: mechanical close gates plus code-reviewer review), report
`.gobby/plans/research/close-review-retirement-2026-10.md` §3 at `a9e1b4c040`.

The lane reviewer that LANDed a candidate lands it on `0.5.0` itself, through
one daemon tool, `gobby-tasks-ops:land_commit(task_id, commit_sha)`. The tool
is the only writer of the main checkout's branch for anything except Markdown
plans, roles and docs and plan coverage files. It needs Orchestrator approval only for a
restart-class landing, an active freeze, or overlap with another lane's
in-flight candidate. A git `reference-transaction` hook refuses every other
update of that branch.

What changes for seats:
- A reviewer's LAND is recorded as today (an `independent_review_approval`
  receipt), then the reviewer calls `land_commit`. Docs-, test- and
  `web/`-only candidates land in seconds with no approval.
- The Merge Manager's `0.5.0` landing duty, its "chore: land reviewed"
  landing tasks and the per-landing landed-tree pytest retire. A moved tip gets
  a focused retest of the candidate's tests on the landed tree instead.
- task-close-reviewer and every close gate stay unchanged. Developers still
  close their own tasks through the Lane Manager release line.

Measured cost today (Lane 7 timing sample, 2026-10-05): a landing task waits a
median 101 minutes from creation to landing, for 1 to 5 minutes of actual
merge work.

## Decision Record
`kind: framing`

Rulings of record, verbatim:
- Josh: "I don't like developers doing merges"
- Josh: "Lane Reviewer could do it after getting orchestrator approval."
- Josh (2026-10-05): "landing: lighter variant, approval only for
  restart/freeze/overlap, freeze flag"
- Josh (2026-10-05): "Let's hold off on removing task close reviewer, but keep
  the merge process changes. Show me that plan."
- Josh (2026-10-05, 11:56 CT): "approve M3 alone with decisions 1, 3, 4, 5"

1. **Moved tip (Josh decision 1).** When `0.5.0` moved past the candidate's
   base and none of the moved paths is a candidate path, `land_commit` builds
   the two-parent merge commit itself and lands it, with no rebase and no
   re-review. The reviewer then runs the candidate's focused tests on the
   landed tree in the main checkout, which is the Merge Manager's current
   practice. When a moved path is also a candidate path, the tool refuses with
   `base_update_required`; the developer merges `0.5.0` into the lane branch,
   reruns its focused tests, and the reviewer reviews and LANDs the new SHA.
   - Rejected: retest before landing. It needs a temporary checkout of the
     merge commit plus its own virtualenv per landing; planners and reviewers
     create no worktrees.
   - Rejected: refuse every moved tip. About 32 code commits land per day, so
     every concurrent restart-class candidate would need a rebase, a new LAND
     and a new approval.
   - Rejected: patch-id carry-over of a LAND across a rebase (report §3).
     Rebases happen only for intersecting paths, which need re-review anyway.
2. **Approval authority (Josh decision 3).** A `landing_approval` close
   receipt comes only from the task's creator or delegator, the rule
   `activation` receipts already use. `transfer_task_authority` stays the
   fallback when both have ended. Its `reason` fact holds one or more of
   `restart`, `freeze`, `overlap`, comma-joined. An optional `batch` fact
   names an Orchestrator restart batch. Approvals are append-only: a
   `landing_approval` is idempotent per author, SHA and reason set, so an
   approver adds a reason with a new receipt and never edits one. The tool
   takes the union of reasons across that SHA's approvals.
   - Rejected: a named Orchestrator seat. The daemon cannot identify a seat.
3. **Freeze home (Josh decision 4).** A JSON file at
   `<git-common-dir>/gobby/landing-freeze.json` holding `on`, `reason`,
   `set_by_session_id` and `set_at`, written by
   `gobby-tasks-ops:set_landing_freeze(on, reason)`. The daemon records the
   setter and enforces no seat; role files restrict the tool to the
   Orchestrator. An unreadable file reads as frozen.
   - Rejected: a `CONFIG_REGISTRY` key in `src/gobby/config/registry.py`. Any
     `src/gobby/config/` change carries the crate contract
     `runtime_config_contract.json`, which makes a runtime flag a cutover.
   - Rejected: a hub table. It needs a schema migration for one row.
4. **Path classes (Josh decision 5; reload/sync amendments: 2026-10-08 #23750).** The strongest class among the
   candidate's changed paths wins, in the order cutover, restart, ui_build,
   reload, sync, none. A candidate combining reload and ui_build uses restart:
   startup sync reloads agents, and the UI still needs a separate `gobby ui
   build`, as for any restart candidate that also changes `web/**`:
   - cutover: `crates/**`, `Cargo.toml`, `Cargo.lock`,
     `src/gobby/storage/schema_expected_identity.json`;
   - restart: `src/gobby/**` (the CLI included, because the daemon imports
     `gobby.cli`), `pyproject.toml`, `uv.lock`;
   - ui_build: `web/**` (the daemon serves `web/dist` from disk; `gobby ui
     build` activates it with no restart and no approval);
   - reload: top-level `src/gobby/install/shared/workflows/agents/*.yaml`
     definitions (reload_cache syncs them for new spawns; existing seats keep
     their applied definitions);
   - sync: top-level `.gobby/workflows/pipelines/*.yaml` and `*.yml` authoring
     files (`gobby-workflows:reload_cache` from the main checkout syncs them
     into the project's runtime DB; no restart approval is required);
   - none: everything else.
   When a candidate also changes these files, run the project sync as well as any
   UI build, restart or cutover. Startup does not sync imported project workflows.
   Reload already performs that sync; verify the relevant DB rows before using them.
   cutover and restart both require a `restart` approval.
5. **Landing record.** `land_commit` writes a daemon-only `landing` close
   receipt, authored by the landing reviewer and naming the candidate SHA. Its
   facts hold the branch, the landed tip, the mode, the activation class, the
   merge commit and whether a retest is required. The REST comment route
   already refuses forged close receipts, receipts are idempotent per author,
   kind and SHA, and the close reviewer already reads them.
   - Rejected: linking the merge commit to the task. Close gates 7, 8 and 13
     read linked commits, and a merge commit's combined diff is usually empty.
     The merge subject therefore carries no `[<project>-#N]` or `<project>-#N`
     tag, which commit discovery would link back to the task.
   - Rejected: a plain task comment. Forgeable through REST, and it needs its
     own idempotency.
6. **Overlap.** The in-flight set is every `independent_review_approval`
   receipt on another open task in the project whose SHA is not an ancestor
   of the tip. Candidates that are an ancestor or a descendant of this SHA are
   excluded, because a stacked lane branch lands them together. An overlap is
   a non-empty intersection of changed paths, each side measured from its
   merge base with the tip.
7. **Guard and direct-commit allowance (Josh decision 7, 12:24 CT: "guard,
   direct commits for Markdown only").** A `reference-transaction` hook refuses every update of the branch
   checked out in the main worktree unless `GOBBY_LAND_COMMIT=1` is set or
   every changed path is a `*.md` file at the repository root or under
   `.gobby/plans/`, `.gobby/roles/` or `docs/`, or any file under
   `.gobby/plans/coverage/`. Plan writers and the
   Orchestrator keep committing those directly (69 of 123 commits on
   2026-10-05). The coverage allowance is Josh's choice (a) on reviewer
   finding B1, relayed by the Orchestrator at 13:19 CT: plan registration,
   write-backs and archives commit coverage manifests and
   `.gobby/plans/coverage/.regenerate.log` from the main checkout
   (27883c9ac9, 2fedc56469, 1fb4cb12dd), where no lane ref exists for
   `land_commit`. `docs/reference-audit/*.json` stays out because tests read
   it (#23484). A bundled rule refuses agent shell commands that mention
   `GOBBY_LAND_COMMIT`. Consequence: research attachments (`*.svg`, `*.txt`)
   under `.gobby/plans/` land through `land_commit`.

Out of scope: the needs-review handoff (Josh dropped decisions 2 and 6),
task-close-reviewer retirement, and `main`/push routing. This plan has no
dependency on #23341 (Remove gobby build): `land_commit` uses receipts and no
stage rows.

## As-Is Facts
`kind: framing`

- The Merge Manager lands with two-parent merge commits in the main checkout,
  e.g. `94cb8fc8a1` (parents `b0f2ce2339`, `faa10b98cf`), after a broadcast
  reservation. `0.5.0` took 123 commits on 2026-10-05 before noon: 69 docs,
  22 landings, 32 fix or feat.
- Git probe (git 2.54, observed this session): `git merge --no-ff` refuses
  while any index entry is staged. A merge commit built off-tree with
  `git merge-tree --write-tree <tip> <sha>` and
  `git commit-tree <tree> -p <tip> -p <sha>`, then applied with
  `git merge --ff-only <merge>`, lands while unrelated files are staged or
  dirty and keeps them. `--ff-only` refuses when a landed path is locally
  dirty.
- Receipts: `src/gobby/tasks/close_receipts.py::record_close_receipt` locks the
  task row `FOR UPDATE`, refuses the claimant and task-close reviewer
  sessions, takes `activation` only from the creator or delegator, needs a
  full 40-character SHA, bounds facts (16 keys, key 64, value 300 characters)
  and is idempotent per author, kind and SHA. The REST comment route refuses
  author type `close_receipt`
  (`src/gobby/servers/routes/tasks_comment_routes.py`).
- Locks: `HubDatabase.advisory_lock(LockTarget)` holds PostgreSQL session
  locks without an idle transaction
  (`src/gobby/storage/hub/postgres_pool.py::advisory_lock`); each target maps
  to a key in `advisory_lock_keys`. Nested locks need strictly increasing
  `PRIORITY`; the lowest existing value is 50.
- Git from the daemon goes through `daemon_git.run(args, cwd=, timeout=, env=)`
  in `src/gobby/utils/daemon_git.py`; a passed `env` replaces the whole
  environment. Without one, `run` uses `git_subprocess_env() or
  dict(os.environ)`; `git_subprocess_env` (`src/gobby/utils/git.py`) adds
  fallback `PATH` entries only when `git` is not resolvable.
- Hooks: `src/gobby/cli/installers/git_hooks.py::HOOK_TEMPLATES` drives
  install, uninstall and `get_stale_git_hooks`; `core.hooksPath` is the shared
  `.git/hooks`, so one install covers every worktree. `get_stale_git_hooks`
  skips a hook file that is missing or has no Gobby section, so a newly added
  template never reports stale on an existing install. `install_git_hooks`
  puts the Gobby section before preserved foreign hook content. The
  `pre-push` template runs `exit 0` for delete-only pushes and
  `pre-merge-commit` runs `exit 0` under `GOBBY_MERGE=1`, which skips that
  foreign content. `pre-push` (`PUSH_REFS=$(cat)`) and `post-rewrite`
  (`while read`) consume stdin, which leaves the foreign content none.
- Reflog: the main checkout logs every update of its branch in
  `<git-common-dir>/logs/refs/heads/<branch>` (`core.logAllRefUpdates`
  true; 1,740 entries for `0.5.0` on 2026-10-05). Probed in a throwaway
  repository: `merge --ff-only` to a SHA, to a prebuilt merge commit, and a
  direct commit each add an entry, and linked worktrees read the same log.
  The log stores each entry as `<old> <new> <ident> <ts> <tz>\t<message>`,
  oldest first, but `git log -g --format='%H %gs'` exposes only the new OID
  and the action. A fast-forward leaves no other trace of the pre-landing
  tip. `GIT_REFLOG_ACTION` set on `merge --ff-only` becomes the entry's
  action text (probed for both a SHA and a prebuilt merge: `gobby-land
  candidate=<sha> mode=merge class=restart: Fast-forward`).
  `merge --ff-only` succeeds from any ancestor of its target, so a tip
  observed before the write can differ from the write's real old value:
  probed O→A→B, a fast-forward to A after observing O, then `gobby-land`
  `--ff-only` to B stores old A. This checkout uses ref format `files`
  (`git rev-parse --show-ref-format`; git 2.54.0).
- Commit discovery: `extract_task_ids_from_message` in
  `src/gobby/tasks/commits.py` links a commit whose message carries
  `[<project>-#N]` or `<project>-#N` to task N.
- Memory 37618e95: the Merge Manager's git index commands entered its edit
  ledger and deadlocked other sessions' commits and closes, which is why
  landings run between close batches (memory 283e9a81). `land_commit` runs
  git inside the daemon and names no paths in an agent tool call, so it never
  enters an edit ledger.

## Constraints
`kind: framing`

- Main checkout only; `land_commit` operates on the parent of the project's
  git common dir, whichever worktree the caller sits in.
- No schema migration, no config-registry key, no new hub table.
- `src/gobby/storage/hub/postgres_pool.py` (715 lines),
  `src/gobby/cli/installers/git_hooks.py` (616) and
  `src/gobby/mcp_proxy/tools/task_commits.py` (580) stay under the 850-line
  decomposition trigger; new logic goes in new modules.
- Daemon merge tools (`gobby-merge`, worktree merge fallbacks) that would move
  the protected branch are refused by the guard too. Merges into lane
  branches are unaffected.

## P1: Landing
`kind: framing`

**Goal:** a reviewer lands its LANDed candidate in one call, and nothing else
moves `0.5.0` except Markdown plans, roles and docs and plan coverage files.

### 1.1 Landing receipt kinds [category: code]
`kind: deliverable`

Targets:
- `src/gobby/tasks/close_receipts.py::CLOSE_RECEIPT_KINDS`
- `src/gobby/tasks/close_receipts.py::record_close_receipt`
- `src/gobby/mcp_proxy/tools/task_commits.py::create_commit_registry`
- `src/gobby/mcp_proxy/tools/task_commits.py::record_close_receipt`
- `src/gobby/tasks/agentic_close_review.py::build_agentic_review_prompt`
- `src/gobby/install/shared/workflows/agents/task-close-reviewer.yaml::*` — scope-reason: the receipt paragraph of the reviewer instructions names the two new kinds
- `tests/tasks/test_close_receipts.py::*` — scope-reason: add landing_approval, landing and prompt tests beside the existing receipt tests

Consumers unchanged:
- `tests/mcp_proxy/tools/tasks/test_handed_off_claims.py` — no-edit-reason: records independent review approvals; adding activation classes does not change receipt authority.
- `tests/mcp_proxy/tools/tasks/test_commit_path_ownership.py` — no-edit-reason: exercises commit path ownership through the existing registry factory.
- `tests/mcp_proxy/tools/tasks/test_transfer_task_authority.py` — no-edit-reason: records activation receipts, whose rules do not change.
- `src/gobby/servers/routes/tasks_comment_routes.py` — no-edit-reason: already refuses every close_receipt author type, new kinds included.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_orchestration.py` — no-edit-reason: passes receipts through to the prompt builder unchanged.
- `tests/tasks/test_agentic_close_review.py` — no-edit-reason: asserts prompt sections that keep their text.
- `tests/tasks/test_close_evidence_bounds.py` — no-edit-reason: bounds evidence size, independent of receipt kinds.
- `src/gobby/mcp_proxy/tools/__init__.py` — no-edit-reason: re-exports the registry factory.
- `src/gobby/mcp_proxy/tools/tasks/_factory.py` — no-edit-reason: merges the commit registry unchanged.
- `tests/mcp_proxy/tools/tasks/test_authorization.py` — no-edit-reason: authorization of other commit tools.
- `tests/mcp_proxy/tools/test_task_commits.py` — no-edit-reason: link and diff tools only.
- `tests/tasks/test_diff_paging.py` — no-edit-reason: diff paging only.

**Research context:** receipts live in
`src/gobby/tasks/close_receipts.py`: constants `INDEPENDENT_REVIEW_APPROVAL`,
`ACTIVATION`, `CLOSE_RECEIPT_KINDS`; `record_close_receipt(db, *, task,
author_session_id, kind, commit_sha, facts)` authorizes against the locked task
row (claimant refused; `activation` only from `created_in_session_id` or
`delegated_by_session_id`; task-close reviewer sessions refused through
`agent_runs`), validates `_bounded_facts`, and dedupes per author, kind and
SHA. The MCP tool is the nested `record_close_receipt` inside
`src/gobby/mcp_proxy/tools/task_commits.py::create_commit_registry`; its
`kind` enum is `sorted(close_receipts.CLOSE_RECEIPT_KINDS)` and its description
names the two current kinds. The close reviewer learns receipt semantics from
`build_agentic_review_prompt` in `src/gobby/tasks/agentic_close_review.py`
(the `close_receipts=` paragraph) and from the matching paragraph in
`task-close-reviewer.yaml`.

Implementation:
- New constants `LANDING_APPROVAL = "landing_approval"` and
  `LANDING = "landing"`, both in `CLOSE_RECEIPT_KINDS` and `__all__`, plus
  `CALLER_RECEIPT_KINDS = CLOSE_RECEIPT_KINDS - {LANDING}` and
  `LANDING_APPROVAL_REASONS = frozenset({"restart", "freeze", "overlap"})`.
- `record_close_receipt` applies the `activation` creator-or-delegator check to
  `landing_approval` as well. A `landing_approval` needs a `reason` fact whose
  comma-separated parts are a non-empty subset of `LANDING_APPROVAL_REASONS`;
  `batch` is optional. `landing` adds no authority rule beyond the existing
  claimant and task-close-reviewer refusals; only `land_commit` (1.3) writes
  it.
- Replay for `landing_approval` matches on author, SHA and the sorted reason
  set: an identical set returns the existing receipt, and a different set
  appends a new receipt. Existing receipts are never edited. Every other kind
  keeps the author, kind and SHA match.
- The MCP tool refuses any kind outside `CALLER_RECEIPT_KINDS` before
  verifying the SHA, and its schema enum lists only those kinds. The
  description names `landing_approval` (creator or delegator; reasons) and
  says `landing` is written only by `land_commit`.
- Both reviewer texts add: a `landing_approval` is the creator's or
  delegator's approval to land that SHA despite the named reasons; a `landing`
  receipt is the daemon's record that `land_commit` landed that SHA, with its
  branch, landed tip, mode and activation class. Neither is a verdict.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_close_receipts.py tests/tasks/test_agentic_close_review.py tests/mcp_proxy/tools/tasks/test_transfer_task_authority.py -q`.

**Acceptance:**

- 1.1.1 - A `landing_approval` is recorded from the task's creator or
  delegator and refused from any other session, the claimant included. test:
  `tests/tasks/test_close_receipts.py::test_landing_approval_comes_only_from_task_creator_or_delegator`.
- 1.1.2 - A `landing_approval` without a `reason`, or with a part outside
  `restart`, `freeze`, `overlap`, is refused; `restart,overlap` with a `batch`
  fact is accepted. test:
  `tests/tasks/test_close_receipts.py::test_landing_approval_requires_known_reasons`.
- 1.1.3 - The MCP tool refuses kind `landing`, and its schema enum omits it.
  test: `tests/tasks/test_close_receipts.py::test_tool_refuses_daemon_only_landing_kind`.
- 1.1.4 - A `landing` receipt recorded through the module function is stored
  once per author and SHA and returned on replay. test:
  `tests/tasks/test_close_receipts.py::test_landing_receipt_is_recorded_once_per_author_and_commit`.
- 1.1.5 - The close-review prompt explains `landing_approval` and `landing`
  receipts when the task carries them. test:
  `tests/tasks/test_close_receipts.py::test_reviewer_prompt_explains_landing_receipts`.
- 1.1.6 - The creator approves `restart`, then `freeze,overlap`, for one SHA:
  two receipts exist, the first unchanged, and repeating either call adds
  none. test:
  `tests/tasks/test_close_receipts.py::test_landing_approval_new_reasons_append_receipt`.

### 1.2 Landing policy and freeze flag [category: code]
`kind: deliverable`

Targets:
- `src/gobby/tasks/landing_policy.py`
- `src/gobby/mcp_proxy/tools/tasks/_landing.py`
- `src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::create_task_ops_registry`
- `src/gobby/workflows/enforcement/blocking.py::TASK_MUTATION_TOOLS_BY_SERVER`
- `tests/tasks/test_landing_policy.py`
- `tests/mcp_proxy/tools/tasks/test_landing_tools.py`
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: add a landing-operations tuple to the mutation list and the registry parity assertion

Consumers unchanged:
- `.gobby/plans/research/security-boundaries-22103/probe_control_plane.py` — no-edit-reason: reports mutation server names dynamically, with no tool-set snapshot.
- `src/gobby/mcp_proxy/registries.py` — no-edit-reason: builds the ops registry through the same factory call.
- `src/gobby/mcp_proxy/tools/__init__.py` — no-edit-reason: re-exports the factory.
- `tests/build/test_build_surface_cleanup.py` — no-edit-reason: asserts absent build tools only.
- `tests/mcp_proxy/tools/tasks/test_checkout_unresolved_envelopes.py` — no-edit-reason: exercises other ops tools.
- `tests/mcp_proxy/tools/tasks/test_mcp_proxy_tools_tasks_expansion.py` — no-edit-reason: expansion tools only.
- `tests/mcp_proxy/tools/tasks/test_review_tools_server_placement.py` — no-edit-reason: checks review tool placement by name.
- `tests/mcp_proxy/tools/tasks/test_stage_tools_registered.py` — no-edit-reason: checks stage tools by name.
- `tests/mcp_proxy/tools/test_build_observability.py` — no-edit-reason: build tools only.
- `tests/mcp_proxy/tools/test_mcp_proxy_tools_build.py` — no-edit-reason: build tools only.
- `tests/mcp_proxy/tools/test_read_only_classification.py` — no-edit-reason: the new tools mutate, so the exact read-only set is unchanged.
- `tests/mcp_proxy/tools/test_tasks_ops_artifacts.py` — no-edit-reason: artifact tools only.
- `tests/mcp_proxy/tools/test_tasks_registry_edge_cases.py` — no-edit-reason: no exact tool-set snapshot.
- `tests/storage/tasks/test_artifacts_plan_file_hash.py` — no-edit-reason: artifact tools only.
- `tests/workflows/expansion_qa_helpers.py` — no-edit-reason: expansion helpers only.

**Research context:** gobby-tasks-ops is assembled in
`src/gobby/mcp_proxy/tools/tasks/_ops_factory.py::create_task_ops_registry`
from `registry.merge_from(create_*_registry(ctx))` calls over a shared
`RegistryContext`. `TASK_MUTATION_TOOLS_BY_SERVER` in
`src/gobby/workflows/enforcement/blocking.py` names the mutating tools that
`require-tasks-skill-for-mutations` gates; its gobby-tasks-ops entry holds
`approve_review`, `reject_review`, `submit_for_review`.
`tests/workflows/test_task_enforcement_rules.py` mirrors it
(`TASK_REVIEW_OPERATIONS`, `NON_INTERACTIVE_TASK_OPS`) and
`TestRequireTasksSkillForMutations::test_real_registry_inventory_matches_independent_classification`
asserts the registered ops set equals those tuples. The git common dir is
`git rev-parse --path-format=absolute --git-common-dir`; its parent is the main
checkout. Path classes and the direct-commit allowance follow Decision Record
items 4 and 7.

Implementation, new module `src/gobby/tasks/landing_policy.py`:
- `ACTIVATION_CLASSES = ("none", "sync", "reload", "ui_build", "restart", "cutover")`, weakest
  first, and `classify_paths(paths) -> str` returning the strongest class per
  Decision Record item 4 (`none` for an empty set).
- `DIRECT_COMMIT_MARKDOWN_DIRS = (".gobby/plans/", ".gobby/roles/", "docs/")`,
  `DIRECT_COMMIT_ANY_FILE_DIRS = (".gobby/plans/coverage/",)` and
  `is_direct_commit_path(path) -> bool`: true only for a path that starts
  with a `DIRECT_COMMIT_ANY_FILE_DIRS` prefix, or ends in `.md` and has no
  `/` or starts with a `DIRECT_COMMIT_MARKDOWN_DIRS` prefix. 1.5 generates
  the hook's shell patterns from these names.
- `LandingFreeze` (frozen dataclass: `on`, `reason`, `set_by_session_id`,
  `set_at`), `freeze_path(git_common_dir) -> Path`
  (`<common>/gobby/landing-freeze.json`), `read_freeze(git_common_dir)`
  (missing file: off; unreadable or malformed: on, with reason "unreadable
  landing freeze file") and `write_freeze(git_common_dir, *, on, reason,
  session_id)` (writes a temporary sibling, then `os.replace`).

New module `src/gobby/mcp_proxy/tools/tasks/_landing.py` with
`create_landing_registry(ctx)`, merged in `create_task_ops_registry`:
- `set_landing_freeze(on: bool, reason: str)`: needs a calling session and a
  non-empty reason when `on`; resolves the project's repository path, then its
  common dir; writes the file; returns the stored state. A clear writes
  `on: false` with the clearing session and reason.
- `set_landing_freeze` joins the gobby-tasks-ops mutation set in
  `TASK_MUTATION_TOOLS_BY_SERVER`; the test file gains
  `LANDING_OPERATIONS = ("set_landing_freeze",)` in the mutation list and the
  parity union.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_landing_policy.py tests/mcp_proxy/tools/tasks/test_landing_tools.py tests/workflows/test_task_enforcement_rules.py tests/mcp_proxy/tools/test_read_only_classification.py -q`.

**Acceptance:**

- 1.2.1 - `classify_paths` returns the strongest class: a `crates/` path or
  `src/gobby/storage/schema_expected_identity.json` is cutover, `src/gobby/cli/`
  is restart, `web/` alone is ui_build, top-level project pipeline YAML is sync,
  bundled agent YAML is reload, and `docs/` plus `tests/` is none.
  test: `tests/tasks/test_landing_policy.py::test_classify_paths_strongest_class_wins`.
- 1.2.2 - `is_direct_commit_path` accepts `*.md` at the root or under
  `.gobby/plans/`, `.gobby/roles/`, `docs/`, plus
  `.gobby/plans/coverage/<project>/23273/x.coverage.yaml` and
  `.gobby/plans/coverage/.regenerate.log`, and rejects
  `docs/reference-audit/admin.json`, `.gobby/plans/x.svg`,
  `.gobby/plans/coverage-notes/x.yaml` and `src/gobby/AGENTS.md`. test:
  `tests/tasks/test_landing_policy.py::test_direct_commit_allows_listed_markdown_and_coverage`.
- 1.2.3 - A missing freeze file reads as off and a malformed one as on. test:
  `tests/tasks/test_landing_policy.py::test_unreadable_freeze_file_reads_as_frozen`.
- 1.2.4 - `set_landing_freeze` records the calling session, refuses `on`
  without a reason, and a clear keeps the clearing session. test:
  `tests/mcp_proxy/tools/tasks/test_landing_tools.py::test_set_landing_freeze_records_setter_and_clearer`.
- 1.2.5 - The registered gobby-tasks-ops tools equal the classified tuples,
  with `set_landing_freeze` gated as a mutation. test:
  `tests/workflows/test_task_enforcement_rules.py::TestRequireTasksSkillForMutations::test_real_registry_inventory_matches_independent_classification`.

### 1.3 land_commit fast-forward landing with approvals [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets:
- `src/gobby/tasks/land_commit.py`
- `src/gobby/mcp_proxy/tools/tasks/_landing.py`
- `src/gobby/storage/hub/protocol.py::*` — scope-reason: add the MainCheckoutLanding lock target and export it
- `src/gobby/storage/hub/postgres_pool.py::advisory_lock_keys`
- `src/gobby/workflows/enforcement/blocking.py::TASK_MUTATION_TOOLS_BY_SERVER`
- `tests/tasks/test_land_commit.py`
- `tests/mcp_proxy/tools/tasks/test_landing_tools.py`
- `tests/storage/hub/test_postgres_placeholder_remap.py::*` — scope-reason: add the MainCheckoutLanding key assertion beside the existing key tests
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: add land_commit to the landing-operations tuple

Consumers unchanged:
- `.gobby/plans/research/security-boundaries-22103/probe_control_plane.py` — no-edit-reason: reports mutation server names dynamically, with no tool-set snapshot.
- `src/gobby/storage/hub/postgres.py` — no-edit-reason: imports advisory_lock_keys and passes any LockTarget through.
- `tests/storage/test_manager_surface_parity.py` — no-edit-reason: asserts other targets' keys.

**Research context:** reused pieces, all read-only here:
- `close_receipts.list_close_receipts(db, task_id)` and
  `close_receipts.record_close_receipt` (1.1 kinds) in
  `src/gobby/tasks/close_receipts.py`;
- `landing_policy.classify_paths`, `read_freeze` (1.2);
- `daemon_git.run` and `GitOk` in `src/gobby/utils/daemon_git.py`; pass
  `env={**(git_subprocess_env() or os.environ), "GOBBY_LAND_COMMIT": "1",
  "GIT_REFLOG_ACTION": "gobby-land ..."}` to the ref-moving command, which
  keeps `run`'s git `PATH` fallback;
- `InterSessionMessageManager(db).create_message(from_session, to_session,
  content)` in `src/gobby/storage/inter_session_messages.py`, the pattern
  `src/gobby/mcp_proxy/tools/tasks/_stage_review.py` uses;
- `HubDatabase.advisory_lock` (session-level; see As-Is Facts).
Commit prefixes use the project name and task number, as
`src/gobby/agents/worktree_checkpoint.py` does (`[gobby-#N] chore: ...`).

New lock target in `src/gobby/storage/hub/protocol.py`:
`MainCheckoutLanding(project_id: str)`, `PRIORITY = 25` (below every existing
target, so any lock taken inside nests correctly), exported in `__all__`, key
`main_checkout_landing:<project_id>` in `advisory_lock_keys`.

`land_commit(task_id, commit_sha)` in gobby-tasks-ops (registered in
`_landing.py`, gated as a mutation) calls
`land_commit.land_candidate(db, *, task, caller_session_id, commit_sha)`:
1. Needs a calling session, a full 40-character `commit_sha` that resolves to
   a commit, and a repository path for the task's project. The main checkout
   is the parent of the git common dir, from whichever worktree the caller
   sits in.
2. Holds `MainCheckoutLanding(project_id)` for the rest of the call, then
   reads the target branch as the main checkout's `symbolic-ref HEAD`
   (detached: refuse `main_checkout_detached`). No branch name is hardcoded.
   `set_landing_freeze` (1.2) holds the same lock around its write, so a
   freeze that has returned applies to every later landing.
3. Refuses `review_receipt_missing` unless the caller authored an
   `independent_review_approval` receipt on the task naming `commit_sha`, and
   refuses `caller_is_claimant` when the caller now claims the task. It also
   refuses `candidate_not_linked` unless a commit linked to the task resolves
   (`git rev-parse --verify <linked>^{commit}`) to exactly `commit_sha`, both
   compared as full OIDs. `close_receipts.py`'s prefix-based
   `matches_linked_commit` is a display fact and is not reused. Review,
   landing and close then name the same commit; the developer runs
   `link_commit` before requesting LAND.
4. When `commit_sha` is already an ancestor of the branch tip, there is no git
   write. If the caller already holds a `landing` receipt for the SHA, the
   call returns that receipt's facts unchanged. Otherwise it recovers the
   landing from git's stored branch reflog. Every ref write by `land_commit`
   sets `GIT_REFLOG_ACTION` to `gobby-land candidate=<full sha>
   mode=<ff|merge> class=<class>` (step 9, 1.4), because commit topology
   alone cannot tell a generated merge from a reviewed two-parent candidate
   that was fast-forwarded. The action names the operation; git's stored
   `old` names the write's real old tip, which can differ from any tip
   observed before the write (As-Is Facts: Reflog). Recovery:
   - Requires `git rev-parse --show-ref-format` to print `files`, then
     reads `<git-common-dir>/logs/refs/heads/<branch>` (shared by linked
     worktrees) oldest to newest. Each line must parse as
     `<old> <new> <ident> <ts> <tz>\t<message>` with `old` and `new` full
     OIDs of the repository's hash length. `old` always comes from its own
     line, never from a neighboring entry or `git log -g` output.
   - The landing event is the first entry whose message begins
     `gobby-land `, whose `new` is contained in the current tip (so an event
     the branch was rewound past never counts), whose `new` contains `sha`,
     and whose stored `old` is nonzero, resolves and does not contain `sha`.
     Mode is the action's mode, `landed_tip` is `new`, `merge_commit` is
     `new` for a merge, and `retest_required` is true for a merge.
     `activation_class` is the action's class when its candidate is `sha`;
     for a candidate landed inside a stacked descendant's landing, it is
     computed from the candidate paths measured from
     `merge-base(<stored old>, sha)` (step 5). `provenance` is `reflog`.
   - Unknown provenance: another ref format, a missing or unreadable log
     file, a line outside that grammar, no qualifying entry (a landing by
     any other writer leaves every later `gobby-land` entry's stored `old`
     already containing `sha`), a zero or missing stored `old`, a malformed
     action, or a missing object. The receipt then records `provenance:
     unknown`, mode `already_landed`, `activation_class: unknown` and
     `retest_required: true`. No class, mode or old tip is inferred.
     Rollout step 4 gives the repair path.
   It records that receipt and notifies. Closed tasks land like open ones.
5. Computes the merge base with the tip and the candidate paths
   (`git diff -z --name-only --no-renames <base> <sha>`), then the activation
   class. Every path set `land_commit` computes, overlap and moved paths
   included, uses `-z` output split on NUL.
6. Required approvals: `freeze` when `read_freeze` is on; `restart` when the
   class is restart or cutover; `overlap` when Decision Record item 6 finds
   overlaps (each reported with task ref, SHA and shared paths). Granted
   reasons are the union over `landing_approval` receipts on the task naming
   `commit_sha`.
7. If the tip is not an ancestor of `commit_sha`, this deliverable refuses
   `tip_moved` (1.4 replaces this step).
8. Any missing approval or refusal returns `landed: false` with every blocker
   at once (`missing_approvals`, `overlaps`, `activation_class`, the SHA an
   approver must name) and no git write.
9. Otherwise re-reads `symbolic-ref HEAD` (changed since step 2: refuse
   `main_checkout_branch_changed`), runs `git merge --ff-only <sha>` in the
   main checkout with the override variable and the `gobby-land`
   `GIT_REFLOG_ACTION` (step 4), then reads the branch ref once
   as `observed_tip` and confirms the target is contained in it
   (`git merge-base --is-ancestor <target> <observed_tip>`). A permitted
   Markdown commit may already have advanced the branch past the target.
10. Records the `landing` receipt (author: caller; facts: `branch`,
    `landed_tip` (the attempted target), `observed_tip`, `mode`,
    `activation_class`, `retest_required`, `provenance` (`recorded`,
    `reflog` or `unknown`), and `merge_commit` when present) and messages
    the task's claimant, creator and
    delegator, deduplicated and excluding the caller: `Landed #N "<title>"
    <short sha> on <branch> at <short tip>; activation <class>`.
11. Returns `landed: true`, `mode`, `branch`, `landed_tip`,
    `activation_class`, `retest_required`.

**Granularity:** 1.3 has nine acceptance items and one lifecycle owner, the
landing call. The lock, branch, link and replay items are preconditions of
that one call, so they stay together. Moved-tip handling and git failure handling are
split into 1.4 because they are independently testable once 1.3 refuses
`tip_moved`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_land_commit.py tests/mcp_proxy/tools/tasks/test_landing_tools.py tests/storage/hub/test_postgres_placeholder_remap.py tests/workflows/test_task_enforcement_rules.py -q`.
Tests build throwaway repositories under `tmp_path` with a lane branch, a
main checkout and real git; no test touches this checkout.

**Acceptance:**

- 1.3.1 - Without the caller's `independent_review_approval` for the exact
  SHA, or when the caller claims the task, the call refuses and the branch
  does not move. test:
  `tests/tasks/test_land_commit.py::test_refuses_without_callers_land_receipt`.
- 1.3.2 - A docs-only candidate whose base is the tip fast-forwards the branch
  to the exact SHA, records a `landing` receipt with mode `ff` and class
  `none`, and messages the claimant and creator. test:
  `tests/tasks/test_land_commit.py::test_fast_forward_lands_exact_candidate`.
- 1.3.3 - A restart-class candidate, an active freeze and an overlapping
  in-flight candidate on another task each add their reason; one response
  lists all three with no git write, and matching `landing_approval`
  receipts let the same call land. test:
  `tests/tasks/test_land_commit.py::test_reports_every_missing_approval_at_once`.
- 1.3.4 - In-flight candidates that are ancestors or descendants of the SHA,
  or already on the tip, never count as overlap. test:
  `tests/tasks/test_land_commit.py::test_stacked_and_landed_candidates_never_overlap`.
- 1.3.5 - A SHA already contained in the tip lands with no git write and one
  `landing` receipt across repeated calls. A SHA fast-forwarded earlier by
  `land_commit` without a receipt recovers mode `ff`, the recorded class and
  `provenance: reflog`. A reviewed two-parent candidate that was
  fast-forwarded recovers mode `ff`. For O→A→B where `land_commit` observes
  O for B, a foreign fast-forward lands A, and `--ff-only` to B succeeds
  from A, replay of B recovers mode `ff` and replay of A records
  `provenance: unknown`. An expired or unreadable reflog, a non-`files`
  ref format, a log line outside the grammar, a landing written without
  the `gobby-land` action, a branch-creation entry, and an event the branch
  was rewound past each record `provenance: unknown`, mode
  `already_landed`, `activation_class: unknown` and
  `retest_required: true`. test:
  `tests/tasks/test_land_commit.py::test_already_landed_candidate_records_landing_once`.
- 1.3.6 - `MainCheckoutLanding` maps to `main_checkout_landing:<project_id>`
  and has the lowest lock priority. test:
  `tests/storage/hub/test_postgres_placeholder_remap.py::test_main_checkout_landing_lock_key`.
- 1.3.7 - Two concurrent `land_commit` calls run one at a time, and the lock is
  free again after a refusal and after an exception. A freeze set while a
  landing holds the lock applies to the next landing. test:
  `tests/tasks/test_land_commit.py::test_concurrent_landings_serialize_and_release_lock`.
- 1.3.8 - A detached main HEAD refuses `main_checkout_detached`, a branch
  switched before the write refuses `main_checkout_branch_changed`, and a
  call from a linked worktree lands into the main checkout's branch. Each
  refusal leaves both branches unmoved. test:
  `tests/tasks/test_land_commit.py::test_landing_branch_is_main_checkout_head`.
- 1.3.9 - A SHA carrying the caller's `independent_review_approval` but not
  linked to the task refuses `candidate_not_linked` with no git write; after
  `link_commit`, the same call lands. test:
  `tests/tasks/test_land_commit.py::test_refuses_unlinked_candidate`.

### 1.4 Moved-tip landing and git failure handling [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/tasks/land_commit.py`
- `tests/tasks/test_land_commit.py`

**Research context:** 1.3's `land_candidate` refuses `tip_moved` whenever the
branch tip is not an ancestor of the candidate. This deliverable replaces that
step with Decision Record item 1. Other sessions commit Markdown directly to
the branch (Decision Record item 7), and the landing lock serializes only
`land_commit` calls, so the tip can move between computing and landing.
Observed in the probe (As-Is Facts): a prebuilt merge commit applied with
`git merge --ff-only` keeps unrelated staged and dirty files, and
`--ff-only` refuses a locally dirty landed path. `git merge-tree
--write-tree` exits 1 on conflicts and prints the tree on its first line.
`daemon_git.run` defaults to a 10-second timeout, and git can move the ref
before a timed-out or failed command returns.

Implementation in `src/gobby/tasks/land_commit.py`:
- Moved paths are `git diff -z --name-only --no-renames <base> <tip>`. A
  non-empty intersection with the candidate paths refuses
  `base_update_required` with the shared paths, reported beside any missing
  approvals.
- Disjoint paths: build `TREE` with `git merge-tree --write-tree <tip> <sha>`
  (conflict exit: refuse `merge_conflict`), then
  `git commit-tree <TREE> -p <tip> -p <sha> -m "chore: land reviewed <short
  sha> for #N"`, and fast-forward to that merge commit with
  `GIT_REFLOG_ACTION` naming `candidate=<sha> mode=merge` (1.3
  step 4). The
  subject carries
  no task tag (Decision Record item 5). The `landing` receipt carries mode
  `merge`, `merge_commit` and `retest_required: true`. The response states
  the retest procedure: the reviewer takes the focused verification commands
  from the task's validation evidence, runs them in the main checkout, and
  reports each command and result, the receipt's `landed_tip`, the HEAD it
  tested and any unrelated checkout changes to the developer and the
  Orchestrator.
- `--ff-only` failures:
  - the tip moved since step 5: re-read the branch and tip and repeat every
    policy check (candidate and moved paths, class, freeze, overlap,
    approvals) before the next write, at most three attempts, then refuse
    `tip_contention`;
  - "would be overwritten": refuse `checkout_dirty` with git's path list;
  - `index.lock`: refuse `checkout_busy` (retryable);
  - anything else: refuse `git_failed` with stderr.
  A refusal before the ref-moving command changes neither ref nor checkout.
- Ref reconciliation: the ref-moving command runs with a 120-second timeout.
  After it returns, fails or times out, `land_commit` reads the branch ref
  once as `observed_tip` before classifying the outcome. When the target (the
  SHA or the merge commit) is contained in `observed_tip`, it is a landing
  whatever git returned, including when a permitted Markdown commit has
  already advanced the branch past it; `landed_tip` stays the target. When
  the target is not contained, the refusal stands. Each retry reconciles
  first, so an earlier attempt that did land counts. A failed receipt
  write then returns `landed: true` with `receipt_pending: true`, and a retry
  records it through 1.3 step 4. A failed message returns `landed: true` with
  `notification_pending` naming the unreached sessions. An interrupted
  command whose ref did not move refuses `git_interrupted` with
  `git status --porcelain` output for the operator. Nothing resets the shared
  checkout to compensate.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_land_commit.py -q`.

**Acceptance:**

- 1.4.1 - With the tip moved by disjoint paths, the branch lands a
  two-parent merge commit (tip, SHA) with the landing message, the receipt
  says `merge` and `retest_required`, and unrelated staged and dirty files in
  the main checkout survive. test:
  `tests/tasks/test_land_commit.py::test_moved_tip_with_disjoint_paths_lands_merge_commit`.
- 1.4.2 - With a moved path shared with the candidate, the call refuses
  `base_update_required` naming that path, and the branch does not move.
  test: `tests/tasks/test_land_commit.py::test_moved_tip_with_shared_paths_requires_base_update`.
- 1.4.3 - A direct commit that moves the tip between computing and landing is
  absorbed by recomputing, and the landing still succeeds. A racing commit
  that touches a candidate path refuses `base_update_required`, one that
  makes the class `restart` adds that missing approval, and three exhausted
  attempts refuse `tip_contention`. test:
  `tests/tasks/test_land_commit.py::test_tip_race_recomputes_and_lands`.
- 1.4.4 - A locally dirty landed path refuses `checkout_dirty` and a held
  `index.lock` refuses `checkout_busy`. Each refusal, a merge conflict and an
  exhausted contention leave the ref, the worktree contents and the index
  entries unchanged, unrelated staged files and untracked files included.
  test:
  `tests/tasks/test_land_commit.py::test_dirty_path_and_index_lock_refuse_without_ref_change`.
- 1.4.5 - `extract_task_ids_from_message` finds no task in the generated
  merge commit's message, so commit discovery never links it to the task.
  test: `tests/tasks/test_land_commit.py::test_landing_merge_is_not_task_tagged`.
- 1.4.6 - A ref that moved before git timed out, and a receipt write that
  fails after the ref moved, both return `landed: true`; the second returns
  `receipt_pending: true`. A failed message returns `notification_pending`.
  A direct Markdown commit that advances the branch past the target before
  reconciliation, after a successful command and after a timeout, still
  returns `landed: true` with `landed_tip` the target and `observed_tip` the
  descendant. The checkout is never reset. test:
  `tests/tasks/test_land_commit.py::test_ref_moved_before_failure_reports_landing`.
- 1.4.7 - A repeated call after a merge landing returns mode `merge` with
  `retest_required: true`. A retry after `receipt_pending` recovers the same
  mode, `landed_tip` and class from the `gobby-land` reflog entry. For
  stacked candidates A and B where B lands by merge, A's later call records
  mode `merge` with `retest_required: true` and A's own class; where B lands
  by fast-forward, A records mode `ff`. Where a foreign fast-forward to A
  lands after B's call observed its tip and B then fast-forwards from A,
  A's later call records `provenance: unknown`. test:
  `tests/tasks/test_land_commit.py::test_merge_landing_replay_preserves_retest_obligation`.

**Granularity:** 1.4 has seven acceptance items and one lifecycle owner, the
write half of the landing call. Moved-tip merging and failure reconciliation
share the ref-moving command and its retry loop, so they stay together.

### 1.5 Protected-branch guard [category: code] (depends: 1.2, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/git_hooks.py::HOOK_TEMPLATES`
- `src/gobby/cli/installers/git_hooks.py::get_stale_git_hooks`
- `src/gobby/install/shared/workflows/rules/worker-safety/block-landing-override.yaml`
- `tests/framing_corpus.py::TRUE_RESTRICTION_RULES`
- `tests/cli/installers/test_git_hooks_installer.py::*` — scope-reason: the expected hook-name set gains reference-transaction, plus stale-detection and foreign-hook chaining tests
- `tests/cli/installers/test_landing_guard_hook.py`
- `tests/workflows/rules/test_landing_override_rule.py`

Consumers unchanged:
- `tests/workflows/test_rewrite_rules.py` — no-edit-reason: reads the corpus sets, which this deliverable extends.
- `src/gobby/utils/deps.py` — no-edit-reason: reports whatever list `get_stale_git_hooks` returns per checkout.

**Research context:** `HOOK_TEMPLATES` in
`src/gobby/cli/installers/git_hooks.py` maps hook names to the Gobby section
that `install_git_hooks` wraps in GOBBY HOOK markers and places before any
preserved foreign content. `get_stale_git_hooks` (called from
`src/gobby/utils/deps.py`) reports only hooks whose installed Gobby section
differs, and skips a missing hook, so today a new template never shows as
stale (As-Is Facts). `TestHookTemplates::test_all_expected_hooks_defined` pins
the key set; `test_ignores_hooks_without_managed_section` pins that an
unmanaged repository reports nothing. Git calls `reference-transaction` with the state (`prepared`,
`committed`, `aborted`) as `$1` and `<old> <new> <refname>` lines on stdin;
a non-zero exit in `prepared` aborts the whole transaction. The main
worktree's `HEAD` file is `<git-common-dir>/HEAD`, also from linked
worktrees. Bash-blocking rules follow
`src/gobby/install/shared/workflows/rules/worker-safety/no-force-push.yaml`
(`event: before_tool`, `effects: [{type: block, tools: [Bash],
command_pattern, reason}]`); `tests/framing_corpus.py` must classify every
before-tool block rule once
(`TestBundledBlockReasonFraming::test_every_live_before_tool_block_is_classified_once`).

Implementation:
- New `HOOK_TEMPLATES["reference-transaction"]`, rendered by a helper in
  `git_hooks.py` from `landing_policy.DIRECT_COMMIT_MARKDOWN_DIRS` and
  `DIRECT_COMMIT_ANY_FILE_DIRS` so the allowance has one source. The POSIX `sh` script:
  - reads stdin into a variable once, and checks only when `$1` is
    `prepared` and `GOBBY_LAND_COMMIT` is not `1`;
  - reads `<git-common-dir>/HEAD`; checks only when it is `ref: <name>`,
    which is the protected ref;
  - for each captured line naming exactly the protected ref: allows creation
    (zero old), refuses deletion (zero new), and otherwise reads
    `git diff --name-only --no-renames <old> <new>` line by line with
    `IFS= read -r`; any path that `is_direct_commit_path` would reject refuses,
    and so does any path git quoted (a leading `"`), which covers newlines,
    tabs and other escaped names;
  - fails closed when `git diff` fails;
  - refusal text names the path, `gobby-tasks-ops:land_commit`, and the
    operator override `GOBBY_LAND_COMMIT=1`.
  Only a refusal exits (status 1). An allowed update never exits: the
  section ends by restoring the captured lines as stdin (`exec 0<<EOF` with
  the variable; `/dev/null` when it is empty), so preserved foreign hook
  content after it runs with the same state argument and stdin, and can
  still refuse. The override bypasses only Gobby's checks. The script runs no
  Python, so commits pay no interpreter start.
- The same chaining applies to the existing templates (Orchestrator found
  work, 12:28 CT): `pre-push` and `pre-merge-commit` drop their early
  `exit 0` for an if/else, and `pre-push` (`PUSH_REFS`) and `post-rewrite`
  capture stdin once and restore it the same way. Each still exits non-zero on its
  own failure.
- `get_stale_git_hooks`: when at least one template hook carries a Gobby
  section, a template hook that is missing or has no Gobby section is stale.
  A repository with no Gobby section anywhere still reports nothing.
- New rule `block-landing-override` (group `worker-safety`, enabled, every
  session): blocks a Bash command containing `GOBBY_LAND_COMMIT` with a
  true-restriction reason that names `land_commit`. Added to
  `TRUE_RESTRICTION_RULES`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_landing_guard_hook.py tests/cli/installers/test_git_hooks_installer.py tests/workflows/rules/test_landing_override_rule.py tests/workflows/test_rewrite_rules.py tests/tasks/test_land_commit.py -q`.
Hook tests install the template into a `tmp_path` repository with a linked
worktree and drive real git.

**Acceptance:**

- 1.5.1 - In the main checkout, a commit touching only `docs/x.md` and a root
  `NOTES.md` succeeds, and so does a commit adding a coverage manifest and
  `.gobby/plans/coverage/.regenerate.log` and one that deletes a coverage
  manifest while moving a plan `.md`. A commit touching `src/gobby/a.py`,
  `docs/reference-audit/a.json` or `.gobby/plans/x.svg` is refused with the
  path named. The hook's patterns match `is_direct_commit_path` on the same
  path list. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_direct_commit_allows_listed_markdown_and_coverage`.
- 1.5.2 - From a linked worktree, `git merge`, `git reset` and `git branch -f`
  that move the protected branch to code are refused, while commits on the
  lane branch pass. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_protected_branch_refuses_writes_from_any_worktree`.
- 1.5.3 - With `GOBBY_LAND_COMMIT=1`, `land_commit`'s fast-forward passes the
  hook. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_land_commit_passes_installed_guard`.
- 1.5.4 - The installer defines `reference-transaction`. test:
  `tests/cli/installers/test_git_hooks_installer.py::TestHookTemplates::test_all_expected_hooks_defined`.
- 1.5.5 - An agent Bash command mentioning `GOBBY_LAND_COMMIT` is blocked for
  spawned and interactive sessions. test:
  `tests/workflows/rules/test_landing_override_rule.py::test_landing_override_is_blocked_for_every_session`.
- 1.5.6 - An install made before `reference-transaction` existed reports it
  stale, and `gobby install` clears it; a repository with no Gobby section
  still reports nothing. test:
  `tests/cli/installers/test_git_hooks_installer.py::test_missing_template_hook_is_stale_for_managed_install`.
- 1.5.7 - With a foreign hook after the Gobby section that records its stdin
  and arguments, the foreign hook runs with the original stdin for an
  allowed `reference-transaction` update, a delete-only `pre-push`, a
  `pre-merge-commit` under `GOBBY_MERGE=1` and a `post-rewrite`. A foreign
  `reference-transaction` refusal of a Gobby-allowed update still aborts it.
  Reinstall and uninstall keep the foreign content. test:
  `tests/cli/installers/test_git_hooks_installer.py::test_gobby_sections_chain_to_foreign_hook_content`.
- 1.5.8 - A direct commit mixing `docs/a.md` with `src/gobby/a.py`, and one
  touching a Markdown path that git quotes (a tab or newline in the name),
  are refused; a path with spaces under `docs/` is allowed. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_guard_classifies_unusual_paths_fail_closed`.

**Granularity:** 1.5 has eight acceptance items and one owner, the managed
git hook set in `git_hooks.py`. The guard, the chaining fix and stale
detection change the same templates and installer, and 1.5.6 and 1.5.7 are
what make the guard live and safe beside foreign hooks.

## P2: Documentation
`kind: framing`

### 2.1 Landing references and guide [category: docs] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`
- `docs/guides/tasks.md`

**Research context:** `references/tasks/closing.md` documents
`record_close_receipt` kinds and authority (the "Peer evidence" paragraph).
`docs/guides/tasks.md` has `### Close` under `## Agent Workflow` and
`## Git and Validation`. Role files under `.gobby/roles/` are the Orchestrator's
(Rollout step 4).

Implementation:
- `closing.md`: the receipt paragraph adds `landing_approval` (creator or
  delegator; reasons `restart`, `freeze`, `overlap`) and the daemon-only
  `landing`; one sentence says a code task closes after `land_commit` records
  its landing.
- `docs/guides/tasks.md`: a `### Landing` section after `### Close` covering
  the reviewer flow (the developer links the exact candidate with
  `link_commit` before requesting LAND), who may call each tool and grant
  each approval, replay and `provenance` (the `gobby-land` reflog action,
  and the Rollout step 4 repair path for `unknown`), path
  classes, approvals and freeze, moved-tip behavior with the retest
  procedure (1.4: commands from the task's validation evidence, the tested
  HEAD reported, a failed retest withholds the Lane Manager's close release
  and never rolls the branch back), every refusal code and pending flag in
  `land_commit.py`, the direct-commit allowance, the guard and its operator
  override.

Planned verification: `uv run gobby plans validate .gobby/plans/reviewer-landing.md -p /Users/josh/Projects/gobby`
and a read-through against the shipped tool schemas.

**Acceptance:**

- 2.1.1 - The closing reference names both new receipt kinds, their
  authority, append-only approvals and the landing-before-close order.
  behavior: "landing_approval" in
  `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.
- 2.1.2 - Read against the shipped `land_commit` and `set_landing_freeze`
  schemas, the guide states who may call each tool and grant each approval
  reason, that the candidate is linked before LAND, what each `provenance`
  value means, and the freeze's effect and unreadable-file rule. behavior:
  "set_landing_freeze" in `docs/guides/tasks.md`.
- 2.1.3 - The guide lists the four path classes with their approvals, and
  every refusal code and pending flag that `land_commit.py` returns. behavior:
  "base_update_required" in `docs/guides/tasks.md`.
- 2.1.4 - The guide gives the moved-tip retest procedure and its
  failed-retest path. behavior: "retest_required" in `docs/guides/tasks.md`.
- 2.1.5 - The guide states the direct-commit allowance (listed Markdown and
  `.gobby/plans/coverage/`), the guard and its operator override. behavior: "GOBBY_LAND_COMMIT" in
  `docs/guides/tasks.md`.

## Rollout
`kind: framing`

1. 1.1 to 1.4 land through the Merge Manager as today; one Orchestrator
   restart activates them.
2. The Orchestrator switches routing: a reviewer's LAND is followed by its own
   `land_commit`. The Merge Manager finishes its in-flight landing tasks and
   its `0.5.0` landing duty retires. It keeps its other duty, merging extra
   worktrees into a lane's primary worktree, under Josh's kickoff rule in
   force today.
3. 1.5 lands through `land_commit` (restart class, so with a `restart`
   approval). After the restart that syncs the rule, the Orchestrator runs
   `gobby install` from the main checkout, confirms `get_stale_git_hooks` is
   empty, and confirms an executable `reference-transaction` with the Gobby
   section at `git rev-parse --git-path hooks`.
4. At step 2 the Orchestrator edits `.gobby/roles/merge-manager.md`,
   `code-reviewer.md`, `lane-manager.md`, `orchestrator.md` and `_common.md`:
   developers run `link_commit` on the exact candidate before requesting
   LAND; reviewers land with `land_commit` and, when `retest_required`, run
   the 1.4 retest procedure; Lane Managers check the `landing` receipt and,
   for a merge landing, a passing retest report before a close release; a
   failed or unavailable retest withholds the release and enters the
   found-work path, never a rollback; an `activation_class: unknown` receipt
   withholds the release until its repair path completes: the reviewer runs
   the retest (`retest_required` is true), the Orchestrator classifies the
   candidate's paths against its reviewed base with `classify_paths` and
   messages the class to the Lane Manager, and the Lane Manager releases
   once that activation is live and the retest passed; the Orchestrator
   owns `set_landing_freeze` and approvals.
5. The close-order rule (memory 283e9a81) keeps "code closes after landing".
   Its spacing of landings between close batches existed for the Merge
   Manager's index commands; whether to keep it is the Orchestrator's call.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15429 on #23527,
  rescoped to M3 by Josh's 11:56 CT ruling.
- 2026-10-05: Josh ruled decision 7 at 12:24 CT ("guard, direct commits for
  Markdown only"); Decision Record item 7 is adopted.
- 2026-10-05: Enhancer pass (run d4113107, 12 suggestions on e352c34)
  applied per the Orchestrator's 12:28 CT dispositions. E01 untagged merge
  subject; E02 stale detection for missing template hooks; E03 ref
  reconciliation with pending flags; E04 replay keeps the stored receipt;
  E05 retries repeat every check and the freeze setter takes the landing
  lock; E06 append-only approvals; E07 branch from main HEAD under the lock,
  with no hardcoded name; E08 guard chaining (narrowed); E09 `-z` paths and
  fail-closed quoted paths (narrowed); E10 lock and preservation tests; E11
  retest procedure; E12 docs acceptance as a checklist (decision-7 part
  moot). Found work folded into 1.5: the `pre-push`, `pre-merge-commit` and
  `post-rewrite` templates chain to foreign hook content.
- 2026-10-05: Adversary gobby#15414 findings on 3eaee73 accepted. Blocking:
  F-M3-descendant-ref (reconciliation and 1.3 step 9 test containment in a
  single observed tip); F-M3-replay-provenance (replay without a receipt
  recovers mode, base, class and retest from the branch reflog, with
  `provenance: unknown` as the fallback); F-M3-linked-source (refuse
  `candidate_not_linked`, link before LAND). Nit: N-M3-primary-duty (the
  Merge Manager keeps the lane-primary merge duty). The Orchestrator took
  both role-file consequences at 12:46 CT.
- 2026-10-05: Adversary refinements on f32f6f1: the linked-candidate check
  compares full OIDs resolved by git; replay recovery reads a
  `GIT_REFLOG_ACTION` `gobby-land` entry because topology cannot tell a
  generated merge from a fast-forwarded two-parent candidate; rewound,
  foreign, branch-creation, missing-object and unreadable history is
  `provenance: unknown`, never inferred; Rollout step 4 gives the unknown
  repair path. The action also records `base=<old tip>`, because
  `git log -g` exposes no stored old OID; recovery never rebuilds an old
  value from neighboring entries.
- 2026-10-05: Adversary refinement on 57b9ad5 (ancestor-advance race):
  `--ff-only` succeeds from any ancestor of its target, so an observed
  `base=` can name a tip older than the write's real old value and
  misattribute a foreign landing (probed O→A→B). The action drops `base=`.
  Recovery reads git's stored `old` from the `files` branch log, one line at
  a time, and every other ref format or malformed line is `provenance:
  unknown`. 1.3.5 and 1.4.7 cover the race. A merge landing cannot hit it:
  a forward move that stays an ancestor of the generated merge would make
  the candidate a fast-forward.
- 2026-10-05 13:06 CT: Writer gobby#15429 and Adversary gobby#15414 reached
  consensus on 3b57838146f8b1ab903b177aeb8a4b19a9465aaf (blob 28f4f39627,
  base validation exit 0). F-M3-descendant-ref, F-M3-replay-provenance,
  F-M3-linked-source and N-M3-primary-duty are resolved; no findings remain.
  The Adversary derives M1 next. Josh approves before expansion.
- 2026-10-05 13:21 CT: Lane 7 reviewer gobby#15396 bounced 250eecbd2d. B1:
  the Markdown-only guard left coverage manifests and
  `.gobby/plans/coverage/.regenerate.log` without an executable route.
  Josh chose (a) (Orchestrator 13:19 CT): Decision Record item 7, 1.2
  (`DIRECT_COMMIT_ANY_FILE_DIRS`, 1.2.2) and 1.5 (hook generation, 1.5.1)
  allow any file under `.gobby/plans/coverage/`. LOW-1: the landing env
  starts from `git_subprocess_env() or os.environ` so `daemon_git.run`'s git
  `PATH` fallback survives. M1 is stale until the Adversary re-derives it.
- 2026-10-05 13:33 CT: Writer gobby#15429 and Adversary gobby#15414 renewed
  consensus on the B1 and LOW-1 repair at ae0f0acfc43660ec9496bcb23b074d9d629acb95
  (blob da352b86b0, base validation exit 0). No findings remain. The stale
  M1 from 250eecbd2d is withdrawn whole; the Adversary derives a fresh one.
  Josh re-approves before expansion.

## V2: Verification
`kind: verification`

Each leaf runs its planned verification after its final edit. After the last
leaf lands:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/tasks/test_close_receipts.py tests/tasks/test_agentic_close_review.py tests/tasks/test_landing_policy.py tests/tasks/test_land_commit.py tests/mcp_proxy/tools/tasks/test_landing_tools.py tests/storage/hub/test_postgres_placeholder_remap.py tests/workflows/test_task_enforcement_rules.py tests/mcp_proxy/tools/test_read_only_classification.py tests/cli/installers/test_landing_guard_hook.py tests/cli/installers/test_git_hooks_installer.py tests/workflows/rules/test_landing_override_rule.py tests/workflows/test_rewrite_rules.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/reviewer-landing.md -p /Users/josh/Projects/gobby
```

After rollout step 3, in the main checkout: a Markdown-only plan commit
succeeds, and `git commit --allow-empty` with a staged `src/` path is refused
by the guard.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Landing receipt kinds
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: A `landing_approval` is recorded from the task''s creator
    or delegator and refused from any other session, the claimant included. test:
    `tests/tasks/test_close_receipts.py::test_landing_approval_comes_only_from_task_creator_or_delegator`.

    1.1.2: A `landing_approval` without a `reason`, or with a part outside `restart`,
    `freeze`, `overlap`, is refused; `restart,overlap` with a `batch` fact is accepted.
    test: `tests/tasks/test_close_receipts.py::test_landing_approval_requires_known_reasons`.

    1.1.3: The MCP tool refuses kind `landing`, and its schema enum omits it. test:
    `tests/tasks/test_close_receipts.py::test_tool_refuses_daemon_only_landing_kind`.

    1.1.4: A `landing` receipt recorded through the module function is stored once
    per author and SHA and returned on replay. test: `tests/tasks/test_close_receipts.py::test_landing_receipt_is_recorded_once_per_author_and_commit`.

    1.1.5: The close-review prompt explains `landing_approval` and `landing` receipts
    when the task carries them. test: `tests/tasks/test_close_receipts.py::test_reviewer_prompt_explains_landing_receipts`.

    1.1.6: The creator approves `restart`, then `freeze,overlap`, for one SHA: two
    receipts exist, the first unchanged, and repeating either call adds none. test:
    `tests/tasks/test_close_receipts.py::test_landing_approval_new_reasons_append_receipt`.'
  labels:
  - covers:reviewer-landing:1.1:1.1.1
  - covers:reviewer-landing:1.1:1.1.2
  - covers:reviewer-landing:1.1:1.1.3
  - covers:reviewer-landing:1.1:1.1.4
  - covers:reviewer-landing:1.1:1.1.5
  - covers:reviewer-landing:1.1:1.1.6
  tdd: false
  source_section: '1.1'
  implementation_domain: backend
- title: Landing policy and freeze flag
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.2.1: `classify_paths` returns the strongest class: a `crates/`
    path or `src/gobby/storage/schema_expected_identity.json` is cutover, `src/gobby/cli/`
    is restart, `web/` alone is ui_build, top-level project pipeline YAML is sync,
    bundled agent YAML is reload, and `docs/` plus `tests/` is none. test:
    `tests/tasks/test_landing_policy.py::test_classify_paths_strongest_class_wins`.

    1.2.2: `is_direct_commit_path` accepts `*.md` at the root or under `.gobby/plans/`,
    `.gobby/roles/`, `docs/`, plus `.gobby/plans/coverage/<project>/23273/x.coverage.yaml`
    and `.gobby/plans/coverage/.regenerate.log`, and rejects `docs/reference-audit/admin.json`,
    `.gobby/plans/x.svg`, `.gobby/plans/coverage-notes/x.yaml` and `src/gobby/AGENTS.md`.
    test: `tests/tasks/test_landing_policy.py::test_direct_commit_allows_listed_markdown_and_coverage`.

    1.2.3: A missing freeze file reads as off and a malformed one as on. test: `tests/tasks/test_landing_policy.py::test_unreadable_freeze_file_reads_as_frozen`.

    1.2.4: `set_landing_freeze` records the calling session, refuses `on` without
    a reason, and a clear keeps the clearing session. test: `tests/mcp_proxy/tools/tasks/test_landing_tools.py::test_set_landing_freeze_records_setter_and_clearer`.

    1.2.5: The registered gobby-tasks-ops tools equal the classified tuples, with
    `set_landing_freeze` gated as a mutation. test: `tests/workflows/test_task_enforcement_rules.py::TestRequireTasksSkillForMutations::test_real_registry_inventory_matches_independent_classification`.'
  labels:
  - covers:reviewer-landing:1.2:1.2.1
  - covers:reviewer-landing:1.2:1.2.2
  - covers:reviewer-landing:1.2:1.2.3
  - covers:reviewer-landing:1.2:1.2.4
  - covers:reviewer-landing:1.2:1.2.5
  tdd: false
  source_section: '1.2'
  implementation_domain: backend
- title: land_commit fast-forward landing with approvals
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: "1.3.1: Without the caller's `independent_review_approval`\
    \ for the exact SHA, or when the caller claims the task, the call refuses and\
    \ the branch does not move. test: `tests/tasks/test_land_commit.py::test_refuses_without_callers_land_receipt`.\n\
    1.3.2: A docs-only candidate whose base is the tip fast-forwards the branch to\
    \ the exact SHA, records a `landing` receipt with mode `ff` and class `none`,\
    \ and messages the claimant and creator. test: `tests/tasks/test_land_commit.py::test_fast_forward_lands_exact_candidate`.\n\
    1.3.3: A restart-class candidate, an active freeze and an overlapping in-flight\
    \ candidate on another task each add their reason; one response lists all three\
    \ with no git write, and matching `landing_approval` receipts let the same call\
    \ land. test: `tests/tasks/test_land_commit.py::test_reports_every_missing_approval_at_once`.\n\
    1.3.4: In-flight candidates that are ancestors or descendants of the SHA, or already\
    \ on the tip, never count as overlap. test: `tests/tasks/test_land_commit.py::test_stacked_and_landed_candidates_never_overlap`.\n\
    1.3.5: A SHA already contained in the tip lands with no git write and one `landing`\
    \ receipt across repeated calls. A SHA fast-forwarded earlier by `land_commit`\
    \ without a receipt recovers mode `ff`, the recorded class and `provenance: reflog`.\
    \ A reviewed two-parent candidate that was fast-forwarded recovers mode `ff`.\
    \ For O\u2192A\u2192B where `land_commit` observes O for B, a foreign fast-forward\
    \ lands A, and `--ff-only` to B succeeds from A, replay of B recovers mode `ff`\
    \ and replay of A records `provenance: unknown`. An expired or unreadable reflog,\
    \ a non-`files` ref format, a log line outside the grammar, a landing written\
    \ without the `gobby-land` action, a branch-creation entry, and an event the branch\
    \ was rewound past each record `provenance: unknown`, mode `already_landed`, `activation_class:\
    \ unknown` and `retest_required: true`. test: `tests/tasks/test_land_commit.py::test_already_landed_candidate_records_landing_once`.\n\
    1.3.6: `MainCheckoutLanding` maps to `main_checkout_landing:<project_id>` and\
    \ has the lowest lock priority. test: `tests/storage/hub/test_postgres_placeholder_remap.py::test_main_checkout_landing_lock_key`.\n\
    1.3.7: Two concurrent `land_commit` calls run one at a time, and the lock is free\
    \ again after a refusal and after an exception. A freeze set while a landing holds\
    \ the lock applies to the next landing. test: `tests/tasks/test_land_commit.py::test_concurrent_landings_serialize_and_release_lock`.\n\
    1.3.8: A detached main HEAD refuses `main_checkout_detached`, a branch switched\
    \ before the write refuses `main_checkout_branch_changed`, and a call from a linked\
    \ worktree lands into the main checkout's branch. Each refusal leaves both branches\
    \ unmoved. test: `tests/tasks/test_land_commit.py::test_landing_branch_is_main_checkout_head`.\n\
    1.3.9: A SHA carrying the caller's `independent_review_approval` but not linked\
    \ to the task refuses `candidate_not_linked` with no git write; after `link_commit`,\
    \ the same call lands. test: `tests/tasks/test_land_commit.py::test_refuses_unlinked_candidate`."
  labels:
  - covers:reviewer-landing:1.3:1.3.1
  - covers:reviewer-landing:1.3:1.3.2
  - covers:reviewer-landing:1.3:1.3.3
  - covers:reviewer-landing:1.3:1.3.4
  - covers:reviewer-landing:1.3:1.3.5
  - covers:reviewer-landing:1.3:1.3.6
  - covers:reviewer-landing:1.3:1.3.7
  - covers:reviewer-landing:1.3:1.3.8
  - covers:reviewer-landing:1.3:1.3.9
  tdd: false
  source_section: '1.3'
  implementation_domain: backend
- title: Moved-tip landing and git failure handling
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: With the tip moved by disjoint paths, the branch lands
    a two-parent merge commit (tip, SHA) with the landing message, the receipt says
    `merge` and `retest_required`, and unrelated staged and dirty files in the main
    checkout survive. test: `tests/tasks/test_land_commit.py::test_moved_tip_with_disjoint_paths_lands_merge_commit`.

    1.4.2: With a moved path shared with the candidate, the call refuses `base_update_required`
    naming that path, and the branch does not move. test: `tests/tasks/test_land_commit.py::test_moved_tip_with_shared_paths_requires_base_update`.

    1.4.3: A direct commit that moves the tip between computing and landing is absorbed
    by recomputing, and the landing still succeeds. A racing commit that touches a
    candidate path refuses `base_update_required`, one that makes the class `restart`
    adds that missing approval, and three exhausted attempts refuse `tip_contention`.
    test: `tests/tasks/test_land_commit.py::test_tip_race_recomputes_and_lands`.

    1.4.4: A locally dirty landed path refuses `checkout_dirty` and a held `index.lock`
    refuses `checkout_busy`. Each refusal, a merge conflict and an exhausted contention
    leave the ref, the worktree contents and the index entries unchanged, unrelated
    staged files and untracked files included. test: `tests/tasks/test_land_commit.py::test_dirty_path_and_index_lock_refuse_without_ref_change`.

    1.4.5: `extract_task_ids_from_message` finds no task in the generated merge commit''s
    message, so commit discovery never links it to the task. test: `tests/tasks/test_land_commit.py::test_landing_merge_is_not_task_tagged`.

    1.4.6: A ref that moved before git timed out, and a receipt write that fails after
    the ref moved, both return `landed: true`; the second returns `receipt_pending:
    true`. A failed message returns `notification_pending`. A direct Markdown commit
    that advances the branch past the target before reconciliation, after a successful
    command and after a timeout, still returns `landed: true` with `landed_tip` the
    target and `observed_tip` the descendant. The checkout is never reset. test: `tests/tasks/test_land_commit.py::test_ref_moved_before_failure_reports_landing`.

    1.4.7: A repeated call after a merge landing returns mode `merge` with `retest_required:
    true`. A retry after `receipt_pending` recovers the same mode, `landed_tip` and
    class from the `gobby-land` reflog entry. For stacked candidates A and B where
    B lands by merge, A''s later call records mode `merge` with `retest_required:
    true` and A''s own class; where B lands by fast-forward, A records mode `ff`.
    Where a foreign fast-forward to A lands after B''s call observed its tip and B
    then fast-forwards from A, A''s later call records `provenance: unknown`. test:
    `tests/tasks/test_land_commit.py::test_merge_landing_replay_preserves_retest_obligation`.'
  labels:
  - covers:reviewer-landing:1.4:1.4.1
  - covers:reviewer-landing:1.4:1.4.2
  - covers:reviewer-landing:1.4:1.4.3
  - covers:reviewer-landing:1.4:1.4.4
  - covers:reviewer-landing:1.4:1.4.5
  - covers:reviewer-landing:1.4:1.4.6
  - covers:reviewer-landing:1.4:1.4.7
  tdd: false
  source_section: '1.4'
  implementation_domain: backend
- title: Protected-branch guard
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.4'
  validation_criteria: '1.5.1: In the main checkout, a commit touching only `docs/x.md`
    and a root `NOTES.md` succeeds, and so does a commit adding a coverage manifest
    and `.gobby/plans/coverage/.regenerate.log` and one that deletes a coverage manifest
    while moving a plan `.md`. A commit touching `src/gobby/a.py`, `docs/reference-audit/a.json`
    or `.gobby/plans/x.svg` is refused with the path named. The hook''s patterns match
    `is_direct_commit_path` on the same path list. test: `tests/cli/installers/test_landing_guard_hook.py::test_direct_commit_allows_listed_markdown_and_coverage`.

    1.5.2: From a linked worktree, `git merge`, `git reset` and `git branch -f` that
    move the protected branch to code are refused, while commits on the lane branch
    pass. test: `tests/cli/installers/test_landing_guard_hook.py::test_protected_branch_refuses_writes_from_any_worktree`.

    1.5.3: With `GOBBY_LAND_COMMIT=1`, `land_commit`''s fast-forward passes the hook.
    test: `tests/cli/installers/test_landing_guard_hook.py::test_land_commit_passes_installed_guard`.

    1.5.4: The installer defines `reference-transaction`. test: `tests/cli/installers/test_git_hooks_installer.py::TestHookTemplates::test_all_expected_hooks_defined`.

    1.5.5: An agent Bash command mentioning `GOBBY_LAND_COMMIT` is blocked for spawned
    and interactive sessions. test: `tests/workflows/rules/test_landing_override_rule.py::test_landing_override_is_blocked_for_every_session`.

    1.5.6: An install made before `reference-transaction` existed reports it stale,
    and `gobby install` clears it; a repository with no Gobby section still reports
    nothing. test: `tests/cli/installers/test_git_hooks_installer.py::test_missing_template_hook_is_stale_for_managed_install`.

    1.5.7: With a foreign hook after the Gobby section that records its stdin and
    arguments, the foreign hook runs with the original stdin for an allowed `reference-transaction`
    update, a delete-only `pre-push`, a `pre-merge-commit` under `GOBBY_MERGE=1` and
    a `post-rewrite`. A foreign `reference-transaction` refusal of a Gobby-allowed
    update still aborts it. Reinstall and uninstall keep the foreign content. test:
    `tests/cli/installers/test_git_hooks_installer.py::test_gobby_sections_chain_to_foreign_hook_content`.

    1.5.8: A direct commit mixing `docs/a.md` with `src/gobby/a.py`, and one touching
    a Markdown path that git quotes (a tab or newline in the name), are refused; a
    path with spaces under `docs/` is allowed. test: `tests/cli/installers/test_landing_guard_hook.py::test_guard_classifies_unusual_paths_fail_closed`.'
  labels:
  - covers:reviewer-landing:1.5:1.5.1
  - covers:reviewer-landing:1.5:1.5.2
  - covers:reviewer-landing:1.5:1.5.3
  - covers:reviewer-landing:1.5:1.5.4
  - covers:reviewer-landing:1.5:1.5.5
  - covers:reviewer-landing:1.5:1.5.6
  - covers:reviewer-landing:1.5:1.5.7
  - covers:reviewer-landing:1.5:1.5.8
  tdd: false
  source_section: '1.5'
  implementation_domain: backend
- title: Landing references and guide
  category: docs
  task_type: chore
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.4'
  - '1.5'
  validation_criteria: '2.1.1: The closing reference names both new receipt kinds,
    their authority, append-only approvals and the landing-before-close order. behavior:
    "landing_approval" in `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.

    2.1.2: Read against the shipped `land_commit` and `set_landing_freeze` schemas,
    the guide states who may call each tool and grant each approval reason, that the
    candidate is linked before LAND, what each `provenance` value means, and the freeze''s
    effect and unreadable-file rule. behavior: "set_landing_freeze" in `docs/guides/tasks.md`.

    2.1.3: The guide lists the four path classes with their approvals, and every refusal
    code and pending flag that `land_commit.py` returns. behavior: "base_update_required"
    in `docs/guides/tasks.md`.

    2.1.4: The guide gives the moved-tip retest procedure and its failed-retest path.
    behavior: "retest_required" in `docs/guides/tasks.md`.

    2.1.5: The guide states the direct-commit allowance (listed Markdown and `.gobby/plans/coverage/`),
    the guard and its operator override. behavior: "GOBBY_LAND_COMMIT" in `docs/guides/tasks.md`.'
  labels:
  - covers:reviewer-landing:2.1:2.1.1
  - covers:reviewer-landing:2.1:2.1.2
  - covers:reviewer-landing:2.1:2.1.3
  - covers:reviewer-landing:2.1:2.1.4
  - covers:reviewer-landing:2.1:2.1.5
  tdd: false
  source_section: '2.1'
  assigned_agent: tech-writer
```
