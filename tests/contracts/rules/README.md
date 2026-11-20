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
  `run_command`. In Python's replay a rule-pass `run_command` fails open before the spawn,
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

## Synthetic only

Every rule id starts with `synthetic-`. No case or vector asserts the behavior of a bundled
rule. The bundled rule YAML is loaded through the engine loader only to check that each
file still validates. Case and vector JSON carry no absolute or transcript paths.

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
