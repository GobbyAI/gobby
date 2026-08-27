"""Leaf constants shared by compact continuation producers and consumers."""

from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN

COMPACT_SELF_CONTINUE_VARIABLE = "compact_self_continue_pending"
COMPACT_NOTIFICATION_STARTED_AT_VARIABLE = "compact_notification_started_at"
COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE = "compact_resume_required_skills"
COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE = "compact_resume_advisory_skills"
COMPACT_HANDOFF_MARKER_VARIABLE = "handoff_source"
COMPACT_HANDOFF_INJECT_PENDING_VARIABLE = "compact_handoff_inject_pending"
COMPACT_SELF_INTERRUPT_WARNING = (
    "In a terminal session that call comes back as a rejected or cancelled tool use "
    "attributed to the user. That is the daemon interrupting the turn to deliver the "
    "compaction command, never a refusal: do not stop, do not ask the user about it, "
    "and resume from the continuation prompt."
)
COMPACT_SELF_CONTINUE_INTRO = (
    "Continue where you last left off. The previous turn called "
    "`gobby-sessions:compact_self`. " + COMPACT_SELF_INTERRUPT_WARNING + " "
)
COMPACT_SELF_CONTINUE_PROMPT = (
    COMPACT_SELF_CONTINUE_INTRO + "If startup context contains "
    f"`{INJECTED_CONTEXT_BEGIN}`, use that injected context directly and continue. "
    "Only if the injected context is missing or incomplete, call "
    "`gobby-sessions.wait_for_summary` for the compacted session. If it returns "
    "`completed=false`, repeat the same wait call. Once complete, use the returned "
    "`context` and continue."
)
COMPACT_SELF_CONTINUE_FRESH_SECONDS = 600
COMPACT_SELF_CONTINUE_SEND_DELAY_SECONDS = 1.0
# A composer still settling a bracketed paste can swallow the Enter that
# follows it; a second Enter after this delay submits the trigger and is a
# no-op when the first Enter already submitted (empty composer).
COMPACT_SELF_CONTINUE_SUBMIT_RETRY_DELAY_SECONDS = 1.5
LOADING_SKILLS_NAME = "loading-skills"
# Meta-skills excluded from compact resume reload tiers: the per-turn reminder
# rules and the proxy server instructions re-deliver their guidance every
# epoch, so a post-compact reload only duplicates context.
COMPACT_RESUME_EXCLUDED_SKILLS = frozenset({LOADING_SKILLS_NAME, "brevity"})
# Written by the workflow engine's load_skill effect: the skills the session's
# active workflow asked for, whether or not the agent got to them yet.
WORKFLOW_REQUESTED_SKILLS_VARIABLE = "workflow_requested_skills"
# Compact reload scope comes from current core, claimed-task, and workflow requirements.
# `loaded_skills` is historical within the context epoch and can retain language skills
# from already-closed work, so it is reset after compaction without entering the snapshot.
COMPACT_RESUME_REQUIRED_SKILL_VARIABLE_KEYS = (
    "required_skills",
    "claimed_task_required_skills",
    WORKFLOW_REQUESTED_SKILLS_VARIABLE,
)
COMPACT_RESUME_ADVISORY_SKILL_VARIABLE_KEYS = (
    "additional_skills",
    "claimed_task_additional_skills",
)

SKILL_LIST_VARIABLE_NAMES = frozenset(
    (
        *COMPACT_RESUME_REQUIRED_SKILL_VARIABLE_KEYS,
        *COMPACT_RESUME_ADVISORY_SKILL_VARIABLE_KEYS,
        COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE,
        COMPACT_RESUME_ADVISORY_SKILLS_VARIABLE,
    )
)
