"""Leaf constants shared by compact continuation producers and consumers."""

from gobby.utils.injected_context import INJECTED_CONTEXT_BEGIN

COMPACT_SELF_CONTINUE_VARIABLE = "compact_self_continue_pending"
COMPACT_RESUME_REQUIRED_SKILLS_VARIABLE = "compact_resume_required_skills"
COMPACT_HANDOFF_MARKER_VARIABLE = "handoff_source"
COMPACT_SELF_CONTINUE_INTRO = (
    "Continue where you last left off. If the previous turn shows a rejected or "
    "cancelled compact_self tool-use message immediately followed by /compact or "
    "/compress, treat it as expected terminal self-compaction delivery, not user "
    "refusal. "
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
LOADING_SKILLS_NAME = "loading-skills"
# Written by the workflow engine's load_skill effect: the skills the session's
# active workflow asked for, whether or not the agent got to them yet.
WORKFLOW_REQUESTED_SKILLS_VARIABLE = "workflow_requested_skills"
# Compaction is an in-place handoff on the same session row, so `loaded_skills` —
# the runtime ledger of successful agent-visible get_skill calls — survives it and
# describes exactly what this session had in context. Skills loaded before
# compaction must be reloaded after it, so the ledger is part of the resume set.
COMPACT_RESUME_SKILL_VARIABLE_KEYS = (
    "required_skills",
    "additional_skills",
    "claimed_task_required_skills",
    "claimed_task_additional_skills",
    WORKFLOW_REQUESTED_SKILLS_VARIABLE,
    "loaded_skills",
)
