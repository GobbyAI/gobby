# Runner lifecycle inventory

Status: plan, not a completion claim. The inventory describes the 0.9.0 base commit
(`base.commit` in the fixture). It records what the Python runner starts, stops and
depends on today, so that later checkpoints can be measured against it.

## Fixtures and pins

- `tests/contracts/runner_lifecycle.json` is the static inventory. Its `inventory_version`
  is `1`. Any change to the fixture or to a bundled runner module that moves an entry
  updates the fixture in the same commit.
- `tests/contracts/test_runner_lifecycle_inventory.py` pins the inventory against the source
  with `ast` and `inspect`. The pins name the static facts (phase order, hub-only sets,
  creation sites, periodic spawns, cancel owners, service receivers, raise and exit sites,
  identities and errors) and the digest of `GobbyRunner.create`.
- `tests/contracts/runner_census.json` is the runtime census of one isolated boot. It is
  recorded by the e2e census (see the census section below).

The fixture and the doc are changed in the same commit as the code they describe. A commit
that moves a create site, a start or stop receiver, an identity or an error without
updating the inventory fails the pins.

## Vocabularies

Stage, for tasks (`stage`):

- `preflight`: started before `GobbyRunner.create` runs (for example the supervisor watcher).
- `construct`: started while `GobbyRunner.create` runs its five phases.
- `serve`: started after `create` returns, while the daemon serves.

Stage, for phases (`stage`):

- `construct`: one of the five `InitPhase` values, owned by `GobbyRunner.create`.
- `subsystems`: a literal `timed_startup_phase` or `hub_only_phase` call in `init_subsystems`.
- `recovery`: a literal phase call in `_recover_after_restart`.
- `shutdown`: the stop path. Stops are recorded as stop owners, not as phases.

Next owner (`next_owner`), the work item that will own the entry next:

- `R1`: process identity and the supervisor handshake.
- `R2`: construction and readiness.
- `R3`: recovery.
- `R4`: periodic, cron, pipeline and system automation.
- `R5`: hook, MCP, WebSocket and chat.
- `R6`: the stop order.

`next_owner` is routing, not completion: it names where the entry lands, not that it is done.

## Schema

- `phases`: `id`, `stage`, `owner`, `hub_only`, `next_owner`, `seams`. Construct phases are
  owned by `GobbyRunner.create` (the statements stay in `create`). `hub_only` ids equal
  `HUB_ONLY_STARTUP_PHASES`.
- `seam_notes` (construct phases only, R36): one entry per name in `seams`, holding `module` and
  `qualname`, the definition the seam resolves to (checked by AST). An `unobserved_reason`
  (non-empty text) is present exactly when no construct-stage census row has that definition as
  its creator. A census row is construct-stage when an inventory task with stage `construct`
  lists its key, since thread rows carry no stage. Executor rows are not task creators.
  The union pin counts the functions that `GobbyRunner.create`'s phase statements name (imports,
  top-level functions of `runner.py`, and `self.<method>` calls; receiver chains are left to the
  marker pins below) united with the construct task and thread creators in the census. Only the
  first part is derived from source. A seam that neither a statement names nor the census creates
  fails that pin, so adding one is a deliberate change to the rule.
- `unverified_reason` (construct phases only, R45): non-empty text on a transitive seam that no
  `phase_scope` block reaches in source. It is present exactly when no direct seam reaches the
  seam, and it is never present on a direct seam or on a reached seam.
- Phase attribution (R45, the marker pins). Each `phase_scope(InitPhase.X)` block of
  `GobbyRunner.create` is read by AST, with the marker itself excluded. The functions a block names
  are its direct seams: a call, a callable passed to `_run_blocking_startup_phase` or
  `asyncio.to_thread`, or a method reached through a typed receiver such as
  `self.config_runtime.start()`. Each direct seam is listed under exactly that phase, and a listed
  seam that some block names is listed only under a phase whose block names it.
- A transitive seam, one that no block names, is listed under the first construct phase whose
  direct seams reach it. Reach follows named references in gobby source: a call, a callable
  argument, or a method read on a receiver typed by `self`, a parameter annotation, a class-body
  annotation, or a local whose every assignment names one class. A method resolves on its class or its bases,
  and a class call resolves to `__init__`. Reach does not follow `getattr` dispatch, dict-typed
  receivers, `isinstance` narrowing, or call results. The reach is an over-approximation of the named references in
  source, so a seam listed under a later phase than its first reach fails the pin.
- `tasks`: `id`, `creator` (the scope that calls `create_task`, `ensure_future`, or
  `threading.Thread`), `spawned_from` (the scope that calls the periodic `spawn` helper),
  `target` (the coroutine or callable the task runs), `runner_attr`, `name`, `stage`,
  `hub_only`, `cancel_owner`, `condition`, `next_owner`, `cadence`, `one_shot`, `census`,
  `observed`, `debt`, `disabled_in_profile` (a recurring entry the census profile turns off, R28),
  and `runtime_attributed` (the creator is the frame a construct task reports at runtime, R29, not
  a static call site, so the static site pins skip it).
- `services`: `id`, `start_owner`, `stop_owner`, `stage`, `hub_only`, `next_owner`. A service
  with an empty `stop_owner` has no stop in scope. Each stop owner's body names the service's
  attribute. A start owner's body names it too, or reaches through named references a function that
  stores `runner.<attr>`, because the runner's create phases assign storage and memory services in
  init helpers (R58, deviation D1). A non-gobby owner is one of the allow-listed library calls.
- `identities`: `name`, `source` (`env`, `pipe`, `file`, `database`, `process`), `producer`,
  `consumers`, `current_owner` (`rust` or `python`, the component that issues the identity
  on the base commit). A Python producer spells its identity: the def's own name, a string
  constant in its body, or a module constant holding the identity that its body reads (R58).
- `errors`: `raised_by`, `exception` or `exit_code`, `effect` (`refusal exit 78`,
  `crash exit 1`, `clean exit 0`, `clean return`, `propagates`).
- `outside_placements`: `key`, `stop_owner`, `request_driven`, `timing_dependent`, `debt`. One
  entry per observed key that no inventory task owns; the recorder reads it to route the key.
- Census rows (`tests/contracts/runner_census.json`): a `tasks` row holds `key`, `creator`,
  `target`, `names`, `stage`, `cadence`, `cancel_owner`, `observed` and `debt`; a `threads` row
  holds `key`, `creator`, `name`, `daemon`, `cancel_owner` and `debt`; an `executors` row holds
  `creator`, `prefix`, `max_workers` (as passed), `stage`, `cite`, `submitters`, `cancel_owner`
  and `debt`; an `outside_runner_lifecycle` row holds `key` (null for a static cite),
  `stop_owner` and `debt`, plus the placement fields for a placed key.
  The owner and debt of a task, thread or executor row are the owner and debt of its inventory
  task, or of its outside placement when no task owns it. The pins compare them row by row.
- `create_body`: the sha256 of the normalized AST of `GobbyRunner.create`'s body, computed
  as in the digest test. The digest proves that the create body is unchanged at C1b.

## Cancel owner and debt

`cancel_owner` is the scope that cancels, or awaits to completion, the task. A task whose
owner is absent records `cancel_owner: null` and `debt: "debt:R0"`. Defects found by the
inventory are recorded, not fixed, in R0 (R12). The debt lines live in the port record.

## Cadence

`cadence` is `{seconds, param, default, config_key, wake, source}`. Recurring tasks carry a
cadence. Config-driven cadence stores `config_key` and `default`. `source` is a `path:line`
cite. A `wake` of `event` means the loop can run early on an event.

## Census and the same-commit rule

The runtime census lists what one isolated boot actually creates. The census section below
states its record, key and skip rules.

When a commit adds, moves or removes a task, a service, an identity, an error, or a phase,
the fixture, the doc and the census change in that same commit. The census never changes
after it is frozen, except through a new `inventory_version`.

## Runtime census

`tests/contracts/runner_census.json` is recorded from one isolated boot of the genuine runner
(`tests/e2e/task_census_bootstrap.py`, entered through `gobby.runner._standalone_main`). The
bootstrap refuses to run unless `GOBBY_TEST_PROTECT=1`, `DATABASE_URL` names the isolated test hub,
`GOBBY_HOME` is set and is not the operator home or its `.gobby` directory, and the home's
`bootstrap.yaml` `database_url` names the test hub too. The hub check compares the parsed DSN
parts, not the string: the user, the password, a loopback host (`127.0.0.1` or `localhost`), port
`60892`, and a database that is `gobby_test` or an isolated `gobby_test_*` database. The query
string is ignored. The guard reads the home's `bootstrap.yaml` only after the operator-home check,
and it reads it the way the daemon does (`read_present_bootstrap_mapping` in
`src/gobby/config/bootstrap_io.py`): UTF-8, then `yaml.safe_load`, so a repeated `database_url` key
keeps its last value, which is the value the daemon uses. A file that does not decode, does not
parse, or does not parse to a mapping is refused. A refusal never echoes a URL. The bootstrap never
sets `GOBBY_ALLOW_WORKTREE_DAEMON`.

Host: the fixture was recorded on macOS (darwin) under CPython 3.14 (`RECORDED_PLATFORM` and
`RECORDED_PYTHON` in `tests/e2e/task_census_recorder.py`). The census e2e module skips elsewhere, on
any other platform or minor version, with a reason that names that host. The fixture names no
interpreter, so the recorded minor version is the named constant and is not part of the profile.
The Linux, Windows and other-interpreter censuses are unrecorded.

Record: each task or thread creation is one JSONL record in `$GOBBY_HOME/logs/task-census.jsonl`
with `kind`, `stage` (`preflight`, `construct`, `serve`), `creator` (the nearest `gobby.` frame),
`target` and `name`. Stage markers and one environment record bracket the boot.

The fixture's top-level sections are `profile` (the run mode, the config profile with its disabled
features, the host inputs and the bootstrap), `environment` (the `GOBBY_*` keys the boot reads or
sets), `tasks`, `threads`, `executors`, `unobserved` (recurring inventory entries that no boot
observed, each with creator, target, cadence, condition and cites), `outside_runner_lifecycle` and
`excluded`.

Key: `(kind, creator, target)`. The line number and the normalized name are not part of the key,
so a line move never changes the census. Keys are listed in the inventory `census` fields.
An executor key is `executor|creator|prefix`. It carries no worker count, because a defaulted
worker count is derived from the host and a count in the key would let a host change flip an
owner. The size is recorded in the executor row as passed. A key listed by two inventory tasks
is refused by the recorder, and the pins require every observed key to have exactly one owner.

Host inputs: the boot pins the CPU count that sizes the defaulted executors
(`CENSUS_CPU_COUNT`, recorded in the profile), and it puts a stub `agy` with a fixed version line
on `PATH` (`CENSUS_AGY_STUB`), so the version probe spawns the same processes on every host. It
also pins the asyncio child watcher to the threaded one (`CENSUS_CHILD_WATCHER`): the watcher is
chosen when the loop is built, and a kernel that offers pidfd would start no `_do_waitpid` thread,
so the thread rows would differ by host. The Postgres capacity of the hub also sizes the database
executor. The census needs at least 64
usable connections on the isolated hub; a smaller hub changes the recorded `max_workers` and fails
the fixture match loudly.

Timing-optional submitters: the loop's default executor (`loop-default`) lists the frame that
submitted each `asyncio.to_thread` call, once that call has run. Two background paths can add such
a frame after the fixture's boot: the attention monitor's poll pass (`InteractiveAttentionMonitor`,
first tick 5 seconds after its lifespan start), and the terminal-host upgrade check
(`HostUpgradeCoordinator._installed_sha256`), which `HostUpgradeCoordinator.observe` reaches on each
host adoption and health ping. The census is read 2 seconds after recovery-ready, so the monitor
pass lands before the read on some boots, most often when host load slows the boot. A boot creates
no task or thread for these calls; only the submitter list changes.

Evidence: before the rule, the fixture match passed in 9 of 9 quiet runs (8 isolated, and 1 combined
with the worker and contract tests) and failed in 2 of 2 runs taken while eight busy-loop processes
shared the host. In both failing boots the diff was the same and sat on the live side only: four
monitor helper submitters (`_check_attention_panes`, `_clear_attention_if_current`,
`_list_active_agent_sessions`, `_list_interactive_sessions`) and the host-upgrade submitter. There
was no other row, and the sandbox submitter matched on both sides. The monitor pass found one
interactive session row with no live terminal. Each pytest process has its own database, which is
reset to the canonical seed before each boot, and a literal search of `src/gobby` found session
inserts only in runtime storage code, so the row was created during the boot. Its creator is not
identified. Other agents' concurrent runs were not measured; only the synthetic load was. A
verifier's report of a sandbox difference on one run, with no recorded direction, is not reproduced;
the sandbox submitter stays exact.

The rule: `TIMING_OPTIONAL_SUBMITTERS` in `tests/e2e/task_census_recorder.py` names five
submitters that may be absent from a boot or present in it: the four monitor helpers above and the
host-upgrade check. `timing_comparison_view` removes them from the `loop-default` executor row on
both sides before the byte comparison. It touches no other row, no other submitter and no other
executor. Three further helpers of the same pass (`_enter_interactive_lifecycle_wait`,
`_resolve_interactive_lifecycle_wait`, `_settle_local_command`) run only after a live terminal row
yields pane output, which the census boot never creates, so they are not named; if one appears, the
match fails. A contract test pins that no named submitter is in the fixture's list, so the rule
only admits extras. Another checks that each named submitter is still defined in its source, so a
renamed helper fails the contract. The fixture is not edited.

Skip: the skip rule (GF-14) drops a creation whose stack has no `gobby.` frame, so it is not
recorded (harness-owned threads are listed under `excluded`). A pool worker start is not recorded;
the owned executor and the submitting frames are recorded instead, and the loop's default executor
is keyed `loop-default`.

Placement: every observed key is placed exactly once. An inventory task lists its keys in `census`.
A key that no inventory task owns is listed in the fixture's `outside_placements` with its stop
owner, `request_driven`, `timing_dependent` and `debt`, and the recorder emits it under
`outside_runner_lifecycle`. The health-monitor timer is keyed by its start creator
(`CREATOR_ALIASES`), because either of its two arming frames can be the one a boot observes.

Status: `tests/contracts/runner_census.json` is frozen. It was recorded from two clean boots, and
the two recordings were byte-identical. Do not edit it by hand. To re-record after an inventory
change, run `tests/e2e/test_runner_task_census.py::test_census_recording` twice, each time with
`GOBBY_CENSUS_RECORD_TO` set to a path outside the real home and outside `tests/contracts`, which the
recorder refuses (without the variable, each run writes
to its own pytest `tmp_path`, which is also outside the real home), `cmp` the two files, and copy one over
`tests/contracts/runner_census.json` in the same commit as the inventory change. A census change
that is not a re-record of the same boot requires a new `inventory_version`.
