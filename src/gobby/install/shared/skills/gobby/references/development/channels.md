# Channel development

Load when building or validating communications adapters or provider hook support.
Also load [obligations](obligations.md). Channel operation/setup belongs to the
communications capability; provider lifecycle belongs to agents and sessions.

## Establish the contract

For a new channel effort, define the epic's scope and acceptance before code.
For an existing owned task, follow its plan and found-work rules rather than
creating duplicate tasks for every gap. Research the channel's current platform
and competitor documentation when parity is requested; record dated sources and
mark unverifiable claims UNVERIFIED. Historical competitor feature lists are not
current acceptance evidence.

Trace adapter parsing through inbound storage, identities, responder policy,
turn queue/backend and outbound delivery. Check:

- Persistent multi-turn context, restart recovery and deduplication/cursors.
- Allowlisted senders, configurable group/mention policy, per-chat sessions and
  per-sender identity; denied senders must not reach the agent.
- Inbound text/media/captions, attachment rows and shared voice transcription.
- Outbound images/files, typing, platform-safe formatting and length chunking.
- Streaming by edit where supported and single-send fallback elsewhere.
- `/new`, `/reset`, `/stop`, `/status`, `/help`, and visible activation failures.

Additional threads, reactions, buttons, pairing or voice replies follow the
channel specification. Existing implementation is not proof every adapter
supports every method. Compare `adapters/base.py` with the actual adapter and
its tests. The responder requires `responder.enabled` to be exactly true; group
policy and sender authorization precede turns. It serializes by conversation.

## Implement and validate

Inspect current schemas before using `gobby-communications` tools. Keep secrets
in encrypted secret references, verify channel activation and any `init_error`,
and establish inbound storage and session-scoped outbound delivery before
debugging responder wiring. Check destination resolution, identity references,
cursor persistence and shared dedup rather than adding adapter-local substitutes.

Use isolated fixtures first. A channel parity claim additionally requires an
authorized test account and observed live evidence for each required row:
bring-up, inbound/outbound sanity, context, policy, media, formatting, commands,
restart/no-duplicate recovery and negative access tests. Coordinate restarts and
use a dedicated test daemon; do not send test messages or install credentials
without authorization for that external setup. Record message IDs or sanitized
logs, platform/version, date, and unvalidated cases. Tests alone are not live proof.

Update the channel parity matrix and relevant integration guide when behavior
changes. Fix discovered defects through the owned task; deferred enhancement
ideas may be recorded separately. Store only durable verified channel quirks in
memory, with rationale, never credentials or unresolved bugs.

## Provider adapters

CLI hook providers are a separate adapter surface. Before first-class support,
prove native hooks, transcript/session identity, and streaming/control transport.
Declare capabilities in `src/gobby/adapters/capabilities.py` before behavior;
test normalized events, native decisions, context routing, unsupported-field
degradation and provider version floors. A schema-present field is not evidence
that a provider honors it. Preserve negative live evidence, including deliberately
unemitted fields. See [adapter fidelity](../../../../../../../../docs/guides/adapter-fidelity.md).

For failures, isolate transport, identity, authorization, responder and delivery
in that order. Do not enable broad access or fake streaming to hide a failure.
