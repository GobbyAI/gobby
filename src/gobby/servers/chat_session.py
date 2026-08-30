"""
Chat session backed by ClaudeSDKClient for persistent multi-turn conversations.

Each ChatSession wraps a ClaudeSDKClient instance that maintains conversation
context across messages. Sessions are keyed by conversation_id (stable across
WebSocket reconnections) rather than ephemeral client_id.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
)
from claude_agent_sdk.types import (
    PermissionMode,
)

from gobby.agents.sandbox import SandboxConfig
from gobby.agents.sandbox_resolvers import (
    materialize_claude_settings_async,
    preflight_provider_native_settings_file_async,
)
from gobby.config.feature_base import parse_feature_candidate
from gobby.config.values import ConfigRuntimeReader
from gobby.servers.chat_session_helpers import (
    _FALLBACK_SYSTEM_PROMPT,
    PendingApproval,
    _build_gobby_mcp_entry,
    _find_cli_path,
    _find_project_root,
)
from gobby.servers.chat_session_hooks import ChatSessionHooksMixin
from gobby.servers.chat_session_messages import ChatSessionMessagesMixin
from gobby.servers.chat_session_permissions import ChatSessionPermissionsMixin

logger = logging.getLogger(__name__)
ClaudeReasoningEffort = Literal["low", "medium", "high", "max"]
_CLAUDE_REASONING_EFFORTS = frozenset({"low", "medium", "high", "max"})
_HEADLESS_SETTINGS = Path.home() / ".gobby" / "settings" / "headless.json"


@dataclass
class ChatSession(ChatSessionHooksMixin, ChatSessionMessagesMixin, ChatSessionPermissionsMixin):
    """
    A persistent chat session backed by ClaudeSDKClient.

    Maintains conversation context across messages and survives
    WebSocket disconnections. Sessions are identified by conversation_id.
    """

    conversation_id: str
    provider: str = field(default="claude")
    db_session_id: str | None = field(default=None)
    seq_num: int | None = field(default=None)
    project_id: str | None = field(default=None)
    project_path: str | None = field(default=None)
    message_index: int = field(default=0)
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _model: str | None = field(default=None, repr=False)
    reasoning_effort: str | None = field(default=None, repr=False)
    _pending_questions: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _pending_answer_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_answers: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    _pending_approvals: dict[str, PendingApproval] = field(default_factory=dict, repr=False)
    _pending_approval_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_approval_decisions: dict[str, str] = field(default_factory=dict, repr=False)
    _approved_tools: set[str] = field(default_factory=set, repr=False)
    chat_mode: str = field(default="plan", repr=False)
    _plan_approved: bool = field(default=False, repr=False)
    _plan_feedback: str | None = field(default=None, repr=False)
    _plan_approval_completed: bool = field(default=False, repr=False)
    _plan_file_path: str | None = field(default=None, repr=False)
    _last_plan_content: str | None = field(default=None, repr=False)
    _pending_plan_content: str | None = field(default=None, repr=False)
    _pending_plan_allowed_prompts: list[str] | None = field(default=None, repr=False)
    _pending_post_plan_mode: str | None = field(default=None, repr=False)
    _pending_plan_events: dict[str, asyncio.Event] = field(default_factory=dict, repr=False)
    _pending_plan_decisions: dict[str, str] = field(default_factory=dict, repr=False)
    _plan_broadcast_sent: bool = field(default=False, repr=False)
    _on_plan_ready: Callable[[str | None, dict[str, Any], str | None], Awaitable[None]] | None = (
        field(default=None, repr=False)
    )
    _config: Any | None = field(default=None, repr=False)
    _tool_approval_config: Any | None = field(default=None, repr=False)
    _tool_approval_callback: Any | None = field(default=None, repr=False)
    _on_approved_tools_persist: Callable[[set[str]], None] | None = field(default=None, repr=False)
    _needs_history_injection: bool = field(default=False, repr=False)
    _last_model: str | None = field(default=None, repr=False)
    _pending_agent_name: str | None = field(default=None, repr=False)
    _max_history_message_chars: int = field(default=2000, repr=False)
    _max_history_total_chars: int = field(default=30_000, repr=False)
    _context_window_overrides: dict[str, int] = field(default_factory=dict, repr=False)
    _accumulated_output_tokens: int = field(default=0, repr=False)
    _message_manager_source_session_id: str | None = field(default=None, repr=False)
    _message_manager: Any | None = field(default=None, repr=False)
    sdk_session_id: str | None = field(default=None, repr=False)
    system_prompt_override: str | None = field(default=None, repr=False)
    resume_session_id: str | None = field(default=None, repr=False)
    _session_manager_ref: Any | None = field(default=None, repr=False)
    _config_runtime_ref: ConfigRuntimeReader | None = field(default=None, repr=False)
    _transcript_path_captured: bool = field(default=False, repr=False)
    _active_reasoning_effort: str | None = field(default=None, repr=False)
    _preapproved_tool_use_ids: set[str] = field(default_factory=set, repr=False)
    sandbox_config: SandboxConfig | None = field(default=None, repr=False)
    sandbox_policy_hash: str | None = field(default=None, repr=False)
    sandbox_metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    _sandbox_launch: Any = field(default=None, repr=False)

    # Lifecycle callbacks — set by ChatMixin to bridge SDK hooks to workflow engine
    _on_before_agent: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_post_tool: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_pre_compact: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_subagent_start: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_subagent_stop: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = field(
        default=None, repr=False
    )
    _on_mode_changed: Callable[[str, str], Awaitable[None]] | None = field(default=None, repr=False)
    _on_mode_persist: Callable[[str], None] | None = field(default=None, repr=False)

    @property
    def _default_model(self) -> str | None:
        """Resolve default model from config."""
        if not self._config:
            return None

        chat_cfg = getattr(self._config, "chat", None)
        candidates = getattr(chat_cfg, "candidates", ()) if chat_cfg else ()
        for candidate in candidates:
            try:
                candidate_provider, candidate_model = parse_feature_candidate(candidate)
            except ValueError:
                continue
            if candidate_provider == self.provider:
                return candidate_model

        return None

    async def _resolve_requested_model(
        self, requested_model: str | None, env: dict[str, str]
    ) -> str | None:
        """Resolve special model aliases and mutate env for endpoint overrides."""
        if requested_model == "local":
            raise RuntimeError("Model 'local' has been removed; replace it with 'endpoint:<name>'")

        from gobby.ai.endpoints import resolve_generation_endpoint_selector

        selection = resolve_generation_endpoint_selector(self._config, requested_model)
        if selection is None:
            return requested_model or self._default_model

        endpoint = selection.endpoint_with_selected_model()
        if endpoint.wire_api == "responses":
            raise RuntimeError("Responses generation endpoints require provider='codex'")

        env["ANTHROPIC_BASE_URL"] = endpoint.api_base
        if endpoint.api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = endpoint.api_key

        resolved_model = endpoint.model
        try:
            from gobby.agents.local_model import LocalModelError, ensure_local_model

            resolved_model = await ensure_local_model(endpoint, run_manager=None)
        except LocalModelError as e:
            raise RuntimeError(f"Local model pre-flight failed: {e}") from e

        logger.info(
            "ChatSession %s using local endpoint %s model: %s",
            self.conversation_id,
            selection.name,
            resolved_model,
        )
        return resolved_model

    def _resolve_reasoning_effort(self) -> ClaudeReasoningEffort | None:
        normalized = (self.reasoning_effort or "").strip().lower()
        if normalized in _CLAUDE_REASONING_EFFORTS:
            return cast(ClaudeReasoningEffort, normalized)
        return None

    async def _reconnect_for_reasoning_effort_change(self) -> None:
        resume_target = self.sdk_session_id or self.resume_session_id
        current_model = self._model
        await self.stop()
        self.resume_session_id = resume_target
        await self.start(model=current_model)

    async def start(self, model: str | None = None) -> None:
        """Connect the ClaudeSDKClient with configured options."""
        cli_path = _find_cli_path()
        if not cli_path:
            raise RuntimeError(
                "Claude CLI not found in PATH. "
                "Install Claude Code and authenticate it before starting chat."
            )

        self._model = model

        # Use the project's repo_path if available (set by web UI project selector),
        # otherwise fall back to gobby project root (dev mode) or cwd.
        if self.project_path:
            cwd = self.project_path
        else:
            project_root = _find_project_root()
            cwd = str(project_root) if project_root else str(Path.cwd())

        # SDK resume carries its own system prompt and context — skip construction
        if self.resume_session_id:
            system_prompt = None
        else:
            # The Gobby persona is single-sourced from the agent-definition
            # rows: non-default agents arrive as system_prompt_override, and
            # the default agent's preamble is injected once per context epoch
            # at first prompt — the static prompt here stays minimal.
            system_prompt = self.system_prompt_override or _FALLBACK_SYSTEM_PROMPT
            # Inject working directory so the agent doesn't hallucinate paths
            system_prompt += f"\n\n## Environment\n- Working directory: {cwd}\n"
            if self.db_session_id:
                session_ref = f"#{self.seq_num}" if self.seq_num else self.db_session_id
                system_prompt += (
                    f"- Session ID: {session_ref} (use for session_id params in MCP tools)\n"
                )
            if self.project_id:
                system_prompt += f"- Project ID: {self.project_id}\n"

        # Build SDK hooks from lifecycle callbacks
        sdk_hooks = self._build_sdk_hooks()

        # Pass session context to the CLI subprocess so it attaches to the
        # web chat's pre-created session instead of creating a new one.
        env: dict[str, str] = {}
        if self.db_session_id:
            env["GOBBY_SESSION_ID"] = self.db_session_id
            env["GOBBY_SOURCE"] = "claude"
        if self.project_id:
            env["GOBBY_PROJECT_ID"] = self.project_id

        resolved_model = await self._resolve_requested_model(model, env)

        resolved_effort = self._resolve_reasoning_effort()
        sandbox_config = self.sandbox_config or SandboxConfig(enabled=False)
        identity_env = os.environ.copy()
        identity_env.update(env)
        settings_path: str | None
        verified_sandbox: dict[str, Any]
        if sandbox_config.enabled and sandbox_config.backend == "srt":
            from gobby.agents.srt_runtime import prepare_sandbox_launch
            from gobby.paths import get_gobby_home

            daemon_cfg = self._config
            websocket = getattr(daemon_cfg, "websocket", None)
            launch = await prepare_sandbox_launch(
                config=sandbox_config,
                provider="claude",
                workspace_path=cwd,
                run_id=self.db_session_id or self.conversation_id,
                resolver=None,
                daemon_port=int(getattr(daemon_cfg, "daemon_port", 60887)),
                websocket_port=int(getattr(websocket, "port", 60888)),
                api_base=None,
                env=identity_env,
            )
            shim = launch.emit_cli_shim(
                command=[cli_path],
                directory=get_gobby_home() / "run" / "shims",
            )
            self._sandbox_launch = launch
            cli_path = str(shim)
            env = {**identity_env, **launch.provider_env}
            settings_path = await materialize_claude_settings_async(
                base_settings_path=_HEADLESS_SETTINGS,
                config=SandboxConfig(enabled=False),
                workspace_path=cwd,
                name="web-chat",
            )
            verified_sandbox = launch.metadata()
        else:
            settings_path = await materialize_claude_settings_async(
                base_settings_path=_HEADLESS_SETTINGS,
                config=sandbox_config,
                workspace_path=cwd,
                name="web-chat",
            )
            verified_sandbox = await preflight_provider_native_settings_file_async(
                provider="claude",
                settings_path=settings_path,
                config=sandbox_config,
                workspace_path=cwd,
                policy_hash=self.sandbox_policy_hash,
            )
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            max_turns=None,
            model=resolved_model,
            effort=resolved_effort,
            permission_mode=self._to_sdk_permission_mode(self.chat_mode),
            allowed_tools=["mcp__gobby__*"],
            can_use_tool=self._can_use_tool,
            cli_path=cli_path,
            mcp_servers={"gobby": _build_gobby_mcp_entry()},
            cwd=cwd,
            hooks=cast(Any, sdk_hooks) if sdk_hooks else None,
            # Prevent user/project settings from merging in — the programmatic
            # hooks above are sufficient. Without this, SDK 0.1.56+ merges
            # ~/.claude/settings.json hooks which fire Gobby-managed hook commands,
            # creating ghost claude_sdk sessions on every hook call.
            settings=settings_path,
            setting_sources=[],
            env=env or {},
            # Enable partial messages so we receive StreamEvent objects with
            # per-API-call usage from message_start events. Without this, the
            # ResultMessage.usage contains accumulated token counts across ALL
            # API calls in the agentic loop, making context % wildly wrong.
            include_partial_messages=True,
            # SDK native resume — picks up exact conversation state from a
            # previous session (terminal, autonomous, or web chat).
            resume=self.resume_session_id,
            continue_conversation=bool(self.resume_session_id),
        )

        self._client = ClaudeSDKClient(options=options)
        try:
            await self._client.connect()
        except Exception:
            self._cleanup_sandbox_launch()
            raise
        self.sandbox_metadata = verified_sandbox
        self._connected = True
        self._active_reasoning_effort = resolved_effort
        self.last_activity = datetime.now(UTC)
        logger.debug("ChatSession %s started", self.conversation_id)

    def _cleanup_sandbox_launch(self) -> None:
        launch = self._sandbox_launch
        self._sandbox_launch = None
        cleanup = getattr(launch, "cleanup_cli_shim", None)
        if callable(cleanup):
            cleanup()

    async def stop(self) -> None:
        """Disconnect the ClaudeSDKClient and clean up."""
        self._abort_pending_interactions()
        if self._client:
            try:
                await self._client.disconnect()
            except RuntimeError as e:
                # The SDK's Query._tg.__aexit__() raises RuntimeError when
                # stop() is called from a different asyncio task than the one
                # that called start() (e.g. idle cleanup or shutdown).
                if "cancel scope" in str(e):
                    logger.debug(
                        "ChatSession %s cross-task disconnect (expected): %s",
                        self.conversation_id,
                        e,
                    )
                else:
                    logger.debug(
                        "ChatSession %s disconnect error (expected): %s", self.conversation_id, e
                    )
            except Exception as e:
                logger.debug(
                    "ChatSession %s disconnect error (expected): %s", self.conversation_id, e
                )
            finally:
                self._client = None
                self._connected = False
                self._active_reasoning_effort = None
                self._cleanup_sandbox_launch()
                logger.debug("ChatSession %s stopped", self.conversation_id)

    async def clear_context(self) -> bool:
        """Drop SDK resume identifiers and start a fresh Claude session."""
        selected_model = self.model
        preserved_mode = self.chat_mode
        try:
            await self.stop()
        except Exception:
            logger.exception(
                "Failed to stop Claude session before context clear conversation=%s",
                self.conversation_id,
            )
            return False
        self.resume_session_id = None
        self.sdk_session_id = None
        try:
            await self.start(model=selected_model)
        except Exception:
            logger.exception(
                "Failed to start Claude session after context clear conversation=%s",
                self.conversation_id,
            )
            return False
        self.chat_mode = preserved_mode
        return True

    @property
    def model(self) -> str | None:
        """The current model for this session."""
        return self._model

    async def switch_model(self, new_model: str) -> None:
        """Switch to a different Claude model mid-conversation."""
        if not self._client or not self._connected:
            raise RuntimeError("ChatSession not connected")
        resolved_model = new_model
        if new_model == "local":
            raise RuntimeError("Model 'local' has been removed; replace it with 'endpoint:<name>'")
        from gobby.ai.endpoints import resolve_generation_endpoint_selector

        selection = resolve_generation_endpoint_selector(self._config, new_model)
        if selection is not None:
            endpoint = selection.endpoint_with_selected_model()
            if endpoint.wire_api == "responses":
                raise RuntimeError("Responses generation endpoints require provider='codex'")
            resolved_model = endpoint.model
            try:
                from gobby.agents.local_model import LocalModelError, ensure_local_model

                resolved_model = await ensure_local_model(endpoint, run_manager=None)
            except LocalModelError as e:
                raise RuntimeError(f"Local model pre-flight failed: {e}") from e
        await self._client.set_model(resolved_model)
        self._model = new_model

    def add_output_tokens(self, tokens: int) -> int:
        """Accumulate output token usage and return the new total."""
        self._accumulated_output_tokens += max(0, tokens)
        return self._accumulated_output_tokens

    def set_accumulated_output_tokens(self, tokens: int) -> None:
        """Restore accumulated output token usage from durable session state."""
        self._accumulated_output_tokens = max(0, tokens)

    # Map Gobby chat_mode values to SDK PermissionMode values
    _MODE_TO_SDK: ClassVar[dict[str, PermissionMode]] = {
        "plan": "plan",
        "accept_edits": "acceptEdits",
        "bypass": "bypassPermissions",
        "normal": "default",
    }

    @staticmethod
    def _to_sdk_permission_mode(chat_mode: str) -> PermissionMode:
        """Convert a Gobby chat_mode to an SDK PermissionMode string."""
        return ChatSession._MODE_TO_SDK.get(chat_mode, "default")

    async def sync_sdk_permission_mode(self) -> None:
        """Sync the SDK subprocess permission mode to match chat_mode.

        Sends a control protocol message to the running CLI process so
        the agent receives a structured mode transition signal (equivalent
        to EnterPlanMode / ExitPlanMode).
        """
        if not self._client or not self._connected:
            return
        sdk_mode = self._to_sdk_permission_mode(self.chat_mode)
        try:
            await self._client.set_permission_mode(sdk_mode)
            logger.debug(
                "SDK permission mode synced to '%s' for %s", sdk_mode, self.conversation_id
            )
        except Exception as e:
            logger.warning("Failed to sync SDK permission mode: %s", e)

    @property
    def is_connected(self) -> bool:
        """Whether the session is currently connected."""
        return self._connected
