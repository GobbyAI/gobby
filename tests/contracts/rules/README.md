# Rule-pass corpus

This directory holds the committed contract for one rule pass: `RuleEngine.evaluate`
run over synthetic rules, plus the normalizer functions the rule conditions read.

- `manifest.json` names the schema version, the case kinds, the seams the recorder wraps,
  the required coverage sets and the platform record.
- `cases/*.json` hold the rule-pass cases. Each case defines synthetic rules, fires one
  event, and carries an `expect` block: outcome class, eager variable delta, the staged
  receipt payload, the effect log, the audit records, the non-default response fields and
  any skipped groups. The eager variable delta holds every key the pass added or changed,
  a key newly written to `null` included; a key the pass deleted is listed in the optional
  `eager_variable_deletions`. `cases/variables.json` holds the two cases that prove this
  encoding (a `null` write to a new key, and the turn-start reset that deletes a flag).
- A case of kind `seam_caller` drives one real caller of a seam instead of the rule engine,
  because the engine never reaches that seam: `background_delivery` runs the background
  run-command delivery, `tool_result_offload` runs the MCP proxy's result offload and
  `inline_run_command` applies one `run_command` effect (see "Engine-path keys"). Its
  `expect` adds the caller's `returned` value.
- `normalize_vectors.json` holds the normalizer vectors. Eleven entry points each have a
  vector, and every cross-module function in `normalize_modules.json` has one. Each of the
  151 listed functions whose parameters are all builtin types (`str`, `int`, `bool`, `bytes`,
  containers of those, or `Optional` of those) has at least one vector with a non-empty input
  whose result differs from every input argument or that records an argument it changed.
  Realistic vectors (`id` prefix `realistic-`) cover 148 of those 151 functions;
  `extract_heredoc_bodies`, `tokenize_shell_command` and `command_patterns_match` are covered
  by their `entry-` and `negative-` vectors. A vector of a function that takes a parameter
  outside the builtin set, or no parameter, carries `metadata.reason`, a one-line note; a
  vector that records a raise its callers never reach carries `metadata.precondition`
  instead, naming the caller that guards it. A `bytes` argument is written `{"bytes": text}`.
- Every vectored function discriminates: its vectors record at least two distinct outcomes,
  one result that differs from every input argument or a recorded argument change, and a
  function that returns None records the change it makes. So a constant, identity or no-op
  stub of the function fails its replay. `contrast-` vectors are calls captured from the
  hook normalizer that give a function its second outcome, `hand-` vectors are written inputs
  where no captured call did, and `caller-` vectors reach a function whose argument JSON
  cannot carry (a map keyed by token identity) through `normalize_tool_fields`. The test file
  lists the seven exempt functions, each with its reason.
- Vector envelope. Replay snapshots every argument after the `prepare` steps and again after
  the call; when one changed, the expect is `{"result": ..., "mutated_args": {"<index or
  keyword>": <value after the call>}}`. `result_from_arg` stays accepted: it names the
  argument recorded as the result, which `mutated_args` then leaves out. A `scan_budget`
  adds `scan_within_budget` and `scan_observed`, a `scope` adds `node_requests`, and
  `within` adds the values read inside the context manager the entry returns.
- Arguments. A `construct` entry builds an argument from exactly one of `data`, `args` or
  `items`; an argument written `{"from": {"entry", "args", "kwargs", "pick"}}` is built by a
  production builder, nested derivations resolved first. A list reaches a parameter
  annotated `tuple`, `set` or `frozenset` as that type.
- Fixtures and scopes. A vector with `setup` builds a fixture checkout under the system
  `/tmp`, with HOME, USERPROFILE and GOBBY_HOME pointed at an empty home beside it, and
  records every path under the checkout relative to `{repo}`; `nested_repo` and `symlinks`
  add a second repository and links. A vector with `scope` runs inside a node-origin
  delivery that a scripted node answers, so it reads nothing of the host. Every vector that
  reads the filesystem, git, the cwd, HOME or the machine id is a fixture or scope vector;
  the rest read no host state, which a test checks with call-through spies. Fixture vectors
  run on Linux and macOS only; scope vectors run everywhere.
  Every parametrize combination of the 27 negative tests in
  `tests/workflows/test_command_matching.py` and `tests/hooks/test_code_navigation_recovery.py`
  replays through its own vector. The two combinations of
  `test_refused_outline_opens_only_requested_read` differ in the refusal line their
  `gcode outline` event carries, so each vector replays the normalizer step that builds the
  gate's state from that event. The gate call itself is replayed once, by
  `gate-refused-outline-broad-read`, which carries no `negative_from` and so is not counted
  as a combination; the test asserts the gate too. Production records the same recovery for
  either code, so the line, not the code, changes the outcome.
  A vector with `scan_budget` meters the scanner it names and records whether the scan stayed
  within the test's budget.
- `normalize_modules.json` lists the 46 normalizer modules the vectors cover, with a goal
  clause for each.

## Engine-path keys

A case that names an engine path seeds the state that path reads, so replacing the path with
a constant changes the recorded outcome. `tests/contracts/test_rules_engine_paths.py` holds
the table of paths, the cases each reaches and both outcomes.

- `seed` names domain rows written before the rules, in this order: `session` (`true`: the
  synthetic user, machine, project and the `SESSION_ID` session row), `agent_definitions`
  (synthetic agent definitions, installed globally with their `step_workflow`; a key the
  engine's models drop fails the seed) and `step_instance` (`agent`, `current_step` and an
  optional `snapshot_step_workflow`). The snapshot is what the instance stores; the installed
  definition keeps its own `step_workflow`, so a case whose snapshot allows everything shows
  that the engine enforces the installed policy. `tests/contracts/rules_seed.py` validates
  and applies it.
- `proxy`: `{"rtk": "echo-prefix"}` opts into a fake RTK, a fixed `#!/bin/sh` launcher that
  execs the quoted interpreter path on a Python script beside it, which the real proxy
  handler runs: it rewrites a command to `rtk <command>` and denies one that holds
  `synthetic-denied` (exit status 2). Without the key RTK is unavailable.
- `posix_only: true` marks a case whose intended path spawns through `/bin/sh`: the
  background delivery, an inline command without a boundary, the fake RTK, and a rule-pass
  `run_command`, and a handler case whose `domain` holds a git repo or a transcript file
  (POSIX paths, and git runs). In Python's replay a rule-pass `run_command` fails open before the spawn,
  because the frozen event stops the payload build (the known-bad registry's frozen
  run-command payload row and its tagged cases); the marker stays on those cases for the
  port, whose correct effect spawns through `/bin/sh`. It is the only replay skip: such a
  case is skipped on Windows, where a spawn with a cwd has no shell to run through. A case
  with none of these paths never carries the key.
- `{python}` in a `run_command` command list is replaced by the running interpreter when the
  rule is seeded, so the recorded data names no interpreter path.
- `engine_skip_group` names the group the engine skips for a CLI-internal request, and
  `pairs_with` names the main case a partner belongs to. Each CLI-internal case has partners
  with the request metadata absent and false, and the expected skipped groups are derived from
  the case input.
- `skipped_groups` is observed, not inferred: the recorder records which rules reached the
  engine's event-match check, and a group is skipped when it has a member and none reached
  it. A member whose condition was false still reached it, so its group is not skipped.
- Every `run_command` effect logs `{"run_command_deadline": "none" | "live" | "expired"}`
  before it runs, from the deadline's real state: `none` when the effect context holds no
  blocking deadline object, `expired` when it holds one whose budget is spent on the effect
  module's clock, and `live` when it holds one with budget left. This is the effect-seam contract "a run_command effect receives the
  pass's blocking deadline"; a port must record the same entry at its effect dispatch.
- The seam caller `inline_run_command` reads its seeded rule back by `input.rule_name` and
  applies its `run_command` effect through `RuleEngine._apply_effect` with a live event. The
  rule pass hands effects a frozen event that the payload builder cannot write into, so a
  rule-pass `run_command` never runs in Python; this caller is how the corpus reaches the
  effect's execution, result handling and audit write. It needs `seed.session`, and its
  `returned` holds `context_parts` and `audit_rows`, the `(event_type, result)` pairs read
  back from `workflow_audit_log`. `input.deadline` is `"expired"` or null. A case with
  `input.command_boundary` (`{"duration", "context"}`) starts no process and reads no real
  clock: the command execution is replaced by a boundary that succeeds when the duration fits
  the timeout it is handed, the clock does not move, `input.deadline_remaining` sets the
  deadline (seconds, negative for an expired one, or null), and `returned.command_timeouts`
  lists the timeout the effect code supplied.

## Path keys

Family, observer, transition and hardcoded cases name the engine and handler paths they
drive. `tests/contracts/rules_paths.py` is the registry of path ids, `rules_case_paths.py`
the loader checks for the keys below, and `tests/contracts/test_rules_paths.py` the tests.

- `kind` is `rule_pass` (the engine entry), `handler_pass` (`entry: "handler"`: every pass
  goes through `WorkflowHookHandler.evaluate_async`) or `seam_caller`. A handler case seeds
  `domain` (`rules_domain.py`): a project, a git `repo` built in a temporary directory,
  `sessions`, `agents` with step workflows, `tasks`, `variables`, `step_instances` and
  `agent_runs`. A case gives `seed` or `domain`, never both. `{repo}`, `{session:<key>}`,
  `{task:<key>}` and the other placeholders resolve at seed time and are written back into
  the record, so no recorded value names a temporary path or a generated id.
- `sequence` holds the passes after `event`; any pass may carry `between`, the state
  operations applied before it (a claim, a git step, a wait, a cached late MCP result).
  `expect.passes` records each pass's outcome, response, logs, variable delta and, for a
  handler pass, the facts it evaluated and the paths it reached. The top-level logs stay
  whole-case and the top-level response is the last pass's.
- `reaches` lists the path ids the case drives; replacing a callable path with its stub
  changes an output the case produces. `control_for` lists callable paths the case runs
  without the stub changing it; it needs `twin_of`, a partner that lists the path under
  `reaches`.
- `twin_of` names the case this one is paired with and `differs_at` the input paths where
  the two differ; their produced outputs differ. An inline branch is proved by a twin pair or
  by `branch_passes: {"<branch id>": [i, j]}`, two of the case's own passes whose outputs
  differ.
- `faults` injects a failure (`rules_faults.py`); a fault case is the twin of its unfaulted
  base, names its fault branch under `reaches`, and `seam_of` names the helper whose removal
  leaves it unchanged.
- `requires` lists guards that must hold at record and in plain replay (a fact, a variable,
  the current step, a reached path, a fault hit), each on the last pass unless it names
  `pass`.
- `observe` names domain rows recorded after each pass (session variables, the step instance,
  tasks, agent runs). `returned_spy_keys` names the keys of a seam caller's `returned` that
  are the harness's own log, which the mutation comparison drops.
- The seam caller `proxy_self_call_dispatch` runs the engine pass on its event, refuses an
  emitted proxy self-call entry that differs from `input.mcp_calls` (the engine case's
  recorded entry), and hands that entry to the hook manager's dispatch with a recording fake
  proxy. A seam caller records `reached`, the probed paths it called.

## Synthetic only

Every rule id starts with `synthetic-`. No case or vector asserts the behavior of a bundled
rule. The bundled rule YAML is loaded through the engine loader only to check that each
file still validates. Case and vector JSON carry no absolute or transcript paths.

## Known-bad tags

`tests/contracts/rules_known_bad.json` lists engine entries on which Python gives a wrong
value. Each row names the entry, an input, the field read, Python's value
(`python_observed`), the value the Rust port must give (`correct`), a control input on which
Python is right, and `dependent_paths`, the expect paths the flaw can change downstream of
the entry. A row's `id` is computed from its entry, input form, input and field; tags carry
the id, never the row's `ref`.

A case or vector whose replay calls a row's entry with the row's input and gets the row's
flawed value carries `"known_bad": [<row id>, ...]` (sorted) and `known_bad_expect`, a list
of declarations. The re-record of the registry computes the tags; the declarations on a case
are hand-written, and those on a vector are written with it because the row determines them.
A corpus re-record keeps both keys, since it assigns only `expect`.

The Rust harness reads them as one contract:

- At the entry it asserts the row's `correct`. A row's optional `accepts` names an allowed
  alternative to `correct`; the only member is `not_reevaluated`, for route rows only: the row
  also passes when the route does not evaluate the already admitted call a second time.
- A control is unchanged behaviour: the Rust side reproduces each control's
  `python_observed` exactly. A control carries no `correct`.
- In a tagged case it asserts the recorded expect exactly at every path no declaration names,
  and each named path by its declaration. An undeclared difference fails, so a tag never
  switches a case off and never hides an unrelated regression.
- A declaration names `row` (in the case's tag), `path` (into `expect`, at or below one of the
  row's `dependent_paths`) and exactly one of: `correct` with `from` (a case path, vector path
  or row field whose recorded value proves it), or `classification`: `contains` (`value` tied
  by `from`), `extends` (the Rust list holds every recorded entry in order and at least one
  more; `adds` names required members), `unpinned` (with `reason`: not compared until Python
  is fixed and the case re-recorded) or `unchanged` (with `reason`: the flaw does not reach
  this case's outcome).
- A pass case records its response and logs again under `passes`. A top-level declaration has
  a mirror of the same form under `passes`: response and outcome paths on the last pass, log
  slices on each pass the flaw changes. A declaration on a per-pass log slice or on the last
  pass's response has its top-level copy.
- A condition-helper row carries a `demonstration`: a synthetic rule run on the row input and
  on the control. Its `correct_input` is the outcome class Rust must give for the row input;
  Rust asserts `correct_input` and `expect.control`, never `expect.input`.

## Replay

Replay runs the engine and the normalizers against the committed expect blocks:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py -q
```

The engine replay runs each case in an isolated schema on the test hub. A mismatch names
the case or vector id.

## Re-record

A deliberate engine or normalizer change is re-recorded, not hand-edited. Set
`GOBBY_CORPUS_RECORD=1` and run the re-record test; it writes the observed blocks back into
the case and vector files:

```bash
GOBBY_CORPUS_RECORD=1 DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test \
  GOBBY_TEST_PROTECT=1 uv run pytest tests/contracts/test_rules_corpus.py -k rerecord_committed_corpus
```

Review the diff before committing. A re-record that changes an expect block is a behavior
change and needs its own rationale.
