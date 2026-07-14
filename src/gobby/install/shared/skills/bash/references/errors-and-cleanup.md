# Errors And Cleanup

## Explicit Failure Boundaries

Use conditionals when a command can fail in normal operation:

```bash
if ! artifact="$(build_artifact)"; then
  printf 'artifact build failed\n' >&2
  return 1
fi
```

This preserves context and works independently of `errexit`. Keep diagnostic
output on stderr. Functions should return a useful status; the process boundary
decides whether to exit.

## `errexit` Is Contextual

`set -e` does not mean every non-zero command exits. Bash changes its behavior
inside conditional tests, negation, most pipeline positions, command lists, and
some function or subshell contexts. A caller can also change whether a function
body is subject to `errexit` by invoking the function in a condition.

Use `set -e` as a backstop after writing explicit checks around operations that
need recovery or explanation. Never depend on it as the sole error-handling
strategy for destructive or stateful work.

## Pipelines

Enable `pipefail` when failure of any stage invalidates the pipeline. When stages
need distinct handling, copy `PIPESTATUS` immediately because the next command
overwrites it:

```bash
producer | transformer | consumer
statuses=("${PIPESTATUS[@]}")
```

Avoid pipelines whose stages must mutate parent-shell variables. Use process
substitution or redirection so the loop executes in the intended shell.

## Cleanup

Register cleanup immediately after acquiring the resource:

```bash
temp_dir="$(mktemp -d)"

cleanup() {
  local status=$?
  rm -rf -- "$temp_dir"
  return "$status"
}

trap cleanup EXIT
```

Keep cleanup idempotent and safe after partial setup. Quote every resource path,
reject empty or unsafe paths before recursive deletion, and preserve the
original status. Avoid `exit` inside an EXIT trap unless the status behavior has
been tested.

## Signals And Child Processes

Decide which component owns process lifecycle. A long-running wrapper should:

- record child PIDs it starts;
- forward or handle expected termination signals;
- wait for children and collect their statuses;
- avoid leaving background jobs after failure;
- make repeated signal handling safe.

Test SIGINT and SIGTERM behavior in an isolated process group when the script
manages background work.

## Traps In Libraries

Traps are global shell state. Sourced libraries should return cleanup handles or
offer explicit cleanup functions. When trap composition is unavoidable, capture
and restore existing trap behavior carefully and test nested users.
