# Rule Templates Reference

This directory contains bundled rule groups. These are **templates** — they are synced to the `rule_definitions` DB table on daemon start with `enabled: true` by default. Existing installed bundled rows are refreshed when template content changes while preserving the user's enabled toggle. Installed bundled rows whose templates were removed are soft-deleted as orphans. See `../AGENTS.md` for the template vs active enforcement distinction.

## Rule Groups

| Group | Dir | Rules | Purpose |
|-------|-----|-------|---------|
| `worker-safety` | `worker-safety/` | 59 | Block git push for spawned sessions and through an opt-in strict policy, force push, the operator-only landing override, destructive git/shell, unmanaged worktree/clone commands, direct provider launches, bash sleep, agent spawning through an opt-in strict policy, external GitHub issues, package install/publish, remote-script exec, full test suite, validation runs wrapped in pipes or command sequences, daemon management, data exfiltration (curl/wget upload, scp/sftp, secret-path reads), and, for every session, listings of other processes' environment or arguments (`ps aux`, `ps -o args` or `command`, plain `ps` and any `ps` without an allowlisted `-o`, `ps -e`/`-E`, `pgrep -fl`, `pgrep -a`, `/proc/<pid>/environ`); `ps` is allowed only with `-o` naming non-argument columns, and PID-only `pgrep -f`/`-g` stay open |
| `tool-hygiene` | `tool-hygiene/` | 13 | Require `uv` and Gobby memory tools. Selected terminal rules restrict web-chat callers and target scope; opt-in `name:no-send-keys` blocks both terminal tools. Bound spawned messaging by `send_message_targets` and spawning by `spawnable_agents`. Network override requires `override_spawn_network` or the operator terminal. Selected `no-comms-attach` blocks conversation attachment. |
| `progressive-discovery` | `progressive-discovery/` | 5 | Require a current-context schema lease before ordinary MCP calls; track optional inventory discovery |
| `task-enforcement` | `task-enforcement/` | 22 | Require claimed tasks and interactive mutation guidance, block spawned-agent task creation, block `update_task` unless the agent definition excludes `block-update-task`, nudge native trackers after claim, inject claimed-task extras, protect shared-worktree edits and commits, keep the operator-only `gobby sessions approve-dirty-paths` away from every agent, fail closed on unresolved-scope shell writes, and enforce valid task transitions, and refuse unquoted braces or substitutions in gobby command words |
| `stop-gates` | `stop-gates/` | 8 | Require workflow completion, let a standing seat with no claimed task stop at its `idle: true` step after a durable readiness send declares `seat_idle` through a rule effect (cleared at the next turn start), and enforce the Found Work ladder before turn end |
| `plan-mode` | `plan-mode/` | 6 | Track plan-mode entry and exit, block edits, teach plan navigation, and reset state |
| `memory-lifecycle` | `memory-lifecycle/` | 14 | Initial-load recovery gated on MCP proxy readiness, memory surfacing at turn start, agent spawn, task claim, and handoff, post-close review, plan-memory guards, turn sequencing, and tracking reset |
| `session-feedback` | `session-feedback/` | 3 | Bounded Gobby-experience survey through feedback, once per epoch for task closures no submission has covered |
| `context-handoff` | `context-handoff/` | 16 | Compact/resume handoffs, pending pull enforcement, task context, user profile, and pressure nudges |
| `code-index` | `code-index/` | 8 | Require the code-index/overview reference, redirect broad source reads to `gcode`, and fail open when gcode cannot serve the checkout |
| `monolith-enforcement` | `monolith-enforcement/` | 4 | Require same-session decomposition before writes, commits, task transitions, and turn end |
| `pipeline-enforcement` | `pipeline-enforcement/` | 1 | Auto-run assigned pipeline on session start |
| `error-recovery` | `error-recovery/` | 1 | Inject recovery guidance after tool failures |
| `skill-discovery` | `skill-discovery/` | 32 | Bootstrap ordered core guidance each context epoch, require each seat's procedure and listed references before work, require language skills on first file write, require the impeccable design contract on first UI file write, require a plan reference on plan-artifact writes, require the code-review skill on the first commit/merge/rebase/cherry-pick/revert of an epoch and a fresh `ocr delegate rule` review before each later one, list skill hubs once per session, and reset loading tracking |
| `brevity` | `brevity/` | 5 | Handle opt-out phrases, drift detection and next-turn feedback, and per-turn reinforcement |
| `restraint` | `restraint/` | 3 | Block first code write/edit until restraint is loaded, opt-out phrases, per-turn reinforcement |
| `development-discipline` | `development-discipline/` | 1 | Block the first checkout-touching tool call under a claimed code/refactor/test task until developer test and structural guidance is loaded |
| `review-learning` | `review-learning/` | 2 | Inject confirmed planning and review lessons into matching work |
| `roles` | `roles/` | 18 | Definition-selected seat guidance and restrictions on spawning, pipeline launch, project creation, user questions, task closure, database queries and write scopes. Exclusions declare exemptions. Team managers use their declared spawn/resume scopes within the admitted team. Closing definitions declare reasons, task scopes and review requirements. Optional documentation, research, digest and Prometheus scopes are selected by name. Operator exemptions use stored identity; definition names grant no authority. |

Definitions may explicitly select `name:no-git-push` or `name:no-agent-spawn`.
These strict policies cover persona and spawned sessions, including team managers.
They are separate from the broad `worker-safety` tag; agent names grant no exemption.

Experiments that belong to one project are not bundled. They live in that
repository's `.gobby/workflows/rules/`, which `gobby install`/`gobby sync` syncs
as `user` rows owned by the project's id, so the engine never serves them to
another project.

## File Convention

Each group is a directory containing one or more YAML files. Each YAML file has:

```yaml
tags: [group-tag, category-tag]     # Tags for selector matching

rules:
  rule-name:
    description: "..."
    event: before_tool
    enabled: true                    # Templates default to enabled
    priority: 100
    when: "condition"
    effects:
      - type: block
        ...
```

Multiple rules can live in one YAML file, or each rule can have its own file. The convention varies by group.

For lifecycle authoring, prefer semantic workflow events such as `turn_start`
and `turn_end`. Raw events such as `before_agent`, `after_agent`, and `stop`
are escape hatches for provider-specific detail. They fire on every provider
event: the manual-compact bypass skips only the semantic `turn_end` of the
provider-noise Stop after a manual `pre_compact`, so a rule on `event: stop`
still runs there. Stop gates use `turn_end`.

## Turn-End Overrides and Acknowledge Variables

The engine assembles a turn-end response in a fixed order
(`src/gobby/workflows/engine/evaluation.py`, `_assemble_response`): the hardcoded
overrides (`force_allow_stop` → allow; `tool_block_pending` / `edit_write_pending` →
block) are returned before any rule-level block reason, after the winning rule's
effects have already run, and a gate's `acknowledge_variable` is written only when
its block is actually delivered. Consequences for stop-gate authors:

- Consume a gate's trigger state with `acknowledge_variable`, never with a
  `set_variable` effect ahead of `block`. Under an override the block is not
  delivered and the state is lost; under aggregation, later rules contribute only
  their `block` effects through the block-gates path, so the state is consumed
  twice.
- Real-engine `turn_end` tests must seed `_memory_initial_stop_checked: true` or
  `loaded_skill_references: ["gobby:references/memory/overview.md"]`, or the priority-1 memory gate wins the turn end.

Override mechanics are documented in `docs/guides/workflow-rules.md`
(Hard-Coded Engine Behaviors).

## Tags

Tags serve two purposes:

| Tag | Meaning |
|-----|---------|
| `gobby` | **Provenance** — rule ships with Gobby. All bundled rules get this tag. |
| `default` | **Baseline** — selected by the interactive definition and used when no enabled definition resolves. |
| Group tags | **Identity** — rule belongs to a functional group. Workers cherry-pick these. |

The `default` agent uses `rule_selectors: {include: ["tag:default"]}` to load interactive-session rules. Worker agents select the group tags needed for their role.

The current enabled definition supplies rule selection. Missing or disabled
definitions receive only enabled `default`-tagged rules; cached
`_active_rule_names` cannot preserve an exclusion or activate an opt-in rule.
An enabled definition may intentionally select an empty set.

Standing definitions explicitly select `tag:seat-policy`. Their exclusions grant
spawn, pipeline, question, project-creation and closure permissions. Write scopes
(`research-artifact-write-scope`, `documentation-write-scope`, `digest-write-scope`)
and `prometheus-query-only` are selected by name. Database-query permission is an
explicit exclusion of `query-readonly-seat-scope`. Baseline session rules
keep unresolved children and missing or disabled named definitions bounded while
leaving the operator's unspawned terminal open. Project creation uses the stored
operator identity; parent lineage alone never identifies a spawned child. Closing definitions
also declare their permitted reasons and task scopes for MCP enforcement.

`pipeline-enforcement` and `no-send-keys` omit `default` and are selected explicitly, `no-send-keys` by name. `worker-safety` combines default-tagged interactive protections with worker protections selected by group tag.

### Group Tags

Each group uses its directory name as an identity tag. Cross-cutting tags such as
`enforcement`, `lifecycle`, `context`, and `memory` may span several groups.

## Guides

- [Rules](../../../docs/guides/rules.md) — Full rules reference
- [Variables](../../../docs/guides/variables.md) — Session variables used in conditions
- [Workflows Overview](../../../docs/guides/workflows-overview.md) — How rules fit the system
