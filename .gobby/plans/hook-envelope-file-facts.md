Plan artifact: `.gobby/plans/hook-envelope-file-facts.md`

# Hook rules read a node's checkout through the node

**Plan ID:** hook-envelope-file-facts

## Overview
`kind: framing`

This plan specifies #23277 (Hook envelope carries edited-file content for
disk-reading rules). The hook envelope, the hook-ingress contract
(`docs/contracts/hook-ingress.md`) and ghook do not change. The obligation is
met by a read-only node command: when a hook comes from a node, the hub asks
that node for the file and git facts its rules need.

**The obligation.** #23277 is deferred section D5 of
`.gobby/plans/completed/gdaemon-api-keys-nodes.md` and section D3 of
`.gobby/plans/completed/gdaemon-front-door.md`: "Rules that read the edited
file from disk cannot do so on a hub when the hook came from a node; the
envelope must carry the fact. Which rules do this is unverified." The card's
criterion "D1.7: D1.7" is a deferral artifact. D1.7 of the api-keys plan is
its e2e corpus item 4.3.7, which is unrelated.

**The problem.** Rule evaluation runs in the hub's Python daemon. While it
handles a hook, it reads the agent's working tree and runs git on the
evaluating machine (§Site Disposition). A hook that a node forwards names
paths on the node. The hub reads its own disk at those paths: a missing file,
or the hub's own checkout of the same project, which can be stale. Rules then
return wrong answers without failing.

**When the leaves close:**
- Node-commands contract version 4 adds `checkout.read`. One command answers
  either a file-facts query or one allow-listed read-only git invocation,
  confined to the project checkout that contains the event's cwd on that node.
- The node answers it (1.2).
- The hub knows each delivery's origin. One seam,
  `src/gobby/hooks/checkout_reads.py`, serves every node read. Every hook-path
  git call runs on the node for a node event.
- Each hub site that reads disk keeps its local code unchanged. For a node
  event it reads the node's answer, or it takes its stated disposition.
- An unanswered node fails closed with the diagnostic
  `checkout_read_unavailable: <code> (machine <id>)`.
- A live hub and node pair proves the path end to end (3.1).

## Decision Record
`kind: framing`

Orchestrator gobby#15977 ruled on decision N1 on 2026-10-14, choosing
option (1):

> One read-only node command over the hub-mode node-command channel: file
> facts plus the fixed allow-list of read-only git queries, confined to that
> node's registered checkouts and workspaces for the requesting project. Hub
> sites call one seam. Local events read as today; a node event goes to the
> node; an unanswered node fails closed with a named diagnostic. No
> hook-envelope or hook-ingress contract change and no ghook change.

Option (2), envelope `file_facts` plus a node git command, was rejected. It
needs two mechanisms when the git command is required anyway. Option (3),
envelope facts only with the git gates failing closed, was rejected because
node commits could then never pass a git gate. The ruling's no-decision
items are decisions 10, 11 and 12 below.

Option (4), node-local rule evaluation against a cached ruleset, was
rejected. Hub and node are the same gdaemon binary (ROADMAP decision 13),
and after #24151 ports the rule engine to Rust a node carries the engine. A
node could cache the definitions, which change rarely, under hub-pushed
invalidation, as it fetches detection manifests (node-halves Decision 27);
that does not change this decision. The state rules read and write on each
call cannot be cached that way. Session variables, task claims, workflow
step state and live session path owners are read from the hub database on
every hook and written back there, and hub-side actors write them too:
`claim_task` and `close_task` over MCP, reviewers and step transitions. A
node copy would lag those writes inside a hook's decision window, and
counters and flags written from two places would lose updates, unless every
hook first confirmed its copy with the hub. That confirmation is the round
trip node-local evaluation was meant to save, and the hook reaches the hub
anyway, because the node's envelope ledger forwards every envelope (ROADMAP
decision 17). Decision 13 also makes a lost hub a typed error, so
node-local evaluation gains no offline enforcement. Evaluation stays on the
hub before and after #24151, and the hub reads the node's checkout through
`checkout.read`.

1. **The old draft is withdrawn.** It is the draft stamped at `74f5b4ec32`
   and never approved. Gone with it are the envelope `file_facts` field,
   foreign-origin fail-closed as the end state, deferred sections D1 to D3,
   and the "D1.7 Mapping" and "Persistence Inventory" sections. Its 1.2
   (owner-only hook inbox) closed as #23538. Its 1.5 (MultiEdit monolith
   projection) closed as #23537.
2. **Node origin.** A delivery is node-origin when the payload's
   `input_data["machine_id"]` names a machine other than
   `src/gobby/utils/machine_id.py::get_machine_id()`. hub-mode 2.2 (#24272)
   makes that field trustworthy. It refuses a key-authenticated hook whose
   envelope names a machine other than the key's, and an unauthenticated
   hook that names any machine but the hub's. A missing machine id is local.
   `run_adapter_hook` sets the origin around the adapter call. Rule
   evaluation and every thread or loop it hops to inherit the origin, because
   `asyncio.run_coroutine_threadsafe` and the engine's offload copy the
   calling context. `src/gobby/hooks/receipt_effects.py` already relies on
   this for the staging buffer.
3. **Contract version 4.** `docs/contracts/node-commands.md` says: "A new
   command or field bumps `v`." The ruling's "node-commands.md v3 amendment"
   therefore ships as version 4. hub-mode 4.1 owns version 3 (workspace and
   checkout commands), and `checkout.read` lands after it in the same files
   (Cross-plan edges).
4. **Confinement is computed on the node from the event cwd.** This corrects
   writer message `0c04009c`, which had the hub name the confinement roots.
   Adapters normalize tool paths during `normalize_tool_fields` (for example
   `src/gobby/adapters/claude_code.py`, line 260). That runs before
   `HookManager` resolves the project, so at normalization time the hub has
   no project or checkout row to name. The node computes:
   - **R0**, the nearest ancestor of the canonical cwd that holds
     `.gobby/project.json`. Its `id` is the project. With no such ancestor,
     the node answers `no_project_root`.
   - **Admission.** The node classifies a path's canonical form with one
     predicate, in this order. It copies
     `src/gobby/hooks/_path_scope.py::_is_project_managed_path` with R0 as the
     project root, including the rejecting branch between its two accepting
     ones:
     1. A path under R0 is admitted.
     2. A temp agent scratchpad is outside. That is a path under one of the
        node's temp roots that has a scratchpad marker part, using the
        markers and prefixes of `_SCRATCHPAD_MARKERS` and
        `_SCRATCHPAD_PREFIXES` in the same file. The node's temp roots are
        its canonical `std::env::temp_dir()`, `/tmp` and `/private/tmp`, as
        `_temp_scratchpad_roots` lists them for the hub.
     3. A path whose nearest checkout shares R0's git common dir is admitted.
        This includes a symlink or a `..` path that resolves there.
     4. Every other path is outside.
   - **Home exclusion.** After that predicate, a path under the node's gobby
     home is outside, whatever steps 1 to 3 said, unless it is under
     `<home>/worktrees/` or `<home>/clones/`.
   - **Outside paths.** The node returns no content or git result for an
     outside path.

   Workspaces carry a copied marker, so a session running in a node workspace
   has its workspace as R0. The hub-mode 4.2 and node-halves 2.3 edges
   proposed in `0c04009c` are therefore not needed (Cross-plan edges,
   "Relations that set no edge").
5. **One command, two queries.** The `checkout.read` payload is
   `{cwd, query}`. `query` takes one of two forms:
   - `{kind: "files", paths: [str], read_limit: int|null}`, with at most 256
     paths and `read_limit` from 0 to 8,388,608 bytes;
   - `{kind: "git", args: [str], stdin: str|null, timeout_ms: int}`, with
     `stdin` in base64 and `timeout_ms` from 1 to 60,000.

   The reply is `{root, project_id, home, temp_dir, files, git}`:
   - `root` is R0, and `project_id` is its marker id.
   - `home` and `temp_dir` are the node's canonical home and temporary
     directories. Hub path policies need them for node-scoped roots (2.2).
   - `files` is a list of entries or null, and `git` is a result or null.

   Each `files` entry is
   `{path, resolved, kind, checkout_root, git_common_dir, project_root, project_id, data, truncated}`:
   - `resolved` is the node's `Path.resolve(strict=False)` of the path.
     `~` expands with the node's home, and a relative path is joined to
     `cwd`. `resolved` is never null.
   - `kind` is `file`, `dir`, `other`, `missing` or `outside`.
   - For an admitted path, `checkout_root` is the nearest directory holding
     `.git`. `git_common_dir` is that checkout's common dir, computed as
     `src/gobby/hooks/_path_scope.py::_git_common_dir` does. `project_root`
     and `project_id` are the nearest marker and its id.
   - `data` is the first `read_limit` bytes of a `file` in base64. It is null
     when `read_limit` is null, or when the entry is not a `file`.
   - `truncated` is true when the file is longer than `data`.
   - An `outside` entry carries only `path`, `resolved` and `kind`. Its other
     fields are null and `truncated` is false.

   The `git` result is
   `{exit_code: int|null, timed_out, stdout, stderr, truncated}`. `stdout`
   is base64 and at most 8 MiB. `stderr` is UTF-8 text with replacement and
   at most 64 KiB. `truncated` is true when stdout was cut.

   The whole reply carries at most 8 MiB of `data`. Entries fill in request
   order, and an entry cut by the budget has `truncated: true`. Every
   nullable field is required, which is hub-mode 4.1's strictness rule.

   The node answers each of these errors with `stage: "none"`:
   - `invalid_payload`: a malformed query, more than 256 paths, a refused git
     argument, or a `cwd` that is not a directory;
   - `no_project_root`: no marker above `cwd`;
   - `outside`: a git `-C`, `--git-dir` or `--work-tree` path that is not
     admitted.
6. **Git allow-list.** The node runs `git` only with:
   - global options `-c core.fsmonitor=false`, `--literal-pathspecs`,
     `--no-optional-locks` and `--no-pager`, plus `-C`, `--git-dir` and
     `--work-tree` when they name an admitted path;
   - then one subcommand from `rev-parse`, `diff`, `ls-files`, `ls-tree`,
     `status`, `log`, `check-ignore`, `merge-base` and `rev-list`.

   It refuses these options anywhere in the arguments: `--output` and
   `--output=*`, `--no-index`, `--ext-diff`, `--textconv`, `-O*`,
   `--orderfile*`, `--exclude-from*` and `--open-files-in-pager`.

   The node removes every `GIT_*` variable from git's environment and sets
   `GIT_OPTIONAL_LOCKS=0`. It kills git at `timeout_ms`.

   These are exactly the invocations the hub's hook paths make today (sweep
   in 1.4). Repository configuration such as filters and fsmonitor behaves
   on the node as it does in the hub's local git today, so the node gains no
   execution surface the hub's local git lacks.

   A node-origin git call outside the allow-list fails like any other git
   failure, through the caller's existing branch.
7. **The seam.** `src/gobby/hooks/checkout_reads.py` holds every node read:
   - `CheckoutReadUnavailable(OSError)` carries `code` and `machine_id`. Its
     message is `checkout_read_unavailable: <code> (machine <id>)`. Because
     it is an `OSError`, a site's existing `OSError` branch applies unless
     the site states otherwise.
   - `delivery_scope(machine_id, loop, cwd)` is a context manager that sets
     one ContextVar, holding the node machine (or none), the daemon loop, the
     delivery cwd and the memo.
   - `node_origin()` reports whether the current delivery is node-origin.
     `event_machine_id()` returns the delivery's machine, or
     `get_machine_id()` outside a node delivery.
   - `path_facts(paths, *, cwd=None, read_limit=None)` is called only on
     node-origin branches; a local call raises `RuntimeError`. It sends one
     `checkout.read` files query for the paths the delivery memo lacks, then
     answers from the memo. The memo key is `(cwd, read_limit, path)`. The
     default `cwd` is the delivery cwd. The same call with no paths returns
     `root`, `project_id`, `home` and `temp_dir`.
   - `run_git(args, *, cwd, timeout, input_bytes=None)` is synchronous. For a
     local delivery it is the existing
     `spawn.run(["git", "-C", cwd, *args], ...)`. For a node delivery it
     sends a git query and returns a `subprocess.CompletedProcess[bytes]`. A
     node timeout raises `subprocess.TimeoutExpired`. Truncation raises
     `CheckoutReadUnavailable` with code `truncated`.
   - `run_git_async` is the awaitable form that `DaemonGitService._execute`
     uses (1.4).

   Node calls run hub-mode 1.3's `node_command_client().send` on the daemon
   loop captured by `delivery_scope`:
   - from a worker thread, through `asyncio.run_coroutine_threadsafe`;
   - from another loop, by awaiting `asyncio.wrap_future` of that future;
   - on the daemon loop, directly.

   A synchronous call on the daemon loop's own thread raises unavailable
   code `loop_thread` instead of deadlocking.
8. **Latch and time budget.** In one delivery, the first
   `CheckoutReadUnavailable` latches: every later node read in that delivery
   raises it at once, without sending. The per-request node answers
   (`invalid_payload`, `no_project_root`, `outside`) and truncation do not
   latch.
   - A files query sends `timeout_ms` 5,000. hub-mode 1.3's client waits
     that plus 5 seconds when a reply is lost, so an unreachable node costs
     one delivery at most one wait of 10 seconds.
   - A git query sends the caller's timeout, so a slow node git costs what a
     slow local git costs today.
   - Both bounds sit inside the existing ordering
     `workflow.timeout` (15 s) < `hooks.adapter_timeout` (26 s), which
     `src/gobby/config/app.py` enforces and which already bounds local git.
9. **Content stays in the delivery.** File bytes and git output from a node
   live only in the delivery memo. They never enter `HookEvent.data` or
   `HookEvent.metadata`, a log record, the database, session variables or
   the broadcaster.
10. **Node project from registered rows** (ruling). For a node event with no
    explicit or session project, the hub takes the project from
    `LocalProjectCheckoutManager(db).list_for_machine(machine_id)`. It uses
    the longest `root_path` that equals the cwd or encloses it component by
    component. The hub never walks its own disk for a marker, and never
    registers a checkout for another machine (memory 7342e47e:
    `register_cwd_marker_checkout` uses `require_local_machine_id(None)`).
    This refines hub-mode 2.2.7, whose remote event "takes its project from
    its cwd": the cwd now matches the node's rows (2.1).
11. **Machine-scoped ledger owners** (ruling). A hub checkout and a node
    checkout can share an absolute path. `commit_guard._live_path_owners`
    therefore counts only sessions on the event's machine.
12. **#24151 inherits the seam** (ruling). #24151 (S2.11 hub half: hook
    ingress and rule engine in Rust) ports these node reads and dispositions.
    MCP tool calls that a node session makes are not hook deliveries. Their
    seam is unset, so they run locally, and hub-mode's route families own
    them.
13. **Local events read as today.** Every site keeps its local code and adds
    only a `node_origin()` branch. There is one exception, a found bug: for a
    relative path, `_positional_checkout_is_path` tested
    `Path(path).exists()` against the daemon's process cwd instead of the
    command's cwd. 1.4 fixes it for both origins and pins it (1.4.5).
14. **Hub-only effects skip node events.** These steps act on the hub's
    machine, so a node event skips them:
    - `handle_worktree_create` would create a git worktree on the hub, and
      `handle_worktree_remove` could delete one. Both answer allow before any
      hub resolution or effect, which leaves the provider's default
      behavior.
    - The hub's gcode index notification is skipped; node-halves 2.11 runs
      gcode on the node.
    - The rtk rewrite and the impeccable `run_command` spawn are skipped.
    - Provider spill and transcript files live in the provider's home on the
      node, outside confinement. A node event does not read them and takes
      each reader's existing unreadable-file branch.
15. **Found-work terminal evidence is unchanged.**
    `FoundWorkStopAnalyzer._terminal_failures` returns `()` for a node-owned
    session. `derive_transcript_evidence` raises `RemoteSessionOwnershipError`
    through `require_local_session_ownership`, and the analyzer catches it.
    The transcript is session ingestion, not the working tree. hub-mode
    Decision 4 keeps no transcript snapshot for another machine. The
    disposition was escalated to gobby#15977 with the other findings, and 2.8
    pins it with a test.
16. **Live proof, no deferral.** Policy memory 878d0c35 rules out deferral.
    3.1 extends hub-mode 5.1's live pair. It proves that a node-origin rule
    reads the node's answer, and that a stopped node fails closed even though
    the same path is readable on the hub's disk.
17. **Security review gate.** 1.2 is the first node command that returns
    file content and runs git for the hub. Its LAND review needs a Security
    Reviewer verdict on confinement, the allow-list and the environment
    scrub, in addition to the code review.

## Constraints
`kind: framing`

- Use `uv run` for every Python operation. Test runs use
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1`.
- Rust work loads the `rust` skill and follows `crates/CLAUDE.md`. Use
  `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon ...`. The shipping
  commit bumps `gdaemon` by one patch version and updates `Cargo.lock`.
  Installs go through `promote_workspace_binary_set`.
- No agent or test reads `~/.gobby/bootstrap.yaml`. A test that pins the
  bootstrap file as outside builds an isolated node home under `tmp_path`.
- Tests build node trees under `tmp_path`, and fake nodes answer through
  `tests/hooks/checkout_read_fakes.py` (1.3). Nothing touches the user's
  daemon, home or database.
- There is no backward compatibility, because 0.5.0 has not shipped.
- The monolith ceiling holds: `src/gobby/workflows/hooks.py` (923 lines) is
  split in 2.1. `src/gobby/workflows/found_work_gate.py` (882 lines) gets no
  production edit.
- **Cross-plan edges.** The coverage grammar has no cross-plan dependency.
  "Cross-plan edges set at expansion" lists every edge; the Orchestrator sets
  them at expansion.

## Cross-plan edges set at expansion
`kind: framing`

Each row is one edge between a leaf of this plan and work outside it:
- "blocked by": the external work closes before the leaf starts.
- "blocks": the leaf closes before the external work starts.

hub-mode leaves are numbered as in `.gobby/plans/hub-mode.md`, and
node-halves leaves as in `.gobby/plans/node-halves.md`, each with the task
that holds it today. Edges between this plan's own leaves are heading
dependencies and reach the manifest.

| Leaf | External work | Edge | Reason |
| --- | --- | --- | --- |
| 1.1 | hub-mode 4.1 (Node-commands contract version 3: workspace and checkout commands; no task yet) | blocked by | 1.1 bumps the same contract to version 4 in the same files. Its parity tests extend the `node_command_parity` files that 4.1 creates. |
| 1.2 | hub-mode 4.6 (Node checkout.status handler; no task yet) | blocked by | 1.2 adds a handler to `crates/gdaemon/src/nodes/agents/commands.rs` and `crates/gdaemon/src/nodes/agents/mod.rs`. 4.6 is hub-mode's last handler leaf in those files, and it follows node-halves 2.15, the last node agent leaf. |
| 1.3 | hub-mode 1.2 (gdaemon internal node-command route over the node channel; no task yet, #24281 holds it) | blocked by | Every node read the seam sends travels that route. |
| 1.3 | hub-mode 1.3 (#24269 Python node-command client and machine eligibility) | blocked by | The seam awaits `node_command_client().send` and maps `NodeCommandError`. |
| 1.3 | hub-mode 2.2 (#24272 Hook ingress binds the envelope machine to the caller's key) | blocked by | Node origin reads `input_data["machine_id"]`, which is trustworthy only once 2.2 binds it (Decision 2). 2.1 also refines 2.2.7 and follows it through 1.3. |
| 2.1 | hub-mode 1.4 (#24270 Node routes: checkout registration and run reports) | blocked by | 2.1 reads a node's `project_checkouts` rows. Only 1.4's `POST /api/nodes/checkouts` writes them. |
| 3.1 | hub-mode 5.1 (Live hub-to-node proof with isolated daemons and fake providers; no task yet, #24283 holds it) | blocked by | 3.1 runs on the live pair that 5.1 proves, with its node checkout registration and hook delivery. |
| Every leaf from 1.1 to 1.4 and 2.1 to 2.8 | #24151 (S2.11 hub half: hook ingress and rule engine in Rust) | blocks | #24151 ports the seam's node reads and each site's disposition (Decision 12). |

Relations that set no edge:
- **node-halves 2.1 (Durable node agent state) and node-halves 2.7 (Command
  dispatch over the node channel).** 1.2 declares its module in
  `crates/gdaemon/src/nodes/agents/mod.rs`, which node-halves 2.1 creates,
  and dispatches in `commands.rs`, which node-halves 2.7 creates. 1.2 already
  follows both through hub-mode 4.6, which follows node-halves 2.15.
- **hub-mode 4.2 (Node workspace.merge and workspace.remove handlers) and
  node-halves 2.3 (workspace paths).** Writer message `0c04009c` proposed
  them for hub-named workspace confinement. Confinement is now derived on the
  node from the cwd marker, and a workspace carries a copied marker
  (Decision 4). 1.2 still follows 4.2 through 4.6.
- **#24368 (Node hook ingress arm).** It forwards hub bytes unchanged
  (node-halves 1.5). This plan changes no envelope, so the arm needs no
  change.
- **hub-mode and node-halves own tables.** They list their own edges with
  this plan only once the Orchestrator adds them. The rows above are the full
  set.

## Site Disposition
`kind: framing`

Dispositions apply to **node-origin** events. Local events are unchanged,
except for the found bug in row 5 (Decision 13).

- **`node-read`**: the site reads the node's answer through the seam.
- **`fail-closed`**: the gate blocks, and its reason carries
  `checkout_read_unavailable: <code> (machine <id>)`.
- **`degrade-existing`**: the site takes its existing no-answer branch, with
  the cited outcome.
- **`skip`**: a hub-only step or effect does not run for the event.
- **`no-change`**: the site needs no edit, for the stated reason.

The "Unanswered" column gives the outcome when the node does not answer.
For a site that lets `CheckoutReadUnavailable` propagate,
`src/gobby/servers/routes/mcp/hook_responses.py::_hook_exception_response`
blocks a critical hook with the diagnostic in its reason and degrades any
other hook.

| # | Site (`0.5.0`) | Trigger | Node event | Unanswered | Leaf |
| --- | --- | --- | --- | --- | --- |
| 1 | `src/gobby/hooks/adapter_execution.py::run_adapter_hook` | every hook delivery | sets the delivery origin | — | 1.3 |
| 2 | `src/gobby/utils/daemon_git.py::DaemonGitService._execute` and `status`, reached from `commit_guard` (`foreign_dirty_edit_conflict`, `foreign_staged_commit_conflict`), `code_review_scope.inspect_commit_review_scope`, `ledger_reconcile.reconcile_edit_ledgers`, `task_dirty_state`, `resolve_git_worktree_root_async` and `paths_committed_after_async` | commit, edit, review and ledger gates; worktree root | `node-read` | `fail-closed` through each caller's git-failure branch: `commit_guard` blocks naming the detail, the review gate stays armed, the ledger releases nothing, the worktree root stays unresolved, and the landed check keeps the attribution | 1.4 |
| 3 | `src/gobby/workflows/tdd_paths.py::_git_out` (`tdd_path_identity`, `same_repo_worktree_root`, via `condition_helpers.tdd_gate_open`) | `enforce-tdd-block` | `node-read` | `degrade-existing`: the `_GitLookupFailed` string-identity fallback | 1.4 |
| 4 | `src/gobby/hooks/code_navigation_recovery.py::_git_ignored` | `prefer-gcode-for-source-read` | `node-read` | `degrade-existing`: not ignored, so enforcement stays on | 1.4 |
| 5 | `src/gobby/hooks/_normalization_git.py::_positional_checkout_is_path` | `git checkout` classification | `node-read` | `degrade-existing`: unknown (`None`) | 1.4 |
| 6 | `src/gobby/hooks/project_context.py::resolve_hook_project_context` | hook ingress project resolution | `node-read` from the node's checkout rows (database only) | no row: `fail-closed` (the no-project `ValueError`) | 2.1 |
| 7 | `src/gobby/workflows/hooks.py::WorkflowHookHandler._resolve_project_path` | rule `project_path` | the node's checkout row, never a hub path resolve | — | 2.1 |
| 8 | `src/gobby/hooks/_path_scope.py` primitives and `_normalization_git.py::classify_git_mutation` | normalization of write, navigation and git events | `node-read` | `fail-closed` by propagation | 2.2 |
| 9 | `src/gobby/workflows/condition_helpers_paths.py::write_paths_within` | `seat-write-scope` | `node-read` | `fail-closed`: `False`, so the write is outside scope | 2.2 |
| 10 | `src/gobby/workflows/enforcement/blocking.py::plan_write_paths_allowed` | plan-mode write gate | `node-read`, with the node's home and temp roots | `fail-closed`: `False` | 2.2 |
| 11 | `src/gobby/workflows/monolith_guard.py::_projection_for_path` via `projected_monolith_paths` | `require-decompose-monolith-before-threshold-write` | `node-read` | `fail-closed`: the path is listed with the diagnostic | 2.3 |
| 12 | `src/gobby/workflows/monolith_guard.py::outstanding_monolith_paths` | `require-monolith-resolution-before-commit`, `-before-task-transition`, `-before-turn-end` | `node-read`, batched | `fail-closed`: each guard path is listed with the diagnostic | 2.3 |
| 13 | `src/gobby/workflows/rust_test_evidence.py::_read_absolute_text` via `rust_edit_is_test_writing` | `enforce-tdd-block`, `enforce-tdd-track-tests` | `node-read` | `fail-closed`: not test-only | 2.4 |
| 14 | `src/gobby/hooks/code_navigation_recovery.py::_verified_source_line_count`, `_ignore_decision` | `prefer-gcode-for-source-read`, `navigation_requires_index` | `node-read` | `degrade-existing`: the line count is unverified, so the read counts as broad; a missing gcode config counts as empty | 2.5 |
| 15 | `src/gobby/workflows/task_claim_state.py::resolve_edit_pair` (`_tool` before- and after-tool, `commit_guard`) | edit attribution and edit-path selection | `node-read` | `degrade-existing`: the lexical path under the node's checkout row | 2.6 |
| 16 | `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._notify_code_index` | after-tool edits | `skip`: node-halves 2.11 runs gcode on the node | — | 2.6 |
| 17 | `src/gobby/workflows/commit_guard.py::_live_path_owners` | commit and edit ownership gates | owners on the event's machine only | — | 2.6 |
| 18 | `src/gobby/hooks/shell_execution.py::path_execution`, `read_script_body`; `src/gobby/hooks/_normalization_operands.py::_file_transfer_paths` | shell classification | `node-read` | `fail-closed` by propagation | 2.7 |
| 19 | `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_worktree_create`, `handle_worktree_remove` | `WorktreeCreate`, `WorktreeRemove` | `skip`: answer allow before any hub effect | — | 2.8 |
| 20 | `src/gobby/adapters/claude_code.py::_saved_mcp_result`, `src/gobby/adapters/grok.py::_spilled_mcp_output`, `src/gobby/adapters/codex_impl/hooks_adapter.py::_command_execution_result` | provider spill and transcript files | `skip`: no read, take the unreadable-file branch | — | 2.8 |
| 21 | `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy` | `rtk-command-rewrite` | `skip`: the command runs as written | — | 2.8 |
| 22 | `src/gobby/workflows/engine/run_command_effects.py::RunCommandEffectsMixin._apply_run_command` | `impeccable/design-detector` | `skip`: advisory, documented fail-open | — | 2.8 |
| 23 | `src/gobby/workflows/found_work_gate.py::FoundWorkStopAnalyzer._terminal_failures` | `enforce-found-work-ladder` | `no-change`, pinned (Decision 15) | — | 2.8 |
| 24 | `src/gobby/config/validation_detection.py::load_project_validation_detection` | found-work validation config | `no-change`: its only hook-path caller is row 23, which discards the result for a node session | — | 2.8 |
| 25 | `src/gobby/hooks/startup_claim_preflight.py::_resolve_or_register_session` | AGY startup claim | `no-change`: `require_local_machine_id` refuses a node envelope before any disk walk | — | — |
| 26 | `src/gobby/workflows/observer_plan_mode.py` Codex transcript read | plan-mode observer | `no-change`: a transcript is not the working tree | — | — |

A finding outside this plan was escalated to gobby#15977.
`src/gobby/hooks/terminal_context.py::_is_fresh_seat_tui` inspects the
hub's processes with psutil for a node event. That is session identity, in
hub-mode 2.2's area, and not a checkout read.

## P1: Node checkout reads
`kind: framing`

**Goal**: the hub can ask a node for confined file facts and read-only git
answers. It knows which deliveries need that, and every hook-path git call
from a node delivery runs on the node.

### 1.1 Node-commands contract version 4: `checkout.read` [category: code]
`kind: deliverable`

Targets:
- `docs/contracts/node-commands.md`
- `crates/gdaemon/src/node_commands.rs::*` — scope-reason: sets `CONTRACT_VERSION` to 4 and adds the `checkout.read` payload, query and reply types
- `src/gobby/hub/node_commands.py::*` — scope-reason: the same models on the Python side, plus their `_PARSERS` entries
- `crates/gdaemon/tests/fixtures/node_commands/corpus.json::*` — scope-reason: every message changes to `v: 4`, `checkout.read` gains command and reply messages, and the `invalid` list gains entries
- `crates/gdaemon/tests/node_command_parity.rs`
- `tests/hub/test_node_command_parity.py`

**Research context:**
- hub-mode 4.1 sets `CONTRACT_VERSION` to 3 in both models. It adds
  `strict=True` to the Python base model and
  `#[serde(deserialize_with = "Option::deserialize")]` to every Rust `Option`,
  so an omitted nullable field fails on both sides. It also adds the corpus
  `invalid` list and creates both `node_command_parity` test files. Every
  handler and route refuses another `v`.
- The corpus is `{"messages": [{name, kind, json}], "invalid": [...]}`.
  Python's `parse_message(kind, raw)` dispatches through `_PARSERS`.
- The contract states: "A new command or field bumps `v`." Its error tables
  list which codes the node produces (Decision 5's three).

**Implementation:**
- Both sides set `CONTRACT_VERSION = 4`, and every corpus message carries
  `v: 4`. The refusal rule for another `v` is unchanged.
- Add `checkout.read`, hub to node, with Decision 5's payload, query, reply,
  entry and git-result shapes and limits. The query is a tagged union on
  `kind`, and a query that mixes the two forms' fields is invalid.
- The contract gains a section "v4 commands", with a `checkout.read`
  subsection giving the payload, reply, limits and stage rule.
- "Node duties" gains the node rules:
  - Decision 4's confinement;
  - Decision 6's allow-list and environment;
  - the 8 MiB data budget and fill order;
  - `checkout.read` is read-only, so every error carries `stage: "none"`.
- The node error table gains `no_project_root` and `outside`.
- `corpus.json` gains a files command, a git command, a reply for each, an
  error reply for each error, and these `invalid` entries:
  - a query with an unknown `kind`;
  - a files query carrying `args`;
  - a files query carrying `stdin`;
  - a files query carrying `timeout_ms`;
  - a git query carrying `paths`;
  - a git query carrying `read_limit`;
  - 257 paths;
  - an omitted `read_limit`;
  - an omitted `stdin`;
  - a `v` of 3.

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus) + binary(node_command_parity)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_node_command_corpus.py tests/hub/test_node_command_parity.py -v`.

**Acceptance:**

- 1.1.1 - `CONTRACT_VERSION` is 4 in the Rust models. Each corpus `checkout.read` command and reply parses into its v4 Rust type, with `v` equal to `CONTRACT_VERSION`, and re-encodes to the same bytes. test: `crates/gdaemon/tests/node_command_parity.rs::v4_checkout_read_round_trips`.
- 1.1.2 - `CONTRACT_VERSION` is 4 in the Python models, and the same messages parse into their v4 Python models and re-encode to the same bytes. test: `tests/hub/test_node_command_parity.py::test_v4_checkout_read_round_trips`.
- 1.1.3 - Both sides refuse every new `invalid` entry. test: `crates/gdaemon/tests/node_command_parity.rs::invalid_messages_fail`.
- 1.1.4 - The Python models refuse every new `invalid` entry. test: `tests/hub/test_node_command_parity.py::test_invalid_messages_fail`.
- 1.1.5 - The contract specifies `checkout.read` with its payload, reply, limits, node rules, error codes and stage rule. file: `docs/contracts/node-commands.md`, section "v4 commands", subsection `checkout.read`, and "Node duties".

### 1.2 Node `checkout.read` handler [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gdaemon/src/nodes/agents/checkout_read.rs`
- `crates/gdaemon/src/nodes/agents/commands.rs`
- `crates/gdaemon/src/nodes/agents/mod.rs`
- `crates/gdaemon/tests/node_checkout_read.rs`
- `crates/gdaemon/tests/fixtures/node_commands/checkout_read_facts.json`
- `tests/hub/test_checkout_read_facts_golden.py`

**Granularity:** six acceptance items, one outcome. The handler answers
confined reads, and every item exercises the same handler against one
fixture tree.

**Research context:**
- node-halves 2.1 creates `crates/gdaemon/src/nodes/agents/mod.rs`, and
  node-halves 2.7 creates `commands.rs` with `dispatch`. hub-mode 4.2 and 4.6
  add their handlers there. 4.6's `checkout.status` is dispatched outside the
  per-run queues and answers with `stage: "none"`; 1.2 follows that pattern.
- A node workspace lives at `<home>/worktrees/<project_id>/<run_id>` or
  `<home>/clones/<project_id>/<run_id>` (node-halves 2.3), and carries a
  copied `.gobby/project.json`.
- The hub's local oracle for each fact:
  - `resolved` is `Path(...).expanduser().resolve(strict=False)`;
  - `checkout_root` is `src/gobby/hooks/_path_scope.py::checkout_root`;
  - `git_common_dir` is `src/gobby/hooks/_path_scope.py::_git_common_dir`;
  - `project_root` is `src/gobby/utils/project_context.py::find_project_root`;
  - admission is `src/gobby/hooks/_path_scope.py::_is_project_managed_path`
    with R0 as the project root. Its branch order is: under the root, accept;
    temp agent scratchpad (`_is_temp_agent_scratchpad_path`), reject; shared
    git common dir (`_shares_git_repository`), accept. Decision 4's
    predicate copies that order.

**Implementation:**
- `dispatch` sends `checkout.read` to `checkout_read::read` outside the
  per-run queues.
- `read` canonicalizes `cwd` and finds R0 and its marker id
  (`no_project_root` when absent). It then answers the files or git query
  under Decisions 4 to 6.
- `dispatch` passes `read` the node's gobby home and temp roots: the home the
  node daemon runs with, and Decision 4's three temp roots. Tests pass an
  isolated home and the single temp root `<fixture>/tmp`.
- Files: each path is resolved, classified by Decision 4's predicate and home
  exclusion and, when admitted, described. A
  `file` is read up to `read_limit` within the 8 MiB reply budget.
- Git: the arguments are checked against the allow-list before any process
  starts:
  - every `-C`, `--git-dir` and `--work-tree` path must be admitted
    (`outside` otherwise);
  - a refused option or subcommand answers `invalid_payload`.

  git then runs in `cwd` with a scrubbed environment, its stdout capped at
  8 MiB and stderr at 64 KiB, and is killed at `timeout_ms`.
- `checkout_read_facts.json` (new) describes a fixture tree and the expected
  entries. The tree has:
  - a project with a marker and a linked worktree;
  - an unrelated repository;
  - a symlink inside R0 that points into the unrelated repository, and a
    `..` path that resolves to a directory outside every repository;
  - a symlink inside R0 that points into the linked worktree, and a `..`
    path that resolves there;
  - a linked worktree of R0's repository under
    `<fixture>/tmp/agent-scratchpad-1/`;
  - an isolated node home holding `bootstrap.yaml`, `worktrees/` and
    `clones/`;
  - a large file.

  `test_checkout_read_facts_golden.py` builds the same tree and checks every
  entry outside the node home against the Python oracle above, so node and
  hub agree on what each fact means. It patches
  `src/gobby/hooks/_path_scope.py::_temp_scratchpad_roots` to return
  `<fixture>/tmp`, the temp root the Rust test passes.
- LAND requires the Security Reviewer verdict (Decision 17).

Verification planned:
- `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_checkout_read)'`.
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/test_checkout_read_facts_golden.py -v`.

**Acceptance:**

- 1.2.1 - For every golden case, `checkout.read` answers the expected `root`, `project_id`, `home`, `temp_dir` and entries (`resolved`, `kind`, `checkout_root`, `git_common_dir`, `project_root`, `project_id`, `data`, `truncated`). Every successful reply carries the node's canonical `home` and `temp_dir`, and every `outside` entry carries its non-null `resolved`. test: `crates/gdaemon/tests/node_checkout_read.rs::facts_match_golden`.
- 1.2.2 - The Python oracle produces every golden entry outside the node home, so the hub's local helpers and the node agree. test: `tests/hub/test_checkout_read_facts_golden.py::test_golden_matches_local_helpers`.
- 1.2.3 - Admission follows Decision 4's predicate and home exclusion. These answer `kind: "outside"` with null data: a path in an unrelated repository, a symlink inside R0 that points into that repository, a `..` path that resolves outside every repository, a linked worktree of R0's repository under the temp scratchpad `<fixture>/tmp/agent-scratchpad-1/`, and the isolated node home's `bootstrap.yaml`. These are admitted: a linked worktree of R0's repository under `<home>/worktrees/`, a symlink inside R0 that points into the linked worktree, and a `..` path that resolves there. test: `crates/gdaemon/tests/node_checkout_read.rs::confinement_withholds_outside_paths`.
- 1.2.4 - These answer `invalid_payload`, `stage: "none"`, and start no process: a refused subcommand (`commit`, `checkout`, `config`), a `-c` other than `core.fsmonitor=false`, and each refused option. A `-C`, `--git-dir` or `--work-tree` naming an outside path answers `outside`. A cwd with no marker above it answers `no_project_root`. test: `crates/gdaemon/tests/node_checkout_read.rs::refusals_run_nothing`.
- 1.2.5 - git does not see an inherited `GIT_DIR` or `GIT_INDEX_FILE`, and runs with `GIT_OPTIONAL_LOCKS=0`, leaving no `index.lock` behind. stdout over 8 MiB is cut with `truncated: true`. git past `timeout_ms` is killed and answers `timed_out: true`. test: `crates/gdaemon/tests/node_checkout_read.rs::git_runs_scrubbed_and_bounded`.
- 1.2.6 - `read_limit` caps each file's data, and a reply whose files exceed 8 MiB cuts later entries with `truncated: true`. test: `crates/gdaemon/tests/node_checkout_read.rs::reads_respect_limits`.

### 1.3 Hub checkout-read seam and delivery origin [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/checkout_reads.py`
- `src/gobby/hooks/adapter_execution.py::run_adapter_hook`
- `tests/hooks/checkout_read_fakes.py`
- `tests/hooks/test_checkout_reads.py`

Consumers unchanged:
- `src/gobby/servers/routes/mcp/hooks.py` — no-edit-reason: It calls `run_adapter_hook` with an unchanged signature and already puts `machine_id` and `cwd` in `payload["input_data"]`; the delivery scope opens inside `run_adapter`.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: It awaits `run_adapter_hook` with an unchanged signature, and its delivery is local, so the scope adds no node branch.
- `tests/servers/test_mcp_routes.py` — no-edit-reason: It patches or awaits `run_adapter_hook` with an unchanged signature, and its deliveries are local.
- `tests/workflows/test_hook_evaluation_timeout.py` — no-edit-reason: It awaits `run_adapter_hook` with an unchanged signature, and its timeout behavior is unchanged for a local delivery.
- `tests/workflows/test_hook_saturation.py` — no-edit-reason: It awaits `run_adapter_hook` with an unchanged signature, and the scope runs inside the same executor slot it measures.

**Research context:**
- `run_adapter_hook` captures the daemon loop with
  `asyncio.get_running_loop()`. Its inner `run_adapter` runs in
  `_HOOK_ADAPTER_EXECUTOR` inside `worker_staging_scope()`, the boundary of
  one logical delivery (#21427).
- `payload["input_data"]` carries `machine_id` and `cwd`. The route,
  `src/gobby/servers/routes/mcp/hooks.py`, builds the payload with
  `_normalize_hook_request`.
- hub-mode 1.3 provides:
  - `src/gobby/hub/node_client.py::node_command_client()`, whose
    `send(machine_id, command, payload, timeout_ms)` returns the result or
    raises `NodeCommandError(code, message, stage)`;
  - `node_routing_unavailable` and `node_reply_lost`, plus gdaemon's
    `node_offline`, `node_timeout` and `channel_closed`.
- `src/gobby/hooks/dispatchers/mcp.py::run_coro_blocking` is the existing
  thread-to-loop bridge. It returns `None` on failure, so the seam uses
  `asyncio.run_coroutine_threadsafe` directly to keep the error code.

**Implementation:**
- `checkout_reads.py` (new) implements Decisions 7 to 9:
  - `CheckoutReadUnavailable(OSError)`, plus frozen dataclasses
    `CheckoutFacts` and `PathFact` that mirror the reply and entry;
  - `delivery_scope`, `node_origin`, `event_machine_id`, `path_facts`,
    `run_git` and `run_git_async`;
  - the per-delivery memo and latch;
  - the `loop_thread` guard.

  Each `NodeCommandError` maps to `CheckoutReadUnavailable` with its code.
- `run_adapter_hook`: `run_adapter` enters
  `delivery_scope(machine_id, loop, cwd)` beside `worker_staging_scope()`,
  using `payload["input_data"]`'s `machine_id` and `cwd`. A delivery whose
  machine is missing or local enters the scope with no node machine.
- `checkout_read_fakes.py` (new) monkeypatches `node_command_client().send`
  with `FakeNode`. `FakeNode` answers scripted file entries and git replies
  per machine, can be made unavailable with any code, and records every
  request. Every P2 test imports it.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_checkout_reads.py tests/hooks/test_staged_effects_receipt_route.py -v`.

**Acceptance:**

- 1.3.1 - A delivery whose `input_data.machine_id` names another machine reports `node_origin()` true and that machine from `event_machine_id()`. This holds in the adapter call, in a coroutine run on another loop, and in an offloaded thread. A hub-machine or missing machine id reports false and the hub's machine. The scope resets after the delivery. test: `tests/hooks/test_checkout_reads.py::test_delivery_scope_marks_node_origin`.
- 1.3.2 - `path_facts` sends one `checkout.read` files query to the event's machine, with the cwd, paths and read limit, and returns typed entries. A repeat for memoized paths in the same delivery sends nothing. test: `tests/hooks/test_checkout_reads.py::test_path_facts_queries_node_once_per_delivery`.
- 1.3.3 - Each transport code (`node_routing_unavailable`, `node_reply_lost`, `node_offline`, `node_timeout`, `channel_closed`) and a synchronous call on the daemon loop's thread raises `CheckoutReadUnavailable` naming the code and machine. Every later node read in that delivery raises at once without sending. `no_project_root` does not latch. test: `tests/hooks/test_checkout_reads.py::test_unavailable_answers_latch_per_delivery`.
- 1.3.4 - `run_git` spawns local git exactly as before for a local delivery. For a node delivery it sends a git query and maps the exit code, stdout and stderr, a timeout to `subprocess.TimeoutExpired`, and truncation to `CheckoutReadUnavailable` with code `truncated`. test: `tests/hooks/test_checkout_reads.py::test_run_git_routes_by_origin`.
- 1.3.5 - After a node delivery that read file data and git output, neither appears in `HookEvent.data`, `HookEvent.metadata`, captured log records, session variables or broadcaster payloads. test: `tests/hooks/test_checkout_reads.py::test_node_content_stays_in_delivery_memo`.

### 1.4 Node-origin git runs on the node [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/utils/daemon_git.py::DaemonGitService._execute`
- `src/gobby/utils/daemon_git.py::DaemonGitService.status`
- `src/gobby/workflows/tdd_paths.py::_git_out`
- `src/gobby/hooks/code_navigation_recovery.py::_git_ignored`
- `src/gobby/hooks/_normalization_git.py::_positional_checkout_is_path`
- `tests/hooks/test_node_origin_git.py`

Consumers unchanged:
- `src/gobby/workflows/code_review_scope.py` — no-edit-reason: Its git runs through `daemon_git`, and its failure defaults keep the review gate armed.
- `src/gobby/workflows/ledger_reconcile.py` — no-edit-reason: Its git runs through `daemon_git`, and a failure releases nothing.
- `src/gobby/workflows/condition_helpers.py` — no-edit-reason: `tdd_gate_open` reaches git only through `tdd_paths._git_out`.
- `tests/utils/test_daemon_git.py` — no-edit-reason: Its `_execute` replacements keep the method's signature, and its runs are local, outside any node delivery.
- `src/gobby/agents/checkpoint_manager.py` — no-edit-reason: It calls `DaemonGitService.status` with an unchanged signature. Within a node delivery the call reaches the node through `_execute`'s node branch, and outside one it runs locally as before, so it needs no branch of its own.
- `src/gobby/agents/code_index.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/agents/isolation_git_hygiene.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/agents/sandbox.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/agents/worktree_checkpoint.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/build/claim_recovery.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/build/workspace_git.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/clones/git.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/dispatch/daemon_resume.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/merge_conflict_hydration.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/merge_github_protection.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/sessions/_commits.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/task_repo_paths.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/tasks/_landing.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close_preview.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_validation.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/mcp_proxy/tools/tasks/_task_scope.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/servers/routes/files.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/servers/routes/sessions/core.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/servers/routes/source_control_git.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/servers/session_changes.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/servers/websocket/chat/_session_runtime.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/sessions/handoff_summary.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/sessions/workspace_context.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/tasks/commits.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/tasks/diff_paging.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/tasks/land_commit.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/utils/git.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/utils/project_context.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/workflows/git_utils.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/workflows/task_dirty_state.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `src/gobby/worktrees/merge/resolver.py` — no-edit-reason: Same reason as `checkpoint_manager.py`.
- `tests/agents/test_checkpoint_manager.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_isolation_base_capture.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_isolation_project_json.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_sandbox.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_sandbox_policy.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_worktree_checkpoint.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/agents/test_worktree_fork_commit.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/build/test_claim_recovery.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/clones/test_git.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/e2e/daemon_git_deadline_bootstrap.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/close_review_test_support.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/tasks/test_close_task_attributed_cleanliness.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/tasks/test_task_scope.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/mcp_proxy/tools/test_probe_branch_protection.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/merge/test_merge_mcp_tools.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/servers/routes/test_servers_routes_sessions_routes.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/servers/routes/test_source_control_routes.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/servers/test_servers_sessions_routes.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/servers/test_session_changes.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/servers/websocket/chat/test_servers_websocket_chat_session.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/storage/test_task_close_reviews.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/tasks/test_acceptance_artifacts.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/tasks/test_land_commit.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/utils/test_task_dirty_state.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/workflows/test_commit_guard.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/workflows/test_git_utils.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/workflows/test_hooks.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.
- `tests/worktrees/merge/test_resolver_implementation.py` — no-edit-reason: It patches or awaits `status` with an unchanged signature, and its runs are local, outside any node delivery.

**Research context:**
- Every asynchronous hook-path git call goes through
  `DaemonGitService._execute(argv, *, cwd, timeout, env, input_text)`:
  - `run` and `status` both call it;
  - it builds `GitOk`, `GitFailed` or `GitTimeout` in `_run_git_worker`.
- `status` coalesces identical in-flight calls under the key
  `(loop, resolved_cwd, argv, timeout, env_key)`, which has no machine.
- Hook-path argv sweep, on `0.5.0`:
  - `rev-parse`: `--show-toplevel`,
    `--path-format=absolute --git-common-dir`, and
    `--verify --quiet --end-of-options`;
  - `diff`: `HEAD` or `--cached`, with `--name-status`, `--name-only`,
    `--stat`, `-z` or `--diff-filter`;
  - `ls-files`, `ls-tree`, `status --porcelain=v1 -z`, `log -1 --format=%ct`,
    `check-ignore --stdin -z`, `merge-base --is-ancestor` and `rev-list`;
  - the global options `-c core.fsmonitor=false`, `--literal-pathspecs`,
    `--no-optional-locks`, and commit_guard's `-C`, `--git-dir` and
    `--work-tree`.

  All of these fall inside Decision 6's allow-list.
- The synchronous hook-path git calls are:
  - `src/gobby/workflows/tdd_paths.py::_git_out` (`rev-parse`);
  - `src/gobby/hooks/code_navigation_recovery.py::_git_ignored`
    (`check-ignore --stdin -z`, 2-second timeout);
  - `src/gobby/hooks/_normalization_git.py::_positional_checkout_is_path`
    (`rev-parse --verify` and `ls-files --cached -z`, 1-second timeouts,
    with a `GIT_*`-free environment).

  The synchronous helpers in `src/gobby/utils/git.py` have no hook-path
  caller. `src/gobby/workflows/validation_cover.py` runs only for a
  found-work session that is local (Decision 15).
- `_positional_checkout_is_path` tests `Path(path).exists()` for each
  operand. For a relative operand that resolves against the daemon's process
  cwd, not the command's `cwd` (Decision 13).

**Implementation:**
- `_execute`: for a node delivery, return the result of
  `checkout_reads.run_git_async(argv[1:], cwd=cwd, timeout=timeout, input_bytes=...)`,
  mapped as `_run_git_worker` maps a process:
  - exit 0 is `GitOk` with stdout decoded the same way;
  - a non-zero exit is `GitFailed`;
  - `timed_out` is `GitTimeout`;
  - truncation is `GitFailed` with detail `checkout_read_truncated`;
  - `CheckoutReadUnavailable` is `GitFailed` with its message as the
    detail;
  - a non-`None` `env` is `GitFailed` with
    `checkout_read_unavailable: env_unsupported (machine <id>)`, sent
    nowhere.

  No local process starts.
- `status`: the coalescing key gains `event_machine_id()`.
- `_git_out`, `_git_ignored` and `_positional_checkout_is_path`: a node
  delivery calls `checkout_reads.run_git`. `_git_ignored` passes its stdin
  as `input_bytes`. Each existing `OSError` or `TimeoutExpired` branch
  handles the seam's errors.
- `_positional_checkout_is_path` tests existence by joining a relative
  operand to `cwd`, for both origins. For local events this is a deliberate
  local behavior change, which fixes the found bug (Decision 13). For a node
  delivery it reads
  `path_facts(paths, cwd=cwd)`, where any kind other than `missing` or
  `outside` means the path exists.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_git.py tests/utils/test_daemon_git.py tests/workflows/test_tdd_gate_worktree_paths.py -v`.

**Acceptance:**

- 1.4.1 - In a node delivery, `daemon_git.run` and `daemon_git.status` send git queries to the node and return `GitOk`, `GitFailed` or `GitTimeout` from its exit code, output and timeout, and no local git process starts. test: `tests/hooks/test_node_origin_git.py::test_node_origin_git_runs_on_node`.
- 1.4.2 - Identical `status` calls from deliveries of two machines never share one in-flight result. Identical calls from the same machine still coalesce. test: `tests/hooks/test_node_origin_git.py::test_status_coalescing_is_machine_scoped`.
- 1.4.3 - An unavailable node, a truncated reply and an `env` override each yield `GitFailed` whose detail carries the diagnostic, and the env case sends nothing. test: `tests/hooks/test_node_origin_git.py::test_unanswered_node_git_fails_with_diagnostic`.
- 1.4.4 - In a node delivery, `_git_out`, `_git_ignored` and `_positional_checkout_is_path` send their git and existence checks to the node and start no local process. Local deliveries spawn as before. test: `tests/hooks/test_node_origin_git.py::test_sync_git_sites_route_by_origin`.
- 1.4.5 - For a local `git checkout <file>`, a relative operand that exists under the command's `cwd` but not under the daemon's process cwd proves path mode. test: `tests/hooks/test_node_origin_git.py::test_positional_checkout_joins_relative_operand_to_cwd`.

## P2: Hub sites read through the seam
`kind: framing`

**Goal**: every hub site that reads the working tree on a hook path reads
the node's answer for a node event, or takes its stated disposition. Local
events keep their current code.

### 2.1 Node events take their project from the node's checkout rows [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/hooks/project_context.py::resolve_hook_project_context`
- `src/gobby/workflows/hooks.py::WorkflowHookHandler._resolve_project_path`
- `src/gobby/workflows/hooks.py::_is_known_no_repo_project`
- `src/gobby/workflows/hooks.py::_NO_REPO_PROJECT_CONSTANTS`
- `src/gobby/workflows/hooks.py::_NO_REPO_LEGACY_PROJECT_IDS`
- `src/gobby/workflows/hooks.py::_NO_REPO_SYSTEM_PROJECTS`
- `src/gobby/workflows/hook_project_path.py`
- `tests/workflows/test_workflow_hooks.py::*` — scope-reason: the `_NO_REPO_SYSTEM_PROJECTS` and `_is_known_no_repo_project` import moves to `gobby.workflows.hook_project_path`
- `tests/hooks/test_session_binding.py`
- `tests/hooks/test_node_origin_project.py`

Consumers unchanged:
- `tests/workflows/test_hooks.py` — no-edit-reason: It calls `WorkflowHookHandler._resolve_project_path`, which keeps its signature and local behavior.
- `tests/workflows/test_database_deadline_retry.py` — no-edit-reason: It patches the `_resolve_project_path` method, which stays on the handler.
- `src/gobby/hooks/event_handlers/_session_start/flow.py` — no-edit-reason: It calls `resolve_hook_project_context` with an unchanged signature, and the node branch lives inside that function.
- `src/gobby/hooks/hook_manager.py` — no-edit-reason: It calls `resolve_hook_project_context` with an unchanged signature, and the node branch lives inside that function.
- `src/gobby/hooks/session_lookup.py` — no-edit-reason: It calls `resolve_hook_project_context` with an unchanged signature, and the node branch lives inside that function.
- `tests/hooks/test_hook_extracted_helpers.py` — no-edit-reason: It calls or patches `resolve_hook_project_context` with an unchanged signature for local events.
- `tests/hooks/test_hook_manager.py` — no-edit-reason: It calls or patches `resolve_hook_project_context` with an unchanged signature for local events.
- `tests/hooks/test_hooks_manager.py` — no-edit-reason: It calls or patches `resolve_hook_project_context` with an unchanged signature for local events.
- `tests/hooks/test_session_activation_reconciliation.py` — no-edit-reason: It calls or patches `resolve_hook_project_context` with an unchanged signature for local events.
- `tests/hooks/test_transcript_path_derivation.py` — no-edit-reason: It calls or patches `resolve_hook_project_context` with an unchanged signature for local events.

**Research context:**
- `resolve_hook_project_context` tries these sources in order:
  1. the explicit project;
  2. the session candidates (hub-mode 2.2 skips another machine's rows);
  3. the cached session;
  4. the existing session.

  After them, a contract-probe cwd gets the global project, and an unusable
  cwd gets `_project_id_from_current_context()`, which is the daemon's own
  context. Otherwise it calls `resolve_project_id(None, cwd)`, which is
  `ProjectIdResolver.resolve`: a disk marker walk with `get_project_context`,
  then `ensure_project_in_db`. With no marker that raises `ValueError`.
- `_resolve_project_path` calls `require_local_machine_id(None, ...)` and
  `require_root(db, project_id, machine_id)`, and compares `worktree_root`
  with `Path(...).resolve()`. Its soft errors fall back to `worktree_root`.
  `worktree_root` comes from `resolve_git_worktree_root_async`, which runs on
  the node for a node delivery (1.4).
- `src/gobby/storage/project_checkouts.py::LocalProjectCheckoutManager` has
  `get(machine_id, project_id)` and `list_for_machine(machine_id)`. Neither
  has a machine guard or touches the filesystem. hub-mode 1.4 writes a node's
  rows.
- `src/gobby/workflows/hooks.py` is 923 lines. The node branch would grow
  it, so 2.1 splits it.
- hub-mode 2.2.7 pins a remote event taking its project from its cwd, in
  `tests/hooks/test_session_binding.py::test_project_context_skips_foreign_rows`,
  which hub-mode 2.2 creates.

**Implementation:**
- Split: move the body of `WorkflowHookHandler._resolve_project_path`, with
  `_NO_REPO_PROJECT_CONSTANTS`, `_NO_REPO_LEGACY_PROJECT_IDS`,
  `_NO_REPO_SYSTEM_PROJECTS` and `_is_known_no_repo_project`, out of
  `src/gobby/workflows/hooks.py` into the new
  `src/gobby/workflows/hook_project_path.py`, as
  `resolve_hook_project_path(rule_engine, event, worktree_root)`.
  - The method stays and delegates to it.
  - `hooks.py` imports `_is_known_no_repo_project` for its own use.
  - `tests/workflows/test_workflow_hooks.py` imports the moved names from the
    new module.
- `resolve_hook_project_path`, for a node delivery:
  - It reads `LocalProjectCheckoutManager(db).get(event_machine_id(), project_id)`.
  - It returns the row's `root_path` when `worktree_root` is absent or
    equals that root (compared as strings after `os.path.normpath`).
    Otherwise it returns `worktree_root`.
  - It never calls `require_local_machine_id`, `require_root`,
    `resolve_operation_root` or `Path.resolve`.
  - With no row, it returns `worktree_root`.

  Local deliveries keep the moved code unchanged.
- `resolve_hook_project_context`, for a node delivery after the session
  sources:
  - A contract-probe cwd is unchanged.
  - An unusable cwd is skipped, without the daemon's current-context
    project.
  - Otherwise it takes the project of the row from
    `LocalProjectCheckoutManager(session_manager.db).list_for_machine(event_machine_id())`
    whose `root_path` is the longest that equals or encloses the cwd
    component by component.
  - With no such row it raises `ValueError` with
    `checkout_read_unavailable: no_checkout_row (machine <id>)`. The caller's
    handling of the local no-marker `ValueError` applies.
  - It never calls `resolve_project_id`.
- `test_session_binding.py`: `test_project_context_skips_foreign_rows` seeds
  the remote machine's checkout row for the event cwd, and keeps its
  assertion that foreign session rows are never used.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_project.py tests/hooks/test_session_binding.py tests/workflows/test_workflow_hooks.py tests/workflows/test_hooks.py -v`.

**Acceptance:**

- 2.1.1 - A node event with no explicit or session project takes the project of the node's longest checkout row enclosing its cwd. A hub marker at the same path is never read, and no checkout row is registered. test: `tests/hooks/test_node_origin_project.py::test_node_cwd_resolves_from_node_rows`.
- 2.1.2 - A node event whose cwd matches no row of its machine raises the no-project `ValueError` naming `checkout_read_unavailable: no_checkout_row` and the machine. A node event with an unusable cwd is skipped without the daemon's current-context project. test: `tests/hooks/test_node_origin_project.py::test_node_cwd_without_row_fails_closed`.
- 2.1.3 - For a node event, `_resolve_project_path` returns the node's checkout row root when the worktree root is absent or equals it, and the worktree root otherwise. It never calls `require_local_machine_id` or resolves a path on the hub. test: `tests/hooks/test_node_origin_project.py::test_node_project_path_uses_node_row`.
- 2.1.4 - After the move, the no-repo project set and predicate are unchanged. test: `tests/workflows/test_workflow_hooks.py::TestProjectPathResolution.test_no_repo_project_ids_include_constants_and_legacy_literals`.
- 2.1.5 - A remote event whose session ids name another machine's rows takes its project from its own machine's checkout row for its cwd, never from those rows. test: `tests/hooks/test_session_binding.py::test_project_context_skips_foreign_rows`.

### 2.2 Path primitives and write scope use node facts [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/hooks/_path_scope.py::current_project_root`
- `src/gobby/hooks/_path_scope.py::resolve_tool_path`
- `src/gobby/hooks/_path_scope.py::checkout_root`
- `src/gobby/hooks/_path_scope.py::_git_common_dir`
- `src/gobby/hooks/_path_scope.py::_is_project_managed_path`
- `src/gobby/hooks/_normalization_git.py::classify_git_mutation`
- `src/gobby/workflows/condition_helpers_paths.py::write_paths_within`
- `src/gobby/workflows/enforcement/blocking.py::plan_write_paths_allowed`
- `tests/hooks/test_node_origin_path_scope.py`

Consumers unchanged:
- `src/gobby/hooks/_normalization_canonical.py` — no-edit-reason: It reaches paths only through `resolve_tool_path`.
- `src/gobby/providers/path_policy.py` — no-edit-reason: `plan_write_paths_allowed` passes it node-resolved paths, and the node's roots through the existing `home_root` and `temp_root` parameters.
- `src/gobby/hooks/_normalization_base_commands.py` — no-edit-reason: It calls `classify_git_mutation(parts, cwd, base_cwd)` with an unchanged signature, and the node branch lives in `checkout_root`.
- `src/gobby/workflows/engine/templating.py` — no-edit-reason: It registers `plan_write_paths_allowed` as a template function with an unchanged signature, and the node branch lives inside that function.
- `tests/workflows/test_condition_helpers_paths.py` — no-edit-reason: It calls `write_paths_within` with an unchanged signature for local events, whose behavior is unchanged.

**Research context:**
- `src/gobby/hooks/_path_scope.py`, which is on the hub's disk:
  - `resolve_tool_path(path, cwd)` uses `expanduser` and
    `resolve(strict=False)`.
  - `current_project_root(event_data)` resolves metadata `project_path`, or
    walks to a marker with `find_project_root(cwd)`.
  - `checkout_root(path)` walks up for `.git`.
  - `_git_common_dir(path)` reads `.git`, the linked gitdir and `commondir`.
  - `_is_project_managed_path` accepts a path under the project root or one
    sharing its git repository. It rejects agent scratchpads and known
    external paths.
- `classify_git_mutation` calls `checkout_root(Path(git_cwd).resolve())`
  three times.
- `write_paths_within` resolves write paths and allowed directories through
  `resolve_tool_path` against `current_project_root`. An empty path set
  answers `False`.
- `plan_write_paths_allowed` compares `os.path.realpath` of the project and
  of each candidate. It calls `is_project_plan_artifact_path` and
  `is_plan_scratch_path(path, provider)`; the scratch check uses the hub's
  `Path.home()` and temp dir unless `home_root` and `temp_root` are passed.

**Implementation:** each branch below applies to a node delivery only.
- `resolve_tool_path` returns `path_facts([path], cwd=cwd)` `resolved`.
- `current_project_root` returns `Path(project_path)` when metadata carries
  one, because the node already resolved it (2.1). Otherwise it returns
  `path_facts((), cwd=cwd).root`.
- `checkout_root` and `_git_common_dir` return the entry's `checkout_root`
  and `git_common_dir`.
- `_is_project_managed_path` returns whether the entry's `kind` is not
  `outside`.
- `classify_git_mutation` passes `Path(git_cwd)` unresolved, because
  `checkout_root`'s node branch resolves it. Local events keep
  `Path(git_cwd).resolve()`.
- `write_paths_within` returns `False` on `CheckoutReadUnavailable`.
- `plan_write_paths_allowed`:
  - It compares the node's resolved candidate with the node's resolved
    project path component by component.
  - It recognizes a plan artifact by the `.gobby/plans/*.md` parts of the
    candidate relative to that project.
  - It calls `is_plan_scratch_path(resolved, provider, home_root=home, temp_root=temp_dir)`
    with the reply's roots.
  - It returns `False` on `CheckoutReadUnavailable`.
- Every other unavailable read propagates, so normalization fails closed
  (§Site Disposition).

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_path_scope.py tests/hooks/test_path_scope.py -v`.

**Acceptance:**

- 2.2.1 - For a node event, tool paths, the project root, checkout roots, git common dirs and the managed-path verdict come from the node's facts. A hub file, symlink or `.git` at the same paths changes none of them. test: `tests/hooks/test_node_origin_path_scope.py::test_node_path_primitives_use_node_facts`.
- 2.2.2 - `classify_git_mutation` for a node `git` command takes its checkout root from the node, and a local command still resolves on the hub. test: `tests/hooks/test_node_origin_path_scope.py::test_git_mutation_checkout_root_by_origin`.
- 2.2.3 - `write_paths_within` admits a node write under an allowed project directory by the node's resolved path, refuses one whose node path escapes through a symlink, and answers `False` when the node is unavailable. test: `tests/hooks/test_node_origin_path_scope.py::test_node_write_scope`.
- 2.2.4 - In plan mode, a node write to the project's `.gobby/plans/x.md` and to the provider scratch directory under the node's home is allowed. A node source file is refused, and an unavailable node refuses every write. test: `tests/hooks/test_node_origin_path_scope.py::test_node_plan_mode_writes`.
- 2.2.5 - An unavailable node during normalization of a node `Write` blocks the critical hook with the diagnostic in its reason. test: `tests/hooks/test_node_origin_path_scope.py::test_unavailable_normalization_fails_closed`.

### 2.3 Monolith guard reads node files [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/monolith_guard.py::_project_root`
- `src/gobby/workflows/monolith_guard.py::_projection_for_path`
- `src/gobby/workflows/monolith_guard.py::outstanding_monolith_paths`
- `tests/workflows/test_node_origin_monolith.py`

Consumers unchanged:
- `src/gobby/install/shared/workflows/rules/monolith-enforcement/require-same-session-decomposition.yaml` — no-edit-reason: It already lists the returned paths in its block reason, which then carries the diagnostic.
- `src/gobby/workflows/safe_evaluator.py` — no-edit-reason: It passes `project_path` and the variables through unchanged.
- `tests/workflows/test_monolith_guard.py` — no-edit-reason: It calls `outstanding_monolith_paths` with an unchanged signature for local events, whose behavior is unchanged.

**Research context:**
- `_project_root` resolves `project_path` and requires `is_dir()`.
- `_projection_for_path(root, raw_path)` resolves the path and keeps only
  guard paths (`is_monolith_guard_path`). It reads the whole file as UTF-8
  with replacement, and returns `None` on `OSError`.
- `projected_monolith_paths` projects patch, change and fallback paths
  through `_get_projection`, so one call can touch several files.
- `outstanding_monolith_paths(variables, project_path)` recounts every
  ledger path in `task_edited_files`.
- `MONOLITH_LINE_LIMIT` is 1,000.

**Implementation:** each branch below applies to a node delivery only.
- `_project_root` returns `Path(project_path)` when it is absolute.
- `_projection_for_path` reads
  `path_facts([absolute], read_limit=8_388_608)`:
  - It computes `relative` from the entry's `resolved` against the root.
  - `outside` or `missing` follows the local missing or out-of-root
    outcome.
  - A `file` decodes `data` as UTF-8 with replacement.
- `outstanding_monolith_paths` prefetches every ledger path with one
  `path_facts` call per 256 paths, then projects from the memo.
- When a guard path is unanswered, or truncated with a partial count under
  the limit, it is listed as
  `<relative> (checkout_read_unavailable: <code> (machine <id>))`, where
  `<code>` is `truncated` for truncation. That blocks. A truncated read whose
  partial count already reaches the limit is simply over budget.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_node_origin_monolith.py tests/workflows/test_monolith_guard.py -v`.

**Acceptance:**

- 2.3.1 - A node `Write` that takes a node file from 990 to 1,000 lines is listed, and one that keeps it at 995 lines is not. A different file at the same path on the hub does not change either answer. test: `tests/workflows/test_node_origin_monolith.py::test_node_projection_reads_node_file`.
- 2.3.2 - The ledger recount for a node session counts the node's files in one batched read per 256 paths. test: `tests/workflows/test_node_origin_monolith.py::test_node_ledger_recount_batches`.
- 2.3.3 - An unavailable node, or a truncated read under the limit, lists the guard path with the diagnostic, so the rule blocks. test: `tests/workflows/test_node_origin_monolith.py::test_unanswered_node_lists_diagnostic`.

### 2.4 Rust TDD evidence reads node files [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/rust_test_evidence.py::_read_absolute_text`
- `src/gobby/workflows/rust_test_evidence.py::rust_edit_is_test_writing`
- `tests/workflows/test_node_origin_rust_tdd.py`

Consumers unchanged:
- `src/gobby/workflows/condition_helpers.py` — no-edit-reason: It calls `rust_edit_is_test_writing` with an unchanged signature, and `tdd_gate_open` reaches git through 1.4.
- `tests/workflows/test_rust_test_evidence.py` — no-edit-reason: It calls `rust_edit_is_test_writing` with an unchanged signature for local events, whose behavior is unchanged.

**Research context:**
- `rust_edit_is_test_writing(path, tool_input)` reads the prior text with
  `_read_absolute_text`. A missing file, or an `OSError`, gives `None`. It
  then judges each change with `_change_is_test_only(on_disk, change)`.

**Implementation:**
- `_read_absolute_text`, for a node delivery, reads
  `path_facts([path], read_limit=8_388_608)`:
  - `missing` gives `None`, as a local missing file does;
  - a `file` decodes `data` as UTF-8 with replacement;
  - truncation and `outside` raise `CheckoutReadUnavailable`.
- `rust_edit_is_test_writing` returns `False` on `CheckoutReadUnavailable`.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_node_origin_rust_tdd.py tests/workflows/test_rust_test_evidence.py -v`.

**Acceptance:**

- 2.4.1 - A node edit that adds only a `#[test]` function inside the node file's `cfg(test)` module is test-only, judged from the node's text while the hub has no file at that path. test: `tests/workflows/test_node_origin_rust_tdd.py::test_node_rust_test_edit_reads_node_text`.
- 2.4.2 - An unavailable node, or a truncated prior text, makes the edit not test-only, including a full-content `Write`. test: `tests/workflows/test_node_origin_rust_tdd.py::test_unanswered_node_is_not_test_only`.

### 2.5 Code navigation reads node files [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/hooks/code_navigation_recovery.py::_verified_source_line_count`
- `src/gobby/hooks/code_navigation_recovery.py::_ignore_decision`
- `tests/hooks/test_node_origin_code_navigation.py`

**Research context:**
- `_verified_source_line_count(paths)` counts newlines in each operand. It
  answers `None` when an operand is missing, not a file, or over
  `MAX_NARROW_SOURCE_LINES` or `_LINE_COUNT_SCAN_LIMIT` (262,144 bytes).
  `None` makes the read broad, which gets the gcode redirect.
- `_ignore_decision(path, root)`:
  - It finds a linked checkout's root by walking up for `.git`.
  - It treats `build`, `dist` and index-excluded directories as ignored.
  - It reads `<root>/.gobby/gcode.json` for the hidden allowlist; on
    `OSError` or `ValueError` it uses `{}`.
  - It hands everything else to `_git_ignored` (1.4).

**Implementation:** each branch below applies to a node delivery only.
- `_verified_source_line_count` reads all operands with one
  `path_facts(paths, read_limit=_LINE_COUNT_SCAN_LIMIT + 1)`:
  - it counts newlines in `data` under the same limits;
  - a non-`file` entry, truncation or `CheckoutReadUnavailable` answers
    `None`.
- `_ignore_decision`:
  - The linked checkout root is the entry's `checkout_root`.
  - `gcode.json` is read with `path_facts([...], read_limit=65_536)`.
  - A missing, outside, truncated or unavailable config is `{}`, as an
    `OSError` is today.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_code_navigation.py tests/hooks/test_code_navigation.py -v`.

**Acceptance:**

- 2.5.1 - A whole-file read of a 40-line node file counts as narrow, and of a node file over the scan limit as broad, from the node's bytes while the hub has no file at that path. test: `tests/hooks/test_node_origin_code_navigation.py::test_node_line_count_reads_node_bytes`.
- 2.5.2 - The ignore decision for a node path uses the node's checkout root and the node's `.gobby/gcode.json` allowlist. test: `tests/hooks/test_node_origin_code_navigation.py::test_node_ignore_decision_uses_node_config`.
- 2.5.3 - An unavailable node makes the read unverified (broad) and the allowlist empty. test: `tests/hooks/test_node_origin_code_navigation.py::test_unanswered_node_keeps_enforcement`.

### 2.6 Node edits record the node's checkout, and ledger owners are machine-scoped [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/workflows/task_claim_state.py::resolve_edit_pair`
- `src/gobby/hooks/event_handlers/_tool.py::ToolEventHandlerMixin._notify_code_index`
- `src/gobby/workflows/commit_guard.py::_live_path_owners`
- `tests/hooks/test_node_origin_edits.py`

Consumers unchanged:
- `tests/workflows/test_commit_guard.py` — no-edit-reason: Its `_live_path_owners` patches replace the function whole, and its database tests run on the hub machine.
- `tests/hooks/test_tool_handlers.py` — no-edit-reason: It calls or patches `resolve_edit_pair` with an unchanged signature for local events, whose behavior is unchanged.
- `tests/workflows/test_task_claim_state.py` — no-edit-reason: It calls `resolve_edit_pair` with an unchanged signature for local events, whose behavior is unchanged.
- `tests/mcp_proxy/tools/test_claim_task.py` — no-edit-reason: Its `_live_path_owners` patch replaces the function whole.
- `tests/workflows/test_session_variable_mutation_scope.py` — no-edit-reason: Its path-owners case registers its session with `get_machine_id()` and calls `_live_path_owners` outside a hook delivery, so the machine filter keeps that session.

**Research context:**
- `resolve_edit_pair(file_path, cwd, *, project_id, db, variables)` does
  this on the hub's disk:
  1. It expands, joins and resolves the target.
  2. It finds the nearest marker with `find_project_root`.
  3. It reads the marker id with `get_project_context`.
  4. It accepts a different id only through `sanctioned_worktree_root`.
  5. It returns `(root, relative)`.

  Its callers are `ToolEventHandlerMixin._assert_edit_path_selection`
  (before tool), `ToolEventHandlerMixin._record_successful_file_mutation`
  (after tool) and `commit_guard`.
- `_notify_code_index` resolves the hub project for the root, then triggers
  the hub's gcode index. `_record_successful_file_mutation` needs no edit:
  it calls `resolve_edit_pair` and `_notify_code_index` unchanged, and its
  landed check runs git through 1.4.
- `commit_guard`'s git runs through `daemon_git` (1.4). It already blocks on
  a git failure and names the detail.
- `_live_path_owners` selects every live session but the caller's, and
  reads each one's held paths for `checkout_root`. A node checkout and a
  hub checkout can share that string. `sessions.machine_id` is `uuid NOT NULL`
  (`crates/gcore/assets/schema/baseline.sql`).

**Implementation:**
- `resolve_edit_pair`, for a node delivery, reads
  `path_facts([file_path], cwd=cwd)`:
  - `outside`, or a null `project_root`, answers `None`.
  - An entry whose `project_id` differs from `project_id` answers `None`,
    because confinement admits only R0's repository.
  - Otherwise it answers `(project_root, relative path of resolved)`.
- On `CheckoutReadUnavailable` it falls back to the normalized lexical
  join of `cwd` and `file_path`. That join must lie under the root of
  `LocalProjectCheckoutManager(db).get(event_machine_id(), project_id)`,
  and the answer is relative to that root. Otherwise the answer is `None`.
- `_notify_code_index` returns at once for a node delivery.
- `_live_path_owners` adds `AND sessions.machine_id = %s` with
  `event_machine_id()`. Outside a hook delivery that is the hub's machine.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_edits.py tests/workflows/test_task_claim_state.py tests/workflows/test_commit_guard.py tests/hooks/test_tool_handlers.py tests/workflows/test_session_variable_mutation_scope.py -v`.

**Acceptance:**

- 2.6.1 - A node edit attributes `(project_root, relative path)` from the node's facts, and a hub marker at the same path is never read. A node path outside the project, or in another project, is not attributed. test: `tests/hooks/test_node_origin_edits.py::test_node_edit_pair_uses_node_facts`.
- 2.6.2 - With the node unavailable, a node edit under the node's checkout row is attributed by its lexical path, and one outside the row is not. test: `tests/hooks/test_node_origin_edits.py::test_unavailable_node_edit_pair_is_lexical`.
- 2.6.3 - A node edit triggers no hub code-index update. test: `tests/hooks/test_node_origin_edits.py::test_node_edit_skips_hub_code_index`.
- 2.6.4 - A hub session and a node session hold the same path in checkouts with the same absolute root. Each machine's ownership check counts only its own sessions. test: `tests/hooks/test_node_origin_edits.py::test_live_path_owners_are_machine_scoped`.

### 2.7 Shell classification reads node executables [category: code] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/hooks/shell_execution.py::path_execution`
- `src/gobby/hooks/shell_execution.py::read_script_body`
- `src/gobby/hooks/_normalization_operands.py::_file_transfer_paths`
- `tests/hooks/test_node_origin_shell.py`

Consumers unchanged:
- `src/gobby/hooks/_normalization_canonical.py` — no-edit-reason: It calls `path_execution` and `read_script_body` with unchanged signatures, and the node branch lives inside those functions.
- `src/gobby/hooks/_normalization_sed.py` — no-edit-reason: It calls `read_script_body` with an unchanged signature, and the node branch lives inside that function.
- `src/gobby/hooks/_normalization_file_commands.py` — no-edit-reason: It calls `_file_transfer_paths` with an unchanged signature, and the node branch lives inside that function.
- `tests/hooks/test_normalization_canonical_mutation_scope.py` — no-edit-reason: It exercises `read_script_body` with an unchanged signature for local events, whose behavior is unchanged.

**Research context:**
- `path_execution(words, base)` resolves the first word with
  `resolve_tool_path` (2.2). It requires `is_file()`, then reads up to
  `_HEADER_LIMIT` (512) bytes to recognize a native binary or a shebang.
- `read_script_body(word, base)` reads up to `SCRIPT_BODY_LIMIT` (64 KiB).
- `_file_transfer_paths` probes `is_dir()` on an absolute destination.

**Implementation:** each branch below applies to a node delivery only.
- `path_execution` reads
  `path_facts([path], read_limit=_HEADER_LIMIT)`. A non-`file` entry
  answers `None`, as a local non-file does. A `file` uses `data` as the
  header.
- `read_script_body` reads with `read_limit=SCRIPT_BODY_LIMIT + 1` and keeps
  the existing over-limit handling.
- `_file_transfer_paths` treats the destination as a directory when its
  entry's `kind` is `dir`.
- `CheckoutReadUnavailable` propagates, so classification fails closed.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_shell.py tests/hooks/test_normalization_canonical_mutation_scope.py -v`.

**Acceptance:**

- 2.7.1 - A node `./run.sh` is classified from the node's shebang and script body, and a node Mach-O header is classified as native, while the hub has no file at either path. test: `tests/hooks/test_node_origin_shell.py::test_node_executable_classified_from_node_bytes`.
- 2.7.2 - A node `cp a.txt dest` treats `dest` as a directory only when the node reports `dir`. test: `tests/hooks/test_node_origin_shell.py::test_node_transfer_destination_kind`.
- 2.7.3 - An unavailable node while classifying a node shell command blocks the critical hook with the diagnostic. test: `tests/hooks/test_node_origin_shell.py::test_unanswered_node_shell_fails_closed`.

### 2.8 Node events skip hub-only effects [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_worktree_create`
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_worktree_remove`
- `src/gobby/adapters/claude_code.py::_saved_mcp_result`
- `src/gobby/adapters/grok.py::_spilled_mcp_output`
- `src/gobby/adapters/codex_impl/hooks_adapter.py::_command_execution_result`
- `src/gobby/workflows/engine/proxy_hooks.py::ProxyHooksMixin._run_rtk_proxy`
- `src/gobby/workflows/engine/run_command_effects.py::RunCommandEffectsMixin._apply_run_command`
- `tests/hooks/test_node_origin_dispositions.py`

Consumers unchanged:
- `src/gobby/workflows/found_work_gate.py` — no-edit-reason: For a node-owned session, `_terminal_failures` already returns `()` after `RemoteSessionOwnershipError` (Decision 15). The test pins it.
- `src/gobby/config/validation_detection.py` — no-edit-reason: Its only hook-path caller is `_terminal_failures`, which discards the result for a node session.
- `src/gobby/hooks/event_handlers/__init__.py` — no-edit-reason: It maps `WORKTREE_CREATE` and `WORKTREE_REMOVE` to `handle_worktree_create` and `handle_worktree_remove`, whose signatures are unchanged; the node skip lives inside them.
- `src/gobby/workflows/engine/effects.py` — no-edit-reason: It awaits `_apply_run_command` with an unchanged signature, and the node skip lives inside that method.

**Granularity:** five acceptance items, one outcome. A node event triggers
no hub-machine effect. Each item is one guard at a different site, with
one shared fake node.

**Research context:**
- `handle_worktree_create` creates a git worktree on the hub from
  `event.cwd`. `handle_worktree_remove` can remove one.
- `_saved_mcp_result` and `_spilled_mcp_output` read provider spill files
  named in the tool output, and keep the text on `OSError`.
  `_command_execution_result` reads the Codex transcript, and returns `None`
  on `OSError`.
- `_run_rtk_proxy` runs the rtk rewrite on the hub. `_apply_run_command`
  spawns the impeccable `hook.mjs`, which is advisory and documented as
  fail-open.
- `FoundWorkStopAnalyzer._terminal_failures` loads the stop validation
  config, then calls `derive_transcript_evidence`. That calls
  `require_local_session_ownership`, which raises
  `RemoteSessionOwnershipError` for a session owned by another machine.
  The analyzer catches it and returns `()`.

**Implementation:** each guard below applies to a node delivery only.
- `handle_worktree_create` and `handle_worktree_remove` return the allow
  response first, before any hub resolution, git call or filesystem effect.
- `_saved_mcp_result` and `_spilled_mcp_output` return `text`.
  `_command_execution_result` returns `None`.
- `_run_rtk_proxy` returns without a rewrite.
- `_apply_run_command` returns without spawning, and logs nothing that
  carries content.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_node_origin_dispositions.py tests/workflows/test_found_work_gate.py -v`.

**Acceptance:**

- 2.8.1 - A node `WorktreeCreate` or `WorktreeRemove` answers allow, and creates or removes no hub worktree or directory. test: `tests/hooks/test_node_origin_dispositions.py::test_node_worktree_hooks_touch_nothing_on_hub`.
- 2.8.2 - Node Claude and Grok spill notices keep their text, and a node Codex payload recovers no transcript result, even with matching files on the hub. test: `tests/hooks/test_node_origin_dispositions.py::test_node_provider_files_are_not_read`.
- 2.8.3 - A node shell command gets no rtk rewrite, and runs as written. test: `tests/hooks/test_node_origin_dispositions.py::test_node_shell_skips_rtk_rewrite`.
- 2.8.4 - A node event matching `impeccable/design-detector` spawns no process. test: `tests/hooks/test_node_origin_dispositions.py::test_node_run_command_spawns_nothing`.
- 2.8.5 - A node-owned session's turn end with no task disposition gets `()` from `_terminal_failures`, and no node read is sent. test: `tests/hooks/test_node_origin_dispositions.py::test_node_found_work_terminal_evidence_is_empty`.

## P3: Live proof
`kind: framing`

**Goal**: a live hub and node pair proves that a node-origin rule reads the
node's answer, and that the hub never falls back to its own disk.

### 3.1 Live node-origin checkout reads [category: code] (depends: 1.2, 2.1, 2.2, 2.3)
`kind: deliverable`

Targets:
- `tests/e2e/test_hub_node_checkout_read.py`

**Research context:**
- `tests/e2e/test_hub_node_pair.py` provides two fixtures:
  - `pair_hub`, a hub-mode daemon with a self-signed certificate over the
    e2e harness's isolated config and database;
  - `pair_node`, an enrolled bare `gdaemon serve` with `GOBBY_HOME` in
    `tmp_path`, which yields `EnrolledNode` with `machine_id` and `api_key`.

  hub-mode 5.1 proves the live channel, node checkout registration through
  `POST /api/nodes/checkouts`, and hook delivery on this pair.
- Both daemons run on one machine, so the hub's disk can read the node's
  checkout path. The proof therefore pairs a live answer with an offline
  refusal.

**Implementation:**
- The test creates, under the node's `tmp_path`, a git checkout with
  `.gobby/project.json` and a 990-line `src/app.py`. It registers the
  project on the hub, and the node's checkout through
  `POST /api/nodes/checkouts` with the node's key.
- It posts hook envelopes to the hub's `POST /api/hooks/execute` with the
  node's key. Each envelope names the node's machine, a node-origin session
  start, then before-tool `Write` events.
- It then stops the node daemon, and repeats a write that would be allowed.

Verification planned:
- `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_hub_node_checkout_read.py -v -m e2e`.

**Acceptance:**

- 3.1.1 - With the node connected, a node-origin `Write` that takes `src/app.py` to 1,000 lines is blocked by `require-decompose-monolith-before-threshold-write`, and one that keeps it at 995 lines is allowed. test: `tests/e2e/test_hub_node_checkout_read.py::test_node_file_drives_monolith_gate`.
- 3.1.2 - After the node daemon stops, the 995-line `Write` is blocked with `checkout_read_unavailable` and the node's machine, although `src/app.py` is readable at that path on the hub's disk. test: `tests/e2e/test_hub_node_checkout_read.py::test_offline_node_fails_closed`.

## V1 Plan Changelog
`kind: verification`

- 2026-10-04 to 2026-10-05: the first draft carried envelope `file_facts`.
  It reached V1 consensus at `9c26026f66` and was stamped at `74f5b4ec32`,
  but was never approved. It is withdrawn (Decision 1).
- 2026-10-14: Plan Writer gobby#16764 rewrote the plan under Orchestrator
  gobby#15977's N1 ruling, option (1).
  - The plan adds node-commands contract version 4 `checkout.read`, the
    node handler, the hub seam and git routing (P1), and eight hub-site
    leaves (P2).
  - Confinement is node-derived from the cwd marker, which corrects writer
    message `0c04009c`.
  - The plan adds a live proof (3.1) in place of the old deferral.
  - The site sweep, symbols and line counts were verified read-only on
    `0.5.0` at `3e4d426ddf`.
  - The draft is narrative only, with no M1.
- 2026-10-14: review repairs for Plan Adversary gobby#16791's findings on
  `a93896a4ba`.
  - HE-1: Decision 4's admission predicate copies
    `_is_project_managed_path`'s branch order, including the temp scratchpad
    rejection, and then applies the home exclusion. 1.2's fixture tree,
    1.2.1 and 1.2.3 follow that predicate.
  - HE-2: callers flagged by consumer coverage are named under Consumers
    unchanged on 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 2.6, 2.7 and 2.8, each with a
    no-edit reason.
  - HE-3: 1.1's invalid corpus pins every foreign field on both query
    forms.
  - 1.4 flags the 1.4.5 fix as a deliberate local behavior change, as
    #15977 asked.
- 2026-10-14: V1 consensus. Plan Writer gobby#16764 and Plan Adversary
  gobby#16791 agree on this plan at `331dd26498`, with no open findings.
  HE-1, HE-2 and HE-3 are closed, and bare validation exits 0 with no
  warnings.
- 2026-10-15: Plan Writer gobby#16490 withdraws the M1 stamped at
  `0dfa754f8e` before a framing-only Decision Record addition that
  Orchestrator gobby#15977 directed.
- 2026-10-15: the Decision Record adds rejected option (4), node-local rule
  evaluation against a cached ruleset, in the wording Orchestrator
  gobby#15977 accepted, plus one sentence that #24151's Rust port keeps the
  engine on the hub. Josh accepted hub-side evaluation on 2026-10-15. Scope,
  leaves, Targets and acceptance are unchanged.
- 2026-10-15: V1 consensus. Plan Writer gobby#16490 and Plan Adversary
  gobby#16791 agree on this plan at `62d13774c7`, with no open findings.
  Option (4) is a closed rejection, and bare validation exits 0.
- 2026-10-15: after Josh's pushback (hub and node are one gdaemon binary;
  definitions are cached and synced), option (4) is revised: it drops the
  "no engine on the node" reason and the sentence that #24151 keeps the engine
  on the hub, notes that definitions could be cached on a node, and stays
  rejected on per-call hub state, decision-window staleness, lost writes and
  ROADMAP decisions 13 and 17. Josh approved the revised text. Scope, leaves,
  Targets and acceptance are unchanged.
- 2026-10-15: consumer coverage flags three tests that landed after the
  2026-10-14 consensus. 1.4's Consumers unchanged adds
  `tests/storage/test_task_close_reviews.py`. 2.6's adds
  `tests/mcp_proxy/tools/test_claim_task.py` and
  `tests/workflows/test_session_variable_mutation_scope.py`, and 2.6's
  planned verification runs the latter, which calls `_live_path_owners`
  directly. Targets and acceptance are unchanged.
- 2026-10-15: V1 consensus. Plan Writer gobby#16490 and Plan Adversary
  gobby#16791 agree on this plan at `d973d0bbcc`, with no open findings.
  The revised option (4) is a closed rejection, and bare validation exits 0
  with no warnings.

## V2: Verification
`kind: verification`

These are completion gates for the implementation. Except for plan
validation, none has run yet. Each runs after the leaves it names land, and
each must hold before #23277 closes.

- Plan validation must pass: `uv run gobby plans validate .gobby/plans/hook-envelope-file-facts.md`.
  This is the only planning-time check.
- After 1.1 and 1.2 land, both contract suites and the handler suite must
  pass:
  - `CARGO_BUILD_JOBS=3 cargo nextest run -p gobby-daemon -E 'binary(node_command_corpus) + binary(node_command_parity) + binary(node_checkout_read)'`;
  - `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/hub/ -v`.
- 1.2 must carry a Security Reviewer LAND verdict before it lands.
- After P2 lands, the focused suites of 1.3, 1.4 and 2.1 to 2.8 must pass
  together against the test hub. `uv run ruff check src/`,
  `uv run ruff format --check src/` and `uv run mypy src/` must be clean.
  The test-type audit must report no new findings for the changed tests.
- After every leaf lands, 3.1 must pass on the live pair, and no test may
  have read `~/.gobby/bootstrap.yaml` or the user's daemon database.
- Once the release with these leaves is promoted and the daemon restarts, a
  local session's hook-path behavior must be unchanged. The local suites
  named in each leaf's verification must pass unchanged, except for the
  1.4.5 fix.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: 'Node-commands contract version 4: `checkout.read`'
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: `CONTRACT_VERSION` is 4 in the Rust models. Each corpus
    `checkout.read` command and reply parses into its v4 Rust type, with `v` equal
    to `CONTRACT_VERSION`, and re-encodes to the same bytes. test: `crates/gdaemon/tests/node_command_parity.rs::v4_checkout_read_round_trips`.

    1.1.2: `CONTRACT_VERSION` is 4 in the Python models, and the same messages parse
    into their v4 Python models and re-encode to the same bytes. test: `tests/hub/test_node_command_parity.py::test_v4_checkout_read_round_trips`.

    1.1.3: Both sides refuse every new `invalid` entry. test: `crates/gdaemon/tests/node_command_parity.rs::invalid_messages_fail`.

    1.1.4: The Python models refuse every new `invalid` entry. test: `tests/hub/test_node_command_parity.py::test_invalid_messages_fail`.

    1.1.5: The contract specifies `checkout.read` with its payload, reply, limits,
    node rules, error codes and stage rule. file: `docs/contracts/node-commands.md`,
    section "v4 commands", subsection `checkout.read`, and "Node duties".'
  labels:
  - covers:hook-envelope-file-facts:1.1:1.1.1
  - covers:hook-envelope-file-facts:1.1:1.1.2
  - covers:hook-envelope-file-facts:1.1:1.1.3
  - covers:hook-envelope-file-facts:1.1:1.1.4
  - covers:hook-envelope-file-facts:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Node `checkout.read` handler
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.2.1: For every golden case, `checkout.read` answers the
    expected `root`, `project_id`, `home`, `temp_dir` and entries (`resolved`, `kind`,
    `checkout_root`, `git_common_dir`, `project_root`, `project_id`, `data`, `truncated`).
    Every successful reply carries the node''s canonical `home` and `temp_dir`, and
    every `outside` entry carries its non-null `resolved`. test: `crates/gdaemon/tests/node_checkout_read.rs::facts_match_golden`.

    1.2.2: The Python oracle produces every golden entry outside the node home, so
    the hub''s local helpers and the node agree. test: `tests/hub/test_checkout_read_facts_golden.py::test_golden_matches_local_helpers`.

    1.2.3: Admission follows Decision 4''s predicate and home exclusion. These answer
    `kind: "outside"` with null data: a path in an unrelated repository, a symlink
    inside R0 that points into that repository, a `..` path that resolves outside
    every repository, a linked worktree of R0''s repository under the temp scratchpad
    `<fixture>/tmp/agent-scratchpad-1/`, and the isolated node home''s `bootstrap.yaml`.
    These are admitted: a linked worktree of R0''s repository under `<home>/worktrees/`,
    a symlink inside R0 that points into the linked worktree, and a `..` path that
    resolves there. test: `crates/gdaemon/tests/node_checkout_read.rs::confinement_withholds_outside_paths`.

    1.2.4: These answer `invalid_payload`, `stage: "none"`, and start no process:
    a refused subcommand (`commit`, `checkout`, `config`), a `-c` other than `core.fsmonitor=false`,
    and each refused option. A `-C`, `--git-dir` or `--work-tree` naming an outside
    path answers `outside`. A cwd with no marker above it answers `no_project_root`.
    test: `crates/gdaemon/tests/node_checkout_read.rs::refusals_run_nothing`.

    1.2.5: git does not see an inherited `GIT_DIR` or `GIT_INDEX_FILE`, and runs with
    `GIT_OPTIONAL_LOCKS=0`, leaving no `index.lock` behind. stdout over 8 MiB is cut
    with `truncated: true`. git past `timeout_ms` is killed and answers `timed_out:
    true`. test: `crates/gdaemon/tests/node_checkout_read.rs::git_runs_scrubbed_and_bounded`.

    1.2.6: `read_limit` caps each file''s data, and a reply whose files exceed 8 MiB
    cuts later entries with `truncated: true`. test: `crates/gdaemon/tests/node_checkout_read.rs::reads_respect_limits`.'
  labels:
  - covers:hook-envelope-file-facts:1.2:1.2.1
  - covers:hook-envelope-file-facts:1.2:1.2.2
  - covers:hook-envelope-file-facts:1.2:1.2.3
  - covers:hook-envelope-file-facts:1.2:1.2.4
  - covers:hook-envelope-file-facts:1.2:1.2.5
  - covers:hook-envelope-file-facts:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Hub checkout-read seam and delivery origin
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.3.1: A delivery whose `input_data.machine_id` names another
    machine reports `node_origin()` true and that machine from `event_machine_id()`.
    This holds in the adapter call, in a coroutine run on another loop, and in an
    offloaded thread. A hub-machine or missing machine id reports false and the hub''s
    machine. The scope resets after the delivery. test: `tests/hooks/test_checkout_reads.py::test_delivery_scope_marks_node_origin`.

    1.3.2: `path_facts` sends one `checkout.read` files query to the event''s machine,
    with the cwd, paths and read limit, and returns typed entries. A repeat for memoized
    paths in the same delivery sends nothing. test: `tests/hooks/test_checkout_reads.py::test_path_facts_queries_node_once_per_delivery`.

    1.3.3: Each transport code (`node_routing_unavailable`, `node_reply_lost`, `node_offline`,
    `node_timeout`, `channel_closed`) and a synchronous call on the daemon loop''s
    thread raises `CheckoutReadUnavailable` naming the code and machine. Every later
    node read in that delivery raises at once without sending. `no_project_root` does
    not latch. test: `tests/hooks/test_checkout_reads.py::test_unavailable_answers_latch_per_delivery`.

    1.3.4: `run_git` spawns local git exactly as before for a local delivery. For
    a node delivery it sends a git query and maps the exit code, stdout and stderr,
    a timeout to `subprocess.TimeoutExpired`, and truncation to `CheckoutReadUnavailable`
    with code `truncated`. test: `tests/hooks/test_checkout_reads.py::test_run_git_routes_by_origin`.

    1.3.5: After a node delivery that read file data and git output, neither appears
    in `HookEvent.data`, `HookEvent.metadata`, captured log records, session variables
    or broadcaster payloads. test: `tests/hooks/test_checkout_reads.py::test_node_content_stays_in_delivery_memo`.'
  labels:
  - covers:hook-envelope-file-facts:1.3:1.3.1
  - covers:hook-envelope-file-facts:1.3:1.3.2
  - covers:hook-envelope-file-facts:1.3:1.3.3
  - covers:hook-envelope-file-facts:1.3:1.3.4
  - covers:hook-envelope-file-facts:1.3:1.3.5
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Node-origin git runs on the node
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '1.4.1: In a node delivery, `daemon_git.run` and `daemon_git.status`
    send git queries to the node and return `GitOk`, `GitFailed` or `GitTimeout` from
    its exit code, output and timeout, and no local git process starts. test: `tests/hooks/test_node_origin_git.py::test_node_origin_git_runs_on_node`.

    1.4.2: Identical `status` calls from deliveries of two machines never share one
    in-flight result. Identical calls from the same machine still coalesce. test:
    `tests/hooks/test_node_origin_git.py::test_status_coalescing_is_machine_scoped`.

    1.4.3: An unavailable node, a truncated reply and an `env` override each yield
    `GitFailed` whose detail carries the diagnostic, and the env case sends nothing.
    test: `tests/hooks/test_node_origin_git.py::test_unanswered_node_git_fails_with_diagnostic`.

    1.4.4: In a node delivery, `_git_out`, `_git_ignored` and `_positional_checkout_is_path`
    send their git and existence checks to the node and start no local process. Local
    deliveries spawn as before. test: `tests/hooks/test_node_origin_git.py::test_sync_git_sites_route_by_origin`.

    1.4.5: For a local `git checkout <file>`, a relative operand that exists under
    the command''s `cwd` but not under the daemon''s process cwd proves path mode.
    test: `tests/hooks/test_node_origin_git.py::test_positional_checkout_joins_relative_operand_to_cwd`.'
  labels:
  - covers:hook-envelope-file-facts:1.4:1.4.1
  - covers:hook-envelope-file-facts:1.4:1.4.2
  - covers:hook-envelope-file-facts:1.4:1.4.3
  - covers:hook-envelope-file-facts:1.4:1.4.4
  - covers:hook-envelope-file-facts:1.4:1.4.5
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Node events take their project from the node's checkout rows
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.1.1: A node event with no explicit or session project takes
    the project of the node''s longest checkout row enclosing its cwd. A hub marker
    at the same path is never read, and no checkout row is registered. test: `tests/hooks/test_node_origin_project.py::test_node_cwd_resolves_from_node_rows`.

    2.1.2: A node event whose cwd matches no row of its machine raises the no-project
    `ValueError` naming `checkout_read_unavailable: no_checkout_row` and the machine.
    A node event with an unusable cwd is skipped without the daemon''s current-context
    project. test: `tests/hooks/test_node_origin_project.py::test_node_cwd_without_row_fails_closed`.

    2.1.3: For a node event, `_resolve_project_path` returns the node''s checkout
    row root when the worktree root is absent or equals it, and the worktree root
    otherwise. It never calls `require_local_machine_id` or resolves a path on the
    hub. test: `tests/hooks/test_node_origin_project.py::test_node_project_path_uses_node_row`.

    2.1.4: After the move, the no-repo project set and predicate are unchanged. test:
    `tests/workflows/test_workflow_hooks.py::TestProjectPathResolution.test_no_repo_project_ids_include_constants_and_legacy_literals`.

    2.1.5: A remote event whose session ids name another machine''s rows takes its
    project from its own machine''s checkout row for its cwd, never from those rows.
    test: `tests/hooks/test_session_binding.py::test_project_context_skips_foreign_rows`.'
  labels:
  - covers:hook-envelope-file-facts:2.1:2.1.1
  - covers:hook-envelope-file-facts:2.1:2.1.2
  - covers:hook-envelope-file-facts:2.1:2.1.3
  - covers:hook-envelope-file-facts:2.1:2.1.4
  - covers:hook-envelope-file-facts:2.1:2.1.5
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Path primitives and write scope use node facts
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.2.1: For a node event, tool paths, the project root, checkout
    roots, git common dirs and the managed-path verdict come from the node''s facts.
    A hub file, symlink or `.git` at the same paths changes none of them. test: `tests/hooks/test_node_origin_path_scope.py::test_node_path_primitives_use_node_facts`.

    2.2.2: `classify_git_mutation` for a node `git` command takes its checkout root
    from the node, and a local command still resolves on the hub. test: `tests/hooks/test_node_origin_path_scope.py::test_git_mutation_checkout_root_by_origin`.

    2.2.3: `write_paths_within` admits a node write under an allowed project directory
    by the node''s resolved path, refuses one whose node path escapes through a symlink,
    and answers `False` when the node is unavailable. test: `tests/hooks/test_node_origin_path_scope.py::test_node_write_scope`.

    2.2.4: In plan mode, a node write to the project''s `.gobby/plans/x.md` and to
    the provider scratch directory under the node''s home is allowed. A node source
    file is refused, and an unavailable node refuses every write. test: `tests/hooks/test_node_origin_path_scope.py::test_node_plan_mode_writes`.

    2.2.5: An unavailable node during normalization of a node `Write` blocks the critical
    hook with the diagnostic in its reason. test: `tests/hooks/test_node_origin_path_scope.py::test_unavailable_normalization_fails_closed`.'
  labels:
  - covers:hook-envelope-file-facts:2.2:2.2.1
  - covers:hook-envelope-file-facts:2.2:2.2.2
  - covers:hook-envelope-file-facts:2.2:2.2.3
  - covers:hook-envelope-file-facts:2.2:2.2.4
  - covers:hook-envelope-file-facts:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Monolith guard reads node files
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.3.1: A node `Write` that takes a node file from 990 to 1,000
    lines is listed, and one that keeps it at 995 lines is not. A different file at
    the same path on the hub does not change either answer. test: `tests/workflows/test_node_origin_monolith.py::test_node_projection_reads_node_file`.

    2.3.2: The ledger recount for a node session counts the node''s files in one batched
    read per 256 paths. test: `tests/workflows/test_node_origin_monolith.py::test_node_ledger_recount_batches`.

    2.3.3: An unavailable node, or a truncated read under the limit, lists the guard
    path with the diagnostic, so the rule blocks. test: `tests/workflows/test_node_origin_monolith.py::test_unanswered_node_lists_diagnostic`.'
  labels:
  - covers:hook-envelope-file-facts:2.3:2.3.1
  - covers:hook-envelope-file-facts:2.3:2.3.2
  - covers:hook-envelope-file-facts:2.3:2.3.3
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Rust TDD evidence reads node files
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.4.1: A node edit that adds only a `#[test]` function inside
    the node file''s `cfg(test)` module is test-only, judged from the node''s text
    while the hub has no file at that path. test: `tests/workflows/test_node_origin_rust_tdd.py::test_node_rust_test_edit_reads_node_text`.

    2.4.2: An unavailable node, or a truncated prior text, makes the edit not test-only,
    including a full-content `Write`. test: `tests/workflows/test_node_origin_rust_tdd.py::test_unanswered_node_is_not_test_only`.'
  labels:
  - covers:hook-envelope-file-facts:2.4:2.4.1
  - covers:hook-envelope-file-facts:2.4:2.4.2
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Code navigation reads node files
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.5.1: A whole-file read of a 40-line node file counts as
    narrow, and of a node file over the scan limit as broad, from the node''s bytes
    while the hub has no file at that path. test: `tests/hooks/test_node_origin_code_navigation.py::test_node_line_count_reads_node_bytes`.

    2.5.2: The ignore decision for a node path uses the node''s checkout root and
    the node''s `.gobby/gcode.json` allowlist. test: `tests/hooks/test_node_origin_code_navigation.py::test_node_ignore_decision_uses_node_config`.

    2.5.3: An unavailable node makes the read unverified (broad) and the allowlist
    empty. test: `tests/hooks/test_node_origin_code_navigation.py::test_unanswered_node_keeps_enforcement`.'
  labels:
  - covers:hook-envelope-file-facts:2.5:2.5.1
  - covers:hook-envelope-file-facts:2.5:2.5.2
  - covers:hook-envelope-file-facts:2.5:2.5.3
  tdd: true
  source_section: '2.5'
  implementation_domain: backend
- title: Node edits record the node's checkout, and ledger owners are machine-scoped
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.6.1: A node edit attributes `(project_root, relative path)`
    from the node''s facts, and a hub marker at the same path is never read. A node
    path outside the project, or in another project, is not attributed. test: `tests/hooks/test_node_origin_edits.py::test_node_edit_pair_uses_node_facts`.

    2.6.2: With the node unavailable, a node edit under the node''s checkout row is
    attributed by its lexical path, and one outside the row is not. test: `tests/hooks/test_node_origin_edits.py::test_unavailable_node_edit_pair_is_lexical`.

    2.6.3: A node edit triggers no hub code-index update. test: `tests/hooks/test_node_origin_edits.py::test_node_edit_skips_hub_code_index`.

    2.6.4: A hub session and a node session hold the same path in checkouts with the
    same absolute root. Each machine''s ownership check counts only its own sessions.
    test: `tests/hooks/test_node_origin_edits.py::test_live_path_owners_are_machine_scoped`.'
  labels:
  - covers:hook-envelope-file-facts:2.6:2.6.1
  - covers:hook-envelope-file-facts:2.6:2.6.2
  - covers:hook-envelope-file-facts:2.6:2.6.3
  - covers:hook-envelope-file-facts:2.6:2.6.4
  tdd: true
  source_section: '2.6'
  implementation_domain: backend
- title: Shell classification reads node executables
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  validation_criteria: '2.7.1: A node `./run.sh` is classified from the node''s shebang
    and script body, and a node Mach-O header is classified as native, while the hub
    has no file at either path. test: `tests/hooks/test_node_origin_shell.py::test_node_executable_classified_from_node_bytes`.

    2.7.2: A node `cp a.txt dest` treats `dest` as a directory only when the node
    reports `dir`. test: `tests/hooks/test_node_origin_shell.py::test_node_transfer_destination_kind`.

    2.7.3: An unavailable node while classifying a node shell command blocks the critical
    hook with the diagnostic. test: `tests/hooks/test_node_origin_shell.py::test_unanswered_node_shell_fails_closed`.'
  labels:
  - covers:hook-envelope-file-facts:2.7:2.7.1
  - covers:hook-envelope-file-facts:2.7:2.7.2
  - covers:hook-envelope-file-facts:2.7:2.7.3
  tdd: true
  source_section: '2.7'
  implementation_domain: backend
- title: Node events skip hub-only effects
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '2.8.1: A node `WorktreeCreate` or `WorktreeRemove` answers
    allow, and creates or removes no hub worktree or directory. test: `tests/hooks/test_node_origin_dispositions.py::test_node_worktree_hooks_touch_nothing_on_hub`.

    2.8.2: Node Claude and Grok spill notices keep their text, and a node Codex payload
    recovers no transcript result, even with matching files on the hub. test: `tests/hooks/test_node_origin_dispositions.py::test_node_provider_files_are_not_read`.

    2.8.3: A node shell command gets no rtk rewrite, and runs as written. test: `tests/hooks/test_node_origin_dispositions.py::test_node_shell_skips_rtk_rewrite`.

    2.8.4: A node event matching `impeccable/design-detector` spawns no process. test:
    `tests/hooks/test_node_origin_dispositions.py::test_node_run_command_spawns_nothing`.

    2.8.5: A node-owned session''s turn end with no task disposition gets `()` from
    `_terminal_failures`, and no node read is sent. test: `tests/hooks/test_node_origin_dispositions.py::test_node_found_work_terminal_evidence_is_empty`.'
  labels:
  - covers:hook-envelope-file-facts:2.8:2.8.1
  - covers:hook-envelope-file-facts:2.8:2.8.2
  - covers:hook-envelope-file-facts:2.8:2.8.3
  - covers:hook-envelope-file-facts:2.8:2.8.4
  - covers:hook-envelope-file-facts:2.8:2.8.5
  tdd: true
  source_section: '2.8'
  implementation_domain: backend
- title: Live node-origin checkout reads
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '2.1'
  - '2.2'
  - '2.3'
  validation_criteria: '3.1.1: With the node connected, a node-origin `Write` that
    takes `src/app.py` to 1,000 lines is blocked by `require-decompose-monolith-before-threshold-write`,
    and one that keeps it at 995 lines is allowed. test: `tests/e2e/test_hub_node_checkout_read.py::test_node_file_drives_monolith_gate`.

    3.1.2: After the node daemon stops, the 995-line `Write` is blocked with `checkout_read_unavailable`
    and the node''s machine, although `src/app.py` is readable at that path on the
    hub''s disk. test: `tests/e2e/test_hub_node_checkout_read.py::test_offline_node_fails_closed`.'
  labels:
  - covers:hook-envelope-file-facts:3.1:3.1.1
  - covers:hook-envelope-file-facts:3.1:3.1.2
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
```
