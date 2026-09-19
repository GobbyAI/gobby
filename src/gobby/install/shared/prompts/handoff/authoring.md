---
description: Guidance for authoring a bounded current-epoch continuation handoff
---
Persist a structured handoff, then compact the current session or clear into a
successor when clear_session=true. Submit any required survey separately through
gobby-sessions:feedback first, then call set_handoff last.
Feedback is checked before content limits. Use clear_session=false while working
inside tasks. Use clear_session=true only between tasks after task closure;
an open claimed task blocks clearing.

All sections share a hard limit of 10,000 JSON-escaped characters, measured as
len(json.dumps(rendered_markdown)), including headings, list formatting, escaping,
and enclosing quotes. Aim below approximately 5,000 encoded characters for ordinary
handoffs to leave headroom. Oversized content is rejected before staging.

Write fresh reflections from the current context epoch only, alongside current
continuation state. current_state and next_steps describe current work, relevant
validation, and immediate actions; key_decisions and blockers preserve active
constraints and obstacles. Reference durable task/design/memory rationale for older
decisions. what_was_accomplished records this epoch's meaningful outcomes;
problems_encountered and what_didnt_work record fresh friction observations,
including resolved friction: attempt, obstacle, consequence or workaround. No general
lesson is required. notes holds other necessary live context; references locates sources.

found_work lists findings placed on the found-work ladder as {finding, disposition, ref}:
fixed with the #N task this session or a spawned descendant claimed or closed,
escalated with the active owner session ref after send_message, or filed-task with
the rung-3 #N task this session created. An entry not marked fixed re-arms the
continuing session's found-work gate; a finding with no disposition belongs in the
ladder, not the handoff.

Never copy earlier reflections into a new handoff; copied observations inflate
apparent recurrence in future daily synthesis. Record another occurrence only when
friction actually recurs. Unresolved blockers can carry forward without their history.
Do not combine epochs, copy previous handoffs, or reproduce completed-work ledgers.
Earlier history is stored in the database; reference its task or session records
when needed. Existing Gobby memory guidance still applies.

Use clear, readable sentences and ordinary technical terms. Do not overuse
shorthand, invented abbreviations, compressed task-number chains, or cryptic notes
to fit more history into the handoff. Shorten by removing history and unnecessary
detail, not by making the remaining text harder to read.

Supply nonblank current_state and at least one nonblank next_steps entry. Leave
optional fields empty when unneeded. Prune history and superseded detail first.
If necessary live working detail still cannot fit, create or update a session-scoped
Markdown working-context file and add its project-relative path to references before
submitting. Keep immediate orientation and next actions inline. Refresh current state
in the file; never append epoch histories or move discarded history and reflections
into it. Prepare the file while writes are permitted, before hard context-pressure
gates block writes. Surface preservation conflicts before compaction; never bypass
permissions or truncate necessary context.

In a terminal session the daemon interrupts the active turn, confirms the
interrupt from the transcript, clears the composer, and submits the provider
command. Provider cancellation or rejection immediately after this call is the
expected dispatch signal. A clear_acknowledgment_timeout with attempt_pending=true
means /clear was delivered and the successor binds on SessionStart: do not call
set_handoff again. A retry reuses the pending attempt and never types a second
/clear. The continuation must call get_handoff().
