# Coordinate Agent-Safe Gobby Lifecycle Commands

**Plan ID:** daemon-lifecycle-alert

## Overview
`kind: framing`

Protect the shared Gobby daemon from uncoordinated agent lifecycle commands.
`gobby-agents:send_message` already supports same-project fanout through
`target="all"`, so the implementation extends that selector and adds rule-engine
evidence checks instead of introducing another broadcast API.

Live investigation created two broadcasts and four durable messages. Delivery
was confirmed for active recipients, while no recipient returned an ACK. The
recipient set also changed during the test, demonstrating that authorization
must cover the current session set rather than rely on a reusable boolean flag.

## Constraints
`kind: framing`

- Preserve the existing absolute daemon-management ban for spawned agents.
- Scope coordination to the caller's project and exclude the sender and system
  session.
- Treat any peer in `handoff_ready` as a hard veto, regardless of alert state.
- Require a typed, exact-action, one-shot alert when another peer is active.
- Qualifying alerts expire after 5 minutes and require durable fanout coverage;
  ACKs and wake-up requests do not gate authorization.
- Allow only one `start`, `stop`, or `restart` action per shell call.
- Preserve paused-session broadcast delivery without counting paused sessions
  as lifecycle-gate peers.
- Use existing session, message metadata, and workflow-variable storage. No
  database migration or compatibility layer is required before `0.5.0`.
- Keep production daemon restart outside implementation validation.

## P1: Messaging and Lifecycle Enforcement
`kind: framing`

**Goal:** Extend broadcast coverage and enforce coordinated daemon lifecycle
commands with current, durable evidence.

### 1.1 Extend all-session broadcast coverage [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/mailbox.py`
- `tests/sessions/test_mailbox.py`

Extend `target="all"` from `active` and `paused` sessions to `active`, `paused`,
and `handoff_ready` sessions. Preserve same-project scoping, sender exclusion,
system-session exclusion, stable ordering, transactional fanout, selector
metadata, and the current handling for an empty recipient set.

Update mailbox coverage for active, paused, handoff-ready, expired,
foreign-project, sender, and system sessions. Selector metadata must report the
expanded status set so persisted broadcasts provide auditable recipient
selection evidence.

**Acceptance:**

- 1.1.1 - `target="all"` durably fans out to same-project active, paused, and
  handoff-ready sessions while retaining all existing exclusions. file:
  `src/gobby/sessions/mailbox.py`.
- 1.1.2 - Focused mailbox tests prove recipient selection, ordering, metadata,
  and exclusion behavior. test:
  `tests/sessions/test_mailbox.py::TestMailboxBroadcast`.

### 1.2 Add daemon coordination evaluation [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/workflows/daemon_coordination.py`
- `src/gobby/workflows/condition_helpers.py`
- `src/gobby/workflows/engine/templating.py`
- `tests/workflows/test_condition_helpers.py`
- `tests/workflows/test_daemon_safety_rules.py`

Add a focused daemon-coordination module and expose its functions through rule
condition/template helpers. Reuse the existing shell-segmentation utilities to
recognize direct `gobby`, `uv run`, environment assignments, executable paths,
and `python -m gobby` forms. Only an executable invocation counts; quoted
examples and commands such as `echo` must remain unmatched. Return the ordered
lifecycle actions so a shell call containing more than one action can be
rejected.

Build one lazy, memoized coordination snapshot per hook evaluation from the
rule engine's database. The snapshot must contain the current session's
same-project peers in `active` or `handoff_ready`, the handoff-ready subset, and
the latest qualifying alert for each lifecycle action.

A qualifying alert must:

- originate from the current session;
- have `message_type="daemon_lifecycle_alert"`;
- carry exact metadata `{"action": "start"|"stop"|"restart"}`;
- be a `target="all"` broadcast sent within the preceding 5 minutes;
- have persisted recipients covering every current active/handoff-ready peer;
- differ from `daemon_lifecycle_consumed_broadcast_id`.

Extra paused recipients remain valid. A newly active peer invalidates an alert
whose broadcast did not include that session. Direct messages, project/build
selectors, stale messages, wrong actions, and already-consumed broadcasts do
not qualify.

**Acceptance:**

- 1.2.1 - Structured parsing identifies supported daemon lifecycle invocations
  and rejects quoted, non-executable, and compound authorization shortcuts.
  symbol: `gobby.workflows.daemon_coordination`.
- 1.2.2 - A memoized hook snapshot reports current same-project peers and the
  handoff-ready subset without cross-project leakage. file:
  `src/gobby/workflows/daemon_coordination.py`.
- 1.2.3 - Alert eligibility enforces type, exact action, 5-minute freshness,
  current-recipient coverage, and one-shot consumption. test:
  `tests/workflows/test_daemon_safety_rules.py`.
- 1.2.4 - Rule conditions and templates can access the coordination helpers
  through the shared safe-evaluation function set. file:
  `src/gobby/workflows/engine/templating.py`.

### 1.3 Install daemon lifecycle safety rules [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/daemon-safety/daemon-lifecycle-alert.yaml`
- `src/gobby/install/shared/workflows/rules/worker-safety/no-daemon-management.yaml`
- `tests/workflows/test_daemon_safety_rules.py`
- `tests/workflows/test_worker_safety_rules.py`

Add an enabled bundled `daemon-safety` group and preserve the existing enabled
worker-safety rules in the database. Apply rules in this order:

1. Spawned-agent hard ban.
2. Compound lifecycle-command block.
3. Hard block when any peer is `handoff_ready`.
4. Alert requirement when one or more active peers exist.
5. Before allowing the shell call, save the eligible broadcast ID to
   `daemon_lifecycle_consumed_broadcast_id`.

The coordination rules apply only to non-spawned agents. When no active or
handoff-ready peer exists, one lifecycle action is allowed without an alert.
When peers exist, the block reason must render live counts and the exact
remediation call:

```text
gobby-agents:send_message(
  target="all",
  message_type="daemon_lifecycle_alert",
  metadata={"action": "restart"},
  content="About to run gobby restart; expect a brief daemon interruption."
)
```

The action in the remediation must match the attempted command. Queue
persistence is sufficient; `include_wakeup` and ACKs remain optional. The
consumption effect runs before shell execution, so a failed command still
requires a fresh alert.

**Acceptance:**

- 1.3.1 - Bundled rules enforce the declared priority order and render
  action-specific remediation with live peer counts. file:
  `src/gobby/install/shared/workflows/rules/daemon-safety/daemon-lifecycle-alert.yaml`.
- 1.3.2 - Any handoff-ready peer blocks lifecycle commands even when a valid
  alert exists. test: `tests/workflows/test_daemon_safety_rules.py`.
- 1.3.3 - A valid alert permits exactly one matching lifecycle action, while
  stale, reused, mismatched, missing, and incomplete alerts block. test:
  `tests/workflows/test_daemon_safety_rules.py`.
- 1.3.4 - Spawned agents remain absolutely barred from CLI and HTTP daemon
  management after the structured parser replaces the existing CLI regex.
  test: `tests/workflows/test_worker_safety_rules.py`.
- 1.3.5 - Bundled-rule synchronization installs every new rule enabled in the
  database, which remains the source of truth. test:
  `tests/workflows/test_daemon_safety_rules.py`.

## V1: Verification
`kind: verification`

Run focused validation after the final implementation edit:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/sessions/test_mailbox.py tests/workflows/test_condition_helpers.py tests/workflows/test_daemon_safety_rules.py tests/workflows/test_worker_safety_rules.py -v
uv run ruff check <changed-python-files>
uv run mypy <changed-python-files>
```

Do not run the full pytest suite. Production activation occurs through a
coordinated operator restart when no session is handoff-ready. After activation,
verify the installed DB rows with `gobby-workflows:get_rule`; template files
alone do not establish enabled state.
