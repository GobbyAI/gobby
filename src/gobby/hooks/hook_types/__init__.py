"""
Hook Type Definitions and Pydantic Models.

This module defines all Claude Code hook types and their associated input/output models
using Pydantic for validation. Each hook type has specific input and output schemas
that ensure type safety and validation across the hook execution pipeline.

Hook Types (27 total):
1. session-start: Triggered when a Claude Code session starts
2. session-end: Triggered when a session ends
3. user-prompt-submit: Triggered before user prompt is submitted
4. pre-tool-use: Triggered before a tool is executed
5. post-tool-use: Triggered after a tool is executed
6. post-tool-use-failure: Triggered after a tool fails
7. pre-compact: Triggered before context window compaction
8. post-compact: Triggered after context compaction
9. stop: Triggered when agent stops
10. stop-failure: Triggered when a turn ends with an API failure
11. subagent-start: Triggered when a subagent starts
12. subagent-stop: Triggered when a subagent stops
13. task-created: Triggered when a task is created
14. task-completed: Triggered when a task is completed
15. notification: Triggered for system notifications
16. instructions-loaded: Triggered when CLAUDE.md/rules content loads
17. config-change: Triggered when Claude configuration changes
18. cwd-changed: Triggered when the working directory changes
19. file-changed: Triggered when a watched file changes
20. worktree-create: Triggered when a worktree is being created
21. worktree-remove: Triggered when a worktree is being removed
22. elicitation: Triggered when an MCP server requests user input
23. elicitation-result: Triggered after a user responds to an elicitation
24. before-model: Triggered before model inference (Gemini)
25. after-model: Triggered after model inference (Gemini)
26. permission-request: Triggered when permission is requested (Claude)
27. permission-denied: Triggered when auto mode denies a tool (Claude)

Example:
    ```python
    from gobby.hooks.hook_types import HookType, SessionStartInput

    # Validate input
    input_data = SessionStartInput(
        external_id="abc123",
        transcript_path="/path/to/transcript.jsonl",
        source="startup"
    )
    ```
"""

from .agent import (
    SubagentStartInput,
    SubagentStartOutput,
    SubagentStopInput,
    SubagentStopOutput,
    TaskCompletedInput,
    TaskCompletedOutput,
    TaskCreatedInput,
    TaskCreatedOutput,
    TeammateIdleInput,
    TeammateIdleOutput,
)
from .base import HookInput, HookOutput
from .enums import (
    CompactTrigger,
    HookType,
    NotificationSeverity,
    SessionEndReason,
    SessionStartSource,
)
from .environment import (
    ConfigChangeInput,
    ConfigChangeOutput,
    CwdChangedInput,
    CwdChangedOutput,
    FileChangedInput,
    FileChangedOutput,
    InstructionsLoadedInput,
    InstructionsLoadedOutput,
    NotificationInput,
    NotificationOutput,
    WorktreeCreateInput,
    WorktreeCreateOutput,
    WorktreeRemoveInput,
    WorktreeRemoveOutput,
)
from .interactive import (
    AfterModelInput,
    AfterModelOutput,
    BeforeModelInput,
    BeforeModelOutput,
    ElicitationInput,
    ElicitationOutput,
    ElicitationResultInput,
    ElicitationResultOutput,
    PermissionDeniedInput,
    PermissionDeniedOutput,
    PermissionRequestInput,
    PermissionRequestOutput,
)
from .lifecycle import (
    PostCompactInput,
    PostCompactOutput,
    PreCompactInput,
    PreCompactOutput,
    StopFailureInput,
    StopFailureOutput,
    StopInput,
    StopOutput,
)
from .mappings import HOOK_INPUT_MODELS, HOOK_OUTPUT_MODELS
from .session import (
    SessionEndInput,
    SessionEndOutput,
    SessionStartInput,
    SessionStartOutput,
    UserPromptSubmitInput,
    UserPromptSubmitOutput,
)
from .tool import (
    ContextItem,
    PostToolUseFailureInput,
    PostToolUseFailureOutput,
    PostToolUseInput,
    PostToolUseOutput,
    PreToolUseInput,
    PreToolUseOutput,
)

__all__ = [
    "HOOK_INPUT_MODELS",
    "HOOK_OUTPUT_MODELS",
    "AfterModelInput",
    "AfterModelOutput",
    "BeforeModelInput",
    "BeforeModelOutput",
    "CompactTrigger",
    "ConfigChangeInput",
    "ConfigChangeOutput",
    "ContextItem",
    "CwdChangedInput",
    "CwdChangedOutput",
    "ElicitationInput",
    "ElicitationOutput",
    "ElicitationResultInput",
    "ElicitationResultOutput",
    "FileChangedInput",
    "FileChangedOutput",
    "HookInput",
    "HookOutput",
    "HookType",
    "InstructionsLoadedInput",
    "InstructionsLoadedOutput",
    "NotificationInput",
    "NotificationOutput",
    "NotificationSeverity",
    "PermissionDeniedInput",
    "PermissionDeniedOutput",
    "PermissionRequestInput",
    "PermissionRequestOutput",
    "PostCompactInput",
    "PostCompactOutput",
    "PostToolUseFailureInput",
    "PostToolUseFailureOutput",
    "PostToolUseInput",
    "PostToolUseOutput",
    "PreCompactInput",
    "PreCompactOutput",
    "PreToolUseInput",
    "PreToolUseOutput",
    "SessionEndInput",
    "SessionEndOutput",
    "SessionEndReason",
    "SessionStartInput",
    "SessionStartOutput",
    "SessionStartSource",
    "StopFailureInput",
    "StopFailureOutput",
    "StopInput",
    "StopOutput",
    "SubagentStartInput",
    "SubagentStartOutput",
    "SubagentStopInput",
    "SubagentStopOutput",
    "TaskCompletedInput",
    "TaskCompletedOutput",
    "TaskCreatedInput",
    "TaskCreatedOutput",
    "TeammateIdleInput",
    "TeammateIdleOutput",
    "UserPromptSubmitInput",
    "UserPromptSubmitOutput",
]
