# Persistent agent workspaces

Research for #24801, an addendum to #24796 (`.gobby/plans/research/devteam-per-epic-2026-10-21.md`). Researcher gobby#17246, 2026-10-21. Code was checked at integration tip `288522f2fc`. Read-only against code. Evidence hashes are the first eight characters of `gcode evidence` `excerpt_hash` values from working-tree range reads.

## Summary

Recommendation: one separate Git repository per project, outside every checkout, committed only by the daemon.

- **Anchor:** `~/.gobby/agents_workspaces/<project_id>/` (from `get_gobby_home()`, so `$GOBBY_HOME` is honored). `<project_id>` comes from the tracked `.gobby/project.json`, which every worktree carries, so the main checkout and all worktrees resolve the same folder.
- **Git dir:** `~/.gobby/agents_workspaces/<project_id>.git`, kept outside the work tree. The work tree has no `.git`, so agent `git` commands there find no repository, and only the daemon commits (it runs `git --git-dir … --work-tree …`).
- **Commits:** the daemon commits on a five-minute timer when the tree is dirty, and at `set_handoff`, pre-compact, session end, clear-successor binding and epic close. One in-process lock per project repo serializes them, so there is no index-lock contention.
- **Rules:** because the anchor sits under Gobby home and outside the project's Git common dir, the task, dirty-path, attribution, close, monolith and commit rules already ignore it. Four rules need a change. A fifth, the TDD gate, needs none if W1-W3 land after v4 deletes it. One new rule enforces Josh's write isolation (§3).
- **Sharing with nodes:** the hub repo is the authority. Nodes sync through Git bundles over the existing authenticated node channel. Build that deliverable when the first seat runs on a node (Decision 3).

The orphan-branch option fails on three counts, covered in §1.

## 1. Storage and durability

| Option | Result |
|---|---|
| In-repo `.gobby/agents_workspaces/`, gitignored | **Rejected.** `.gitignore:234` ignores `.gobby/**`, so the data has no history (Josh's concern). Each worktree has its own `.gobby/`, so a developer's notes vanish with v4 §3.3 teardown, and seats in different trees see different folders. |
| Orphan branch in the project repo, in its own linked worktree | **Rejected.** (a) A linked worktree shares the project's Git common dir (`git rev-parse --git-common-dir` returns `/Users/josh/Projects/gobby/.git` in both the main checkout and `task-23850`). `_is_project_managed_path` treats any path sharing that common dir as project-managed (`_path_scope.py:173-183`, `6baf79ad`; `_shares_git_repository`, `186-190`, `3f123f2f`). Every workspace write would therefore set `canonical_repo_mutation` and engage the task, dirty-path, attribution and close rules, so far more exemptions are needed. (b) Sharing would ride `origin` (`github.com/GobbyAI/gobby`). Project policy requires Josh's exact-SHA approval for every push, which a timer cannot have, and agent notes would reach the project remote's audience. I could not confirm the repo's visibility from this seat (`gh` and `curl` were blocked). (c) The branch's worktree enters Gobby's worktree registry and cleanup paths, and the `block-git-worktree-mutations` rules. |
| PostgreSQL rows | **Rejected as primary.** The hub DB is already shared with nodes, but file tools cannot write to it, and there is no diff or history. |
| **Separate repo under Gobby home** | **Recommended.** It is outside every checkout and has its own common dir, so most rules already skip it (§3). The daemon is the only committer. History is the archive. |

Commit mechanics:
- **Repo init.** Create the Git dir directly (`git init <project_id>.git` with `core.bare=false` and `core.worktree=<work tree>`). Avoid `--separate-git-dir`, which leaves a `.git` pointer file in the work tree, and pass `--git-dir` and `--work-tree` explicitly on every call. With no `.git` in the tree, an agent's `git commit` there fails and cannot race the daemon.
- **Committer.** It reuses `gobby.utils.daemon_git.daemon_git`, the nonblocking Git runner used by worktree checkpoints, and the checkpoint author identity (`worktree_checkpoint.py:39-40`, "Gobby Coordinator").
- **Commit step.** Each commit runs `git add -A` and then commits only when `status --porcelain` shows changes. The message is `agents-ws: <trigger> <changed top-level folders>`.
- **Failure handling.** A commit failure is logged and never blocks the triggering hook. Handoff and compaction must not fail because of workspace bookkeeping. The timeout is 30 s, matching checkpoints.
- **Guards.** The daemon commits only regular files of 1 MiB or less, skips symlinks, and logs whatever it skips. A weekly `git gc --auto` runs from the same lock.

Daemon commit triggers:

| Trigger | Site | What is committed |
|---|---|---|
| Timer, every 5 minutes, only when dirty | a system cron registration following `src/gobby/memory/dream/cron.py` | whole tree |
| `set_handoff` (both `clear_session` values), before the marker is staged | `mcp_proxy/tools/sessions/_terminal.py:314` | whole tree |
| Pre-compact | `hooks/event_handlers/_agent.py:670 handle_pre_compact` | whole tree |
| Session end, including `end_agent_run` and the v4 §3.3 team collapse | `hooks/event_handlers/_session_end.py:15 handle_session_end` | whole tree |
| Clear-successor binding | `sessions/clear_continuation.py`, the take transaction (`458-470`, `fa1abb45`) | whole tree, before `_agent_workspace_dir` is copied to the successor (§4) |
| Epic close | after a successful `close_task` of an epic (`mcp_proxy/tools/tasks/_lifecycle_close_tool.py:27`; the HTTP route broadcasts `task_closed` at `tasks_lifecycle_routes.py:235`) | whole tree, message names `epic-<seq>` |

## 2. Path layout and anchor

```
~/.gobby/agents_workspaces/
  <project_id>.git/                 daemon-only Git dir
  <project_id>/                     work tree, one per project
    README.md                       daemon-written: layout and write rules
    <agent_definition_name>/
      session-<seq>/                one seat lineage: state file, notes, helper scripts
    epics/
      epic-<seq>/                   one epic's DevTeam: decisions, gotchas, interface contracts
```

- `<agent_definition_name>` is the protected `_agent_type`, or `default` for sessions without one (`default.yaml` exists). The Assistant's folder is `assistant/session-15976/`. The Researcher's is `researcher/session-17246/`.
- `<seq>` is the session's sequence number, or the epic's task number.
- Epic folders sit under `epics/` because every team role writes them. This keeps them apart from definition folders such as `epic-reviewer/`.

**Open question 1, anchoring: resolved.** All checkouts share one path because the anchor derives from `<project_id>` and `.gobby/project.json` is tracked (`git ls-files` lists it, and `~/.gobby/worktrees/gobby/task-23850/.gobby/project.json` exists). No path inside a checkout is used, and nothing is symlinked into checkouts. Seats get the literal absolute path through session context (§4).

Agents must name workspace paths literally. A shell operand such as `"$WS/x"` leaves the scope unknown, and unknown scope counts as a repository mutation (`_path_scope.py:80-86`, `aa7b4242`). That triggers `block-unresolved-scope-shell-write` and `require-task-before-edit`. The injected context says so.

## 3. Rules: what the anchor already exempts and what must change

### 3.1 Already exempt (no change)

The anchor is under Gobby home: `_is_known_external_path` → `_is_gobby_home_path` (`_path_scope.py:265-271`, `1dba333d`). It is not in the project's common dir. So `canonical_repo_mutation` is False for literal workspace paths (`_path_scope.py:80-86`, `aa7b4242`). Separately, `find_project_root` looks for `.gobby/project.json` in ancestors (`utils/project_context.py:79-86`, `9a5b2dbe`). `~/.gobby/project.json` does not exist, so `resolve_edit_pair` returns None (`task_claim_state.py:72-79`, `08df63a1`).

| Rule or gate | File | Why it does not fire |
|---|---|---|
| `require-task-before-edit` (write branch) | `rules/task-enforcement/require-task-before-edit.yaml` | It is gated on `canonical_repo_mutation`, which is False. Josh's exemption therefore holds by construction. |
| `block-cross-session-foreign-dirty-edit` | `rules/task-enforcement/block-cross-session-foreign-dirty-edit.yaml`, `commit_guard.py foreign_dirty_edit_conflict` | It only checks paths `resolve_edit_pair` places in a checkout (`commit_guard.py:287-298`, `001d8434`). |
| Edit attribution and close invariant 3 (no uncommitted task-attributed edits), plus today's close gates | `hooks/event_handlers/_tool.py _record_successful_file_mutation` | The recorder skips a path when `resolve_edit_pair` is None (`_tool.py:436-444`, `6c58d58b`), so workspace files never enter a task's edit ledger. |
| `block-unresolved-scope-shell-write` | `rules/task-enforcement/block-unresolved-scope-shell-write.yaml` | It requires `canonical_repo_mutation`. Literal workspace paths do not set it. |
| Monolith ceiling (`require-same-session-decomposition` and its commit and close blocks) | `rules/monolith-enforcement/require-same-session-decomposition.yaml`, `monolith_guard.py` | Projection drops paths outside the project root (`monolith_guard.py:232-238`, `c97c526c`). |
| `require-task-before-commit`, `block-cross-session-foreign-staged-commit`, `no-destructive-git*`, `block-git-worktree-mutations*` | `rules/task-enforcement/`, `rules/worker-safety/` | Agents never commit there: the work tree has no `.git`. Daemon commits are subprocesses outside the hook path. |
| `no-recursive-rm`, `no-truncate` and the rest of `no-destructive-shell` (kept as-is by design) | `rules/worker-safety/no-destructive-shell.yaml`, `no-destructive-shell-interactive.yaml` | These rules are path-agnostic and still apply inside the workspace. Seats delete single files or overwrite them, and Git history keeps everything, so no carve-out is needed. |
| `prefer-gcode-for-source-read` and other read gates | code-index rules | Reads are open (Josh: "No sense in blocking cross reads"), and navigation outside the project is not repository scope. |

### 3.2 Needs a change

| Rule | File | Change |
|---|---|---|
| `researcher-write-scope` | `rules/roles/seat-write-scope.yaml` (rule at line 6) | `write_paths_within` resolves relative dirs against the project root and blocks everything else (`condition_helpers_paths.py:31-52`, `616faf18`). This seat hit that block writing its own scratchpad. Change: allowed dirs become `['.gobby/plans/research/'] + agent_workspace_write_dirs(variables)`. |
| `assistant-write-scope` | same file (line 21) | Allowed dirs become `['docs/'] + agent_workspace_write_dirs(variables)`. |
| `archivist-write-scope` | same file (line 36) | It uses `write_paths_match` on one regex. Change: `not (write_paths_match(...) or write_paths_within(event.data, tool_input, agent_workspace_write_dirs(variables)))`. |
| `block-edits-plan-mode` | `rules/plan-mode/block-edits-plan-mode.yaml`; `plan_write_paths_allowed` in `workflows/enforcement/blocking.py` | External paths fall through to `is_plan_scratch_path` (`blocking.py:575-595`, `38892341`). That allows only the provider user directory and temp roots (`providers/path_policy.py:56`), so a planning seat could not keep notes. Change: also allow `agent_workspace_write_dirs(variables)`. |
| `enforce-tdd-block` | `rules/tdd-enforcement/enforce-tdd-block.yaml`; `_is_tdd_code_path` | It classifies by extension only (`condition_helpers.py:408-415`, `8cdffa12`). Under a TDD-carrying claim, a `.py` or `.sh` helper in the workspace is refused. v4 §1.2 deletes this gate. Recommendation: sequence W1-W3 after v4 1.2, so this rule needs no edit. |
| **New** `agent-workspace-write-scope` | `rules/roles/agent-workspace-write-scope.yaml`, plus a helper in `workflows/condition_helpers_paths.py` | This implements Josh's isolation. It blocks any write path under the anchor root unless it lies within `agent_workspace_write_dirs(variables)`. It also always blocks paths under `<project_id>.git/` and any `.git` or `.gobby` segment inside the work tree (a stray `.gobby/project.json` there would make it a project root). Reads are not checked. |

`metrics-monitor-query-only` (`rules/roles/metrics-monitor-query-only.yaml`) allows only two exact Bash commands. That seat gets no workspace and needs no change; v4 Decision 12 deletes the definition.

**`require-task-before-edit`, script branch.** The rule also fires on `canonical_script_execution`: running a script that is opaque or reads stdin, regardless of where it lives. A helper script in a workspace that only writes literal workspace paths passes, because shell bodies are classified by content. A Python helper still needs a claim, because it can write the repository. Keeping that gate is Decision 2.

**Helper inputs.** `agent_workspace_write_dirs(variables)` returns the absolute session folder and, when set, the epic folder. It reads two daemon-owned variables, `_agent_workspace_dir` and `_agent_workspace_epic_dir`. Native `set_variable` is already refused for every seat except `assistant`, `orchestrator` and `default` (`NATIVE_VARIABLE_ROLES` in `workflows/engine/idle_declaration.py`). W2 also refuses `_agent_workspace_*` keys for those three, so no seat can repoint its own scope.

## 4. Lifecycle

**Open question 2: resolved as below.**

| Event | Session folder | Epic folder |
|---|---|---|
| Session start | The daemon ensures the repo exists and sets `_agent_workspace_dir`. `inject-seat-common` (`rules/roles/inject-seat-common.yaml`) and the non-seat session-start context add one line: the literal path, plus "write literal paths only". The folder is created on first write. | No change. |
| Compaction | Same session row (`docs/contracts/session-boundary.md` §Default Boundary), so the same folder. Pre-compact commit. | No change. |
| `set_handoff` | Commit. The handoff References line gains the folder path automatically. | Commit. |
| Clear successor (marker consumed) | Commit. In the same take transaction that records parentage and moves task claims, the successor inherits `_agent_workspace_dir`, and the predecessor's write access ends. The folder name stays `session-<first seq in the lineage>`, which keeps Josh's "Assistant uses session-15976" stable across clears. See Decision 1. | Inherited the same way. |
| Manual `/clear` (no marker) | Independent session, so a new folder. The old folder stays readable. | Not inherited. |
| Resume (`spawn_agent(resume_session_id=…)`) | Same session, so the same folder, writable again. | Restored only if the team binding still names the seat. |
| Session end (`end_agent_run`, terminal exit, v4 §3.3 collapse) | Final commit. The folder becomes read-only, because no live session holds it in its variables. | Final commit, and the seat's epic variable is cleared. |
| Epic team admission (v4 §3.1, #24796 P7 7.1) | — | The admission sets `_agent_workspace_epic_dir` on each seat, including sub-worktree developers the Dev Manager spawns. |
| Epic close (after the batch leaf closes, #24796 §2 step 8) | — | Final commit tagged `epic-<seq>`. Teardown clears the variable on any surviving seat, so the folder becomes read-only. |
| Leaf detached from an epic (#24796 §4.3) | — | No action. Its notes stay in the epic folder's history. |
| Retention | Folders and history are kept indefinitely, and nothing is deleted. Decision 4. | Same. |

## 5. Relation to the per-epic DevTeam (#24796, memory `ed664ba7`)

- **Coordination surface for parallel developers.** Under Josh's ruling, the Dev Manager runs parallel developers in sub-worktrees off the epic worktree, then merges and cleans them up. Notes kept inside a sub-worktree die at cleanup. Notes inside the epic worktree are blocked by the shared-worktree dirty-path rules when another seat owns them. `epics/epic-<seq>/` sits outside both trees, so the test writer, primary and sub-worktree developers, reviewer and Dev Manager share decisions, gotchas and interface contracts with no dirty-path conflicts and no task attribution.
- **Concurrent writes.** Two seats editing one file can still overwrite each other at the file level. The epic README sets the convention: one file per topic, and appending with Edit instead of rewriting with Write. Each seat's scratch work stays in its own session folder.
- **Persistent primaries.** The primary developers persist until the epic completes, and their session folders carry state across their compactions and handoffs. Short-lived sub-worktree developers leave a committed session folder when they end.
- **No dependence on leaf claims.** Epic access comes from the team record, consistent with #24796 rows 2-3, where leaves are never claimed. The only new P7 coupling is one line in 7.1: `devteam_lifecycle.py` sets and clears `_agent_workspace_epic_dir` for every seat run, including sub-worktree developers.
- **Close order.** The final epic-folder commit follows step 8 of #24796 §2 (batch leaf close, epic close, teardown). Nothing in the workspace is a close invariant, which matches Decision 4.
- **Memory and task records.** Durable cross-session lessons still go to memory, and the Dev Manager's summary still goes to the task (Decision 2: "lessons survive"). The workspace holds working state, and the Dev Manager reads the epic folder when writing those.

## 6. Hub and nodes

Nodes run seats on their own filesystems, against their own checkouts (`LocalProjectCheckoutManager`, used by `_resolve_node_edit_pair` in `task_claim_state.py`). Today the hub reaches node checkouts only through the read-only `checkout.read` command (`hooks/checkout_reads.py`, `crates/gdaemon/src/node_commands/checkout_read.rs`). Nothing transports workspace Git data yet.

Recommended design, deliverable W5:
- The hub's repo is the authority. Each node daemon keeps a clone at the same anchor.
- At each commit trigger, and on the timer, the node sends an incremental `git bundle` to the hub over a new node-channel command, `agent_workspace.sync`. The hub fetches it, merges, and answers with a bundle of commits the node lacks.
- This reuses the channel's existing authentication and pinned TLS. No SSH or external remote is involved.

**Merge rules.**
- Session folders have one writer on one machine, so they never conflict.
- Epic folders can conflict when team seats run on different machines. The daemon merges `*.md` with Git's `union` driver, set by the daemon in `info/attributes`.
- Any other conflict keeps both versions as `<file>.conflict-<machine_id>`, and the Dev Manager is notified.

W5 adds a gdaemon command, so it carries the AGENTS.md crate rules: a version bump and promotion through `promote_workspace_binary_set`. Until W5 exists, a seat running on a node writes a workspace local to that node.

## 7. Deliverables (draft, for the Orchestrator to place)

- **W1: Repo and committer.** Add `src/gobby/agents/agent_workspaces.py`, a new module covering the anchor, repo init, the commit lock, `daemon_git` commits and guards. It is wired into the six §1 triggers and adds a system cron registration. Tests: the anchor is identical from the main checkout and a worktree; a commit runs only when dirty; a commit failure does not fail `set_handoff` or pre-compact; the work tree contains no `.git`.
- **W2: Rules.** Add `agent_workspace_write_dirs` and the violation helper in `condition_helpers_paths.py`; the new `agent-workspace-write-scope` rule; changes to the three seat-scope rules and `plan_write_paths_allowed`; and refusal of `_agent_workspace_*` in native `set_variable`. Tests:
  - Writes to the session's own folder pass for the researcher, assistant, archivist and developer.
  - Writes to another session's folder, to `<project_id>.git`, or to a `.gobby` segment are blocked.
  - Plan-mode writes to the session's own folder pass.
  - A literal workspace write sets no `canonical_repo_mutation` and leaves no task edit-ledger entry (this guards the §3.1 exemptions).
- **W3: Session lifecycle.** Set `_agent_workspace_dir` at session start and inject the context line. Inherit it in the clear take transaction. Add the folder to the handoff References. Tests: compaction keeps the folder; a clear successor inherits the folder and the predecessor loses write access; after a manual `/clear`, the session gets a new folder.
- **W4: Epic folders.** Depends on v4 3.1 and #24796 P7 7.1. Add `devteam_lifecycle.py` set and clear of `_agent_workspace_epic_dir` for all seat runs, including sub-worktree developers, plus the epic-close commit and the epic README. Tests: team seats can write the epic folder and others cannot; after epic close, writes are refused and the final commit exists.
- **W5: Node sync.** §6, built when the first seat runs on a node.

Sequencing: start W1-W3 after v4 1.2 lands (§3.2, TDD row), so nothing overlaps files in Astra's active v4 work. W4 follows P7 7.1.

## Decisions for Josh (through the Assistant)

1. **Folder across a clear handoff.** The successor inherits the predecessor's folder, and its name keeps the first session number (recommended). The alternative gives the successor its own `session-<new seq>` folder, with the daemon running `git mv` on the old one.
2. **Helper scripts.** Workspace writes need no claim. Running an opaque script, such as Python, still needs a claim wherever the script lives, because it can write the repository (recommended). The alternative exempts execution of scripts stored in the seat's own folder, which would let any seat run code that writes the repository without a claim.
3. **Node sync.** Git bundles over the node channel, with the hub as the authority, built when the first seat runs on a node (recommended). The alternative is a separate private Git remote, which would need its own push policy and credentials on every machine.
4. **Retention.** Keep every folder and its history indefinitely (recommended). The alternative prunes working-tree folders some time after session end or epic close, keeping Git history.

## Method

- Read the #24796 report, the v4 runbook (Decision Record, §3.1 and the status section), memory `ed664ba7`, and #24801.
- Read rule templates under `src/gobby/install/shared/workflows/rules/` (roles, task-enforcement, plan-mode, tdd-enforcement, monolith-enforcement, worker-safety).
- Traced the path-scope classifier, edit attribution, the dirty-edit guard, plan-mode allowance, TDD and monolith path checks, native `set_variable` authority, clear-successor binding, node checkout reads and the worktree checkpoint committer with `gcode outline`, `symbol-at` and `grep`.
- Runtime facts checked: `git rev-parse --git-common-dir` in the main checkout and `task-23850`; `git ls-files .gobby/project.json`; `.gitignore` lines 234-242; the contents of `~/.gobby` (no `project.json`; `worktrees/` holds task trees).

Not verified:
- Workspace writes were not exercised end to end. This seat's own write scope blocks every path outside `.gobby/plans/research/`. W2's tests cover it.
- v4 modules that do not exist yet (`devteam_lifecycle.py`) are taken from the plan text.
