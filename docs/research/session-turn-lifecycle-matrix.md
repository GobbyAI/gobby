# Session Turn Lifecycle Matrix

This document is the normalized provider contract consumed by Gobby's lifecycle
reducer. The machine-readable characterization is
`tests/fixtures/provider_contracts/turn-lifecycle.json`; its contract test rejects a
missing scenario, an unpinned provider, or an unsupported normalized outcome.

## Verified Baseline

| Provider | Version | Completion | Human wait | Confirmed user interruption |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.263 | `Stop` | `AskUserQuestion`, elicitation, `PermissionRequest`, `ExitPlanMode`, and typed notification fallbacks, correlated by interaction ID | Current transcript interrupt marker; the bounded sequence has no `Stop` |
| Codex | 0.153.2 | Native `Stop`; app-server `turn/completed` completed or failed | Request-user-input, approval/MCP requests, and `thread/status/changed` wait flags | Native `Interrupt`; app-server interrupted status; rollout `turn_aborted` |
| Droid | 0.223.0 | `Stop` | Typed permission or elicitation notification correlated with a current transcript tool ID | Current `idle_prompt` message says the agent was stopped by the user and the matching transcript has `agent_turn_outcome(reason=cancelled)`; this explicit pair overrides the generic `Stop` emitted just before it |
| Grok | 1.0.30 | `Stop(reason=end_turn)` and other non-user terminal reasons | `pending_interaction` kinds, correlated by exact interaction and `promptId` | Current `StopCancelled(reason=user_interrupt,cancelledBy=user)`; session `events.jsonl` (beside the registered `updates.jsonl`) appends `turn_ended` with `outcome=cancelled` within milliseconds of Ctrl+C (`cancellation_context.trigger=ctrl_c`). Esc never cancels a Grok turn; Ctrl+C on an empty composer does, and `/compact` is rejected while a turn runs (#22358) |
| Qwen Code | 0.23.0 | `Stop` | Correlated `PermissionRequest` and displayed typed notification | Exact boolean `PostToolUseFailure.is_interrupt is True`; otherwise a Gobby-mediated key plus current output |
| AGY | 1.1.27 | `Stop` | Structured `ask_question` plus characterized permission, plan, and artifact screens | Gobby-mediated Esc/Ctrl-C plus current-version interruption output; the bounded sequence has no `Stop` |

The fixture was normalized on 2026-09-19 from the listed release contracts and
bounded, redacted event/pane slices. It records the command used for each provider,
the raw event order, positive evidence, and required absent events. Droid's ten
bounded source slices are preserved in
`tests/fixtures/provider_contracts/droid/turn-lifecycle-0.223.0.json`. AGY is pinned
to 1.1.27; older 1.1.24 characterization is not used for lifecycle decisions.

## Normalized Evidence

Terminal outcomes use exactly:

```python
TurnDisposition = Literal[
    "completed",
    "ended_non_user",
    "user_interrupted",
    "unknown",
]
```

- A normal answer and a plain assistant prose question both complete and become
  `paused`. Punctuation in assistant text is never structural wait evidence.
- A displayed structured question becomes `awaiting_input`.
- A displayed permission, plan, or artifact decision becomes `awaiting_approval`.
- A matching approval remains protected until resumed provider work is observed.
- A matching denial resolves the wait without impersonating a user turn interrupt.
- Failures, tool-local cancellations, channel shutdown, and session exit are
  `ended_non_user`, not `user_interrupted`.
- Missing, malformed, ambiguous, stale, or outcome-free evidence is `unknown` and
  retains the current lifecycle state.

## Correlation And Races

Every fresh prompt increments the local lifecycle generation and records any
provider turn key. Wait tokens, request IDs, and transcript/protocol cursors are
stored in `attention_states.payload.turn_lifecycle`. A late event must match the
current generation and provider key; an old wait resolution or terminal outcome
cannot overwrite a replacement prompt's `active` state.

Multiple waits remain protected. Input has display priority over approval. An
outcome-free interaction resolution enters a resolving state and stays protected
until resumed work or terminal evidence arrives.

`AFTER_AGENT` and AGY `PostInvocation` are model boundaries only. They do not pause
a session unless normalized terminal evidence accompanies them. Notifications have
lifecycle meaning only when their provider contract establishes a current,
displayed interaction.

## Bounded Missing-Event Conclusions

Claude's explicit-interrupt slice ends with the current transcript interrupt marker
and contains no `Stop`. AGY 1.1.27's in-flight Esc/Ctrl-C slice contains the
`Interrupted · What should Antigravity CLI do instead?` output and no `Stop`.
Waiting slices likewise end at a displayed interaction and assert the absence of a
terminal event. These conclusions are bounded to the complete fixture slices; an
unrelated later event cannot be retroactively attached to them.

Droid 0.223.0 is an explicit exception to the no-`Stop` interrupt shape: its
bounded slice contains `Stop`, then `idle_prompt`, then a matching transcript
`agent_turn_outcome(reason=cancelled)`. The later, current-turn cancellation
evidence wins; the generic `Stop` is retained in the source artifact rather than
discarded or rewritten.

Qwen and AGY terminal keys can bypass Gobby when sent directly through tmux. Such
input has no trustworthy provenance, so the session remains `active` unless a hook
or a Gobby-mediated key/output pair confirms interruption. Explicit waking may
therefore steer an unconfirmed direct-tmux turn; once a protected state is
positively established, composer and wake suppression apply.
