# Context boundaries

Load when context pressure requires compaction, selecting compact versus clear,
or recovering an interrupted boundary. Read the handoffs topic before authoring
content. Discover `gobby-sessions:get_handoff` and `set_handoff` through their
schemas; use the configured boundary instead of typing provider commands to
bypass handoff prerequisites.

Provider compact preserves the session row, external identity, task ownership,
workflow state, and terminal ownership. Context-epoch instruction tracking resets:
reload needed skills and exact references, preserving the requested standalone
levels. Loading the router does not reload its references. Consume a pending
handoff before turn-start bootstrap loads.

Manual `/clear` without a staged handoff creates an independent session with no
predecessor. A staged clear binds one successor and transfers live claims through
expected-owner checks. It is reserved for a root/coordinator moving on after task
closure; never use it to escape close gates. A parent link alone is not proof of
a delivered handoff.

Context-pressure thresholds come from live `context_handoff` configuration;
small windows use ratios, standard/unknown windows use token thresholds, and
extended windows use their own token thresholds. Plan mode and pipelines are
exempt from this pressure enforcement. At block pressure, finish permitted handoff
prerequisites and stage the boundary. Coordination messages do not clear the gate.
A non-retryable missing compaction path can downgrade the epoch to warning;
background delivery failures instead require a handoff retry.

Inspect returned failures and follow recovery guidance. Do not repeatedly poll a
pending delivery or mutate its markers. Shutdown protects unresolved handoffs;
consume the continuation before coordinating a daemon restart. `--force` does
not discard a protected handoff.

Contract: [Session boundary](../../../../../../../../docs/contracts/session-boundary.md).
Guide: [Compaction](../../../../../../../../docs/guides/sessions.md#compaction-is-in-place).

_Last verified: 2026-09-12_
