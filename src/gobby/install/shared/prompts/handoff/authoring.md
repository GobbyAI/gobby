---
description: Guidance for authoring a bounded current-epoch continuation handoff
---
Persist a structured handoff, then compact the current session or clear into a
successor when clear_session=true. Submit any required survey separately through
gobby-sessions:feedback first, then call set_handoff last.

Write a handoff from the current context epoch only: current state, concrete next
actions, and still-active constraints or blockers the next epoch needs. Do not
combine multiple epochs of session history, copy previous handoffs, or reproduce
completed-work ledgers. Earlier history is stored in the database; reference its
task or session records when needed.

Use clear, readable sentences and ordinary technical terms. Do not overuse
shorthand, invented abbreviations, compressed task-number chains, or cryptic notes
to fit more history into the handoff. Shorten by removing history and unnecessary
detail, not by making the remaining text harder to read.

Supply nonblank current_state and at least one nonblank next_steps entry. Rendered
content is limited to 10,000 JSON-escaped characters including section formatting.
Oversized content is rejected before staging; shorten it and retry. Reference
existing evidence instead of copying logs or transcripts.

In a terminal session the daemon interrupts the active turn, confirms the
interrupt from the transcript, clears the composer, and submits the provider
command. Provider cancellation or rejection immediately after this call is the
expected dispatch signal. A clear_acknowledgment_timeout with attempt_pending=true
means /clear was delivered and the successor binds on SessionStart: do not call
set_handoff again. A retry reuses the pending attempt and never types a second
/clear. The continuation must call get_handoff().
