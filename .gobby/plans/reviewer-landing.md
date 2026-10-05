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
plans, roles and docs. It needs Orchestrator approval only for a
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
   names an Orchestrator restart batch. One receipt per SHA; the tool takes
   the union of reasons across that SHA's approvals.
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
4. **Path classes (Josh decision 5).** The strongest class among the
   candidate's changed paths wins, in the order cutover, restart, ui_build,
   none:
   - cutover: `crates/**`, `Cargo.toml`, `Cargo.lock`,
     `src/gobby/storage/schema_expected_identity.json`;
   - restart: `src/gobby/**` (the CLI included, because the daemon imports
     `gobby.cli`), `pyproject.toml`, `uv.lock`;
   - ui_build: `web/**` (the daemon serves `web/dist` from disk; `gobby ui
     build` activates it with no restart and no approval);
   - none: everything else.
   cutover and restart both require a `restart` approval.
5. **Landing record.** `land_commit` writes a daemon-only `landing` close
   receipt, authored by the landing reviewer and naming the candidate SHA. Its
   facts hold the branch, the landed tip, the mode, the activation class, the
   merge commit and whether a retest is required. The REST comment route
   already refuses forged close receipts, receipts are idempotent per author,
   kind and SHA, and the close reviewer already reads them.
   - Rejected: linking the merge commit to the task. Close gates 7, 8 and 13
     read linked commits, and a merge commit's combined diff is usually empty.
   - Rejected: a plain task comment. Forgeable through REST, and it needs its
     own idempotency.
6. **Overlap.** The in-flight set is every `independent_review_approval`
   receipt on another open task in the project whose SHA is not an ancestor
   of the tip. Candidates that are an ancestor or a descendant of this SHA are
   excluded, because a stacked lane branch lands them together. An overlap is
   a non-empty intersection of changed paths, each side measured from its
   merge base with the tip.
7. **Guard and direct-commit allowance (pending Josh, Orchestrator 12:09
   CT).** A `reference-transaction` hook refuses every update of the branch
   checked out in the main worktree unless `GOBBY_LAND_COMMIT=1` is set or
   every changed path is a `*.md` file at the repository root or under
   `.gobby/plans/`, `.gobby/roles/` or `docs/`. Plan writers and the
   Orchestrator keep committing those directly (69 of 123 commits on
   2026-10-05). `docs/reference-audit/*.json` stays out because tests read it
   (#23484). A bundled rule refuses agent shell commands that mention
   `GOBBY_LAND_COMMIT`. Consequence: plan coverage manifests (`*.yaml`) and
   research attachments (`*.svg`, `*.txt`) under `.gobby/plans/` land through
   `land_commit`.
   - Alternative (report §3): every commit goes through `land_commit`, so each
     plan or doc commit first needs an Adversary or reviewer receipt.

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
  environment.
- Hooks: `src/gobby/cli/installers/git_hooks.py::HOOK_TEMPLATES` drives
  install, uninstall and `get_stale_git_hooks`; `core.hooksPath` is the shared
  `.git/hooks`, so one install covers every worktree.
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
moves `0.5.0` except Markdown plans, roles and docs.

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
- `ACTIVATION_CLASSES = ("none", "ui_build", "restart", "cutover")`, weakest
  first, and `classify_paths(paths) -> str` returning the strongest class per
  Decision Record item 4 (`none` for an empty set).
- `DIRECT_COMMIT_MARKDOWN_DIRS = (".gobby/plans/", ".gobby/roles/", "docs/")`
  and `is_direct_commit_path(path) -> bool`: true only for a path ending in
  `.md` that has no `/` or starts with one of those prefixes. 1.5 generates
  the hook's shell patterns from these two names.
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
  is restart, `web/` alone is ui_build, and `docs/` plus `tests/` is none.
  test: `tests/tasks/test_landing_policy.py::test_classify_paths_strongest_class_wins`.
- 1.2.2 - `is_direct_commit_path` accepts only `*.md` at the root or under
  `.gobby/plans/`, `.gobby/roles/`, `docs/`, and rejects
  `docs/reference-audit/admin.json`, `.gobby/plans/coverage/x.yaml` and
  `src/gobby/AGENTS.md`. test:
  `tests/tasks/test_landing_policy.py::test_direct_commit_allows_only_listed_markdown`.
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
- `src/gobby/storage/hub/postgres.py` — no-edit-reason: imports advisory_lock_keys and passes any LockTarget through.
- `tests/storage/test_manager_surface_parity.py` — no-edit-reason: asserts other targets' keys.

**Research context:** reused pieces, all read-only here:
- `close_receipts.list_close_receipts(db, task_id)` and
  `close_receipts.record_close_receipt` (1.1 kinds) in
  `src/gobby/tasks/close_receipts.py`;
- `landing_policy.classify_paths`, `read_freeze` (1.2);
- `daemon_git.run` and `GitOk` in `src/gobby/utils/daemon_git.py`; pass
  `env={**os.environ, "GOBBY_LAND_COMMIT": "1"}` to the ref-moving command;
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
   is the parent of the git common dir; the target branch is its
   `symbolic-ref --short HEAD` (detached: refuse `main_checkout_detached`).
2. Holds `MainCheckoutLanding(project_id)` for the rest of the call.
3. Refuses `review_receipt_missing` unless the caller authored an
   `independent_review_approval` receipt on the task naming `commit_sha`, and
   refuses `caller_is_claimant` when the caller now claims the task.
4. When `commit_sha` is already an ancestor of the branch tip: no git write;
   records the `landing` receipt with mode `already_landed` and notifies.
   Closed tasks land like open ones.
5. Computes the merge base with the tip and the candidate paths
   (`git diff --name-only --no-renames <base> <sha>`), then the activation
   class.
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
9. Otherwise runs `git merge --ff-only <sha>` in the main checkout with the
   override variable, then confirms the branch ref equals `sha`.
10. Records the `landing` receipt (author: caller; facts: `branch`,
    `landed_tip`, `mode`, `activation_class`, `retest_required`, and
    `merge_commit` when present) and messages the task's claimant, creator and
    delegator, deduplicated and excluding the caller: `Landed #N "<title>"
    <short sha> on <branch> at <short tip>; activation <class>`.
11. Returns `landed: true`, `mode`, `branch`, `landed_tip`,
    `activation_class`, `retest_required`.

**Granularity:** 1.3 has six acceptance items and one lifecycle owner, the
landing call. Moved-tip handling is split into 1.4 because it is
independently testable once 1.3 refuses `tip_moved`.

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
  `landing` receipt with mode `already_landed` across repeated calls. test:
  `tests/tasks/test_land_commit.py::test_already_landed_candidate_records_landing_once`.
- 1.3.6 - `MainCheckoutLanding` maps to `main_checkout_landing:<project_id>`
  and has the lowest lock priority. test:
  `tests/storage/hub/test_postgres_placeholder_remap.py::test_main_checkout_landing_lock_key`.

### 1.4 Moved-tip landing [category: code] (depends: 1.3)
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

Implementation in `src/gobby/tasks/land_commit.py`:
- Moved paths are `git diff --name-only --no-renames <base> <tip>`. A
  non-empty intersection with the candidate paths refuses
  `base_update_required` with the shared paths, reported beside any missing
  approvals.
- Disjoint paths: build `TREE` with `git merge-tree --write-tree <tip> <sha>`
  (conflict exit: refuse `merge_conflict`), then
  `git commit-tree <TREE> -p <tip> -p <sha> -m "[<project>-#N] chore: land
  reviewed <short sha>"`, and fast-forward to that merge commit. The
  `landing` receipt carries mode `merge`, `merge_commit` and
  `retest_required: true`; the response tells the reviewer to run the
  candidate's focused tests in the main checkout and report the result to the
  developer and the Orchestrator.
- `--ff-only` failures:
  - the tip moved since step 5: recompute from the ancestry check, at most
    three attempts, then refuse `tip_contention`;
  - "would be overwritten": refuse `checkout_dirty` with git's path list;
  - `index.lock`: refuse `checkout_busy` (retryable);
  - anything else: refuse `git_failed` with stderr.
  No refusal leaves a ref change or a working-tree change behind.

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
  absorbed by recomputing, and the landing still succeeds. test:
  `tests/tasks/test_land_commit.py::test_tip_race_recomputes_and_lands`.
- 1.4.4 - A locally dirty landed path refuses `checkout_dirty` and a held
  `index.lock` refuses `checkout_busy`, each with no ref change. test:
  `tests/tasks/test_land_commit.py::test_dirty_path_and_index_lock_refuse_without_ref_change`.

### 1.5 Protected-branch guard [category: code] (depends: 1.2, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/cli/installers/git_hooks.py::HOOK_TEMPLATES`
- `src/gobby/install/shared/workflows/rules/worker-safety/block-landing-override.yaml`
- `tests/framing_corpus.py::TRUE_RESTRICTION_RULES`
- `tests/cli/installers/test_git_hooks_installer.py::*` — scope-reason: the expected hook-name set gains reference-transaction
- `tests/cli/installers/test_landing_guard_hook.py`
- `tests/workflows/rules/test_landing_override_rule.py`

Consumers unchanged:
- `tests/workflows/test_rewrite_rules.py` — no-edit-reason: reads the corpus sets, which this deliverable extends.

**Research context:** `HOOK_TEMPLATES` in
`src/gobby/cli/installers/git_hooks.py` maps hook names to the Gobby section
that `install_git_hooks` wraps in GOBBY HOOK markers; `get_stale_git_hooks`
(called from `src/gobby/utils/deps.py`) reports an install missing a template,
so adding a key marks every existing install stale until `gobby install`
refreshes it. `TestHookTemplates::test_all_expected_hooks_defined` pins the key
set. Git calls `reference-transaction` with the state (`prepared`,
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
  `git_hooks.py` from `landing_policy.DIRECT_COMMIT_MARKDOWN_DIRS` so the
  allowance has one source. The POSIX `sh` script:
  - exits 0 unless `$1` is `prepared`, or when `GOBBY_LAND_COMMIT=1`;
  - reads `<git-common-dir>/HEAD`; exits 0 unless it is `ref: <name>`, which
    is the protected ref;
  - for each stdin line naming exactly the protected ref: allows creation
    (zero old), refuses deletion (zero new), and otherwise lists
    `git diff --name-only --no-renames <old> <new>`; any path that is not an
    allowed Markdown path refuses;
  - fails closed when `git diff` fails;
  - refusal text names the path, `gobby-tasks-ops:land_commit`, and the
    operator override `GOBBY_LAND_COMMIT=1`.
  It runs no Python, so commits pay no interpreter start.
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
  `NOTES.md` succeeds, and a commit touching `src/gobby/a.py` or
  `docs/reference-audit/a.json` is refused with the path named. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_direct_commit_allows_only_listed_markdown`.
- 1.5.2 - From a linked worktree, `git merge`, `git reset` and `git branch -f`
  that move the protected branch to code are refused, while commits on the
  lane branch pass. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_protected_branch_refuses_writes_from_any_worktree`.
- 1.5.3 - With `GOBBY_LAND_COMMIT=1`, `land_commit`'s fast-forward passes the
  hook. test:
  `tests/cli/installers/test_landing_guard_hook.py::test_land_commit_passes_installed_guard`.
- 1.5.4 - The installer defines `reference-transaction`, and an install made
  before it reports stale. test:
  `tests/cli/installers/test_git_hooks_installer.py::TestHookTemplates::test_all_expected_hooks_defined`.
- 1.5.5 - An agent Bash command mentioning `GOBBY_LAND_COMMIT` is blocked for
  spawned and interactive sessions. test:
  `tests/workflows/rules/test_landing_override_rule.py::test_landing_override_is_blocked_for_every_session`.

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
  the reviewer flow, path classes, approvals and freeze, moved-tip behavior
  with the post-landing retest, the refusal codes, the direct-commit
  allowance, the guard and its operator override.

Planned verification: `uv run gobby plans validate .gobby/plans/reviewer-landing.md -p /Users/josh/Projects/gobby`
and a read-through against the shipped tool schemas.

**Acceptance:**

- 2.1.1 - The closing reference names both new receipt kinds, their
  authority and the landing-before-close order. behavior: "landing_approval"
  in `src/gobby/install/shared/skills/gobby/references/tasks/closing.md`.
- 2.1.2 - The tasks guide documents `land_commit`, `set_landing_freeze`, the
  path classes, the refusal codes and the guard override. behavior:
  "GOBBY_LAND_COMMIT" in `docs/guides/tasks.md`.

## Rollout
`kind: framing`

1. 1.1 to 1.4 land through the Merge Manager as today; one Orchestrator
   restart activates them.
2. The Orchestrator switches routing: a reviewer's LAND is followed by its own
   `land_commit`. The Merge Manager finishes its in-flight landing tasks and
   its `0.5.0` landing duty retires. The Orchestrator reassigns its other
   duty, merging extra worktrees into a lane's primary worktree; the
   recommendation is the lane reviewer, per Josh's "I don't like developers
   doing merges".
3. 1.5 lands through `land_commit` (restart class, so with a `restart`
   approval). After the restart that syncs the rule, the Orchestrator runs
   `gobby install` from the main checkout so `get_stale_git_hooks` clears and
   the guard is live.
4. At step 2 the Orchestrator edits `.gobby/roles/merge-manager.md`,
   `code-reviewer.md`, `lane-manager.md`, `orchestrator.md` and `_common.md`:
   reviewers land with `land_commit` and run the retest; Lane Managers check
   the `landing` receipt before a close release; the Orchestrator owns
   `set_landing_freeze` and approvals.
5. The close-order rule (memory 283e9a81) keeps "code closes after landing".
   Its spacing of landings between close batches existed for the Merge
   Manager's index commands; whether to keep it is the Orchestrator's call.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05: First draft by the Lane 7 Plan Writer gobby#15429 on #23527,
  rescoped to M3 by Josh's 11:56 CT ruling. Decision 7 is pending Josh.

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
