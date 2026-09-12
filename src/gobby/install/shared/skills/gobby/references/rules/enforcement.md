# Enforcement and recovery

Load when diagnosing a block, effect order, or gate. Start with the reported
name and `get_rule`; inspect enabled state, active configuration, event/project
scope, and selectors. A pre-execution denial is not a failed tool execution.

Rules sort by priority, resolved-event order, then name. Agent and step tool
restrictions precede declarative rules. Eligible non-block effects run before
the selected deferred block is applied. Later rules see state changes; context
injections accumulate.

`rules.aggregate_blocks` defaults true. After the first blocking rule, later
rules contribute only block gates; their non-block effects are suppressed.
Disabled aggregation stops after the first blocking rule. On a user interrupt,
configurable turn-end blocks are suppressed while non-block effects continue.

Hard-coded turn-end recovery can displace rule blocks after effects run:
catastrophic force-allow (suppressed with claimed tasks), recent tool failure,
then pending edit/write recovery. `acknowledge_variable` is consumed only for a
delivered block; `on_receipt` defers persistence to receipt acknowledgment.
Never clear a trigger in a preceding `set_variable`: a displaced or aggregated
block may never deliver its prompt.

Repeated attempts at the same blocked tool escalate (default five total attempts).
MCP identity is `server:tool`, not every `call_tool`. Satisfy the prerequisite
before retrying. Turn start clears transient counters/recovery state, not claims.
Use event-driven durable waits; do not poll or weaken stop gates to end with
unresolved owned work.

Programmatic CLI/REST/pipeline/internal MCP calls skip agent before/after-tool
rules and do not grant schema leases or clear agent discovery/tool errors. Do
not use those paths to bypass a gate. Fix the underlying work and verify the
authorized agent path in isolated state when changing policy.

Guides: [Evaluation](../../../../../../../../docs/guides/rules.md#evaluation-rules) and
[recovery](../../../../../../../../docs/guides/workflow-rules.md#hard-coded-engine-behaviors).

_Last verified: 2026-09-12_
