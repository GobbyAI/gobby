from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.prompt_detector import PromptDetector, PromptKind
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.storage.attention import run_attention_entry_id

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun
    from gobby.storage.attention import AttentionKind, AttentionStateManager
    from gobby.storage.sessions import SessionManager


class AgentAttentionTracker:
    """Classify and persist attention episodes for agent panes."""

    def __init__(
        self,
        *,
        run_db: Callable[..., Awaitable[Any]],
        prompt_detector: PromptDetector,
        stall_classifier: StallClassifier,
        tmux_config: TmuxConfig,
        attention_manager: AttentionStateManager | None = None,
        attention_metadata_store: AttentionMetadataStore | None = None,
        get_session_manager: Callable[[], SessionManager | None] | None = None,
    ) -> None:
        self._run_db = run_db
        self._prompt_detector = prompt_detector
        self._stall_classifier = stall_classifier
        self._tmux_config = tmux_config
        self._attention_manager = attention_manager
        self._attention_metadata_store = attention_metadata_store
        self._get_session_manager = get_session_manager

    @property
    def enabled(self) -> bool:
        return self._attention_manager is not None

    async def sync(self, run: AgentRun, pane_output: str) -> None:
        """Persist the attention episode represented by the latest pane output."""
        manager = self._attention_manager
        if manager is None:
            return

        prompt_detector = self._prompt_detector.for_provider(run.provider)
        stall_classifier = self._stall_classifier.for_provider(run.provider)
        reason: PromptKind | None = None
        kind: AttentionKind | None = None
        classification_reason: str | None = None
        detected = prompt_detector.detect_prompt(pane_output)
        approval_dismissed = (
            detected is not None
            and detected.kind == "approval"
            and prompt_detector.was_approval_prompt_dismissed(run.id, pane_output)
        )
        trust_dismissed = (
            detected is not None
            and detected.kind == "trust"
            and prompt_detector.was_dismissed(run.id)
        )
        if (
            detected is not None
            and detected.kind == "approval"
            and (not self._tmux_config.auto_enter_approval_prompts or approval_dismissed)
        ):
            reason = "approval"
            kind = "actionable"
        elif detected is not None and detected.kind == "trust" and trust_dismissed:
            reason = "trust"
            kind = "actionable"
        elif detected is not None and detected.kind == "question":
            reason = "question"
            kind = "actionable"
        else:
            classification = stall_classifier.classify(
                run.id,
                pane_output=pane_output,
                error=run.error,
            )
            if classification.status is StallStatus.PROVIDER_STALL:
                reason = "stall"
                kind = "non_actionable"
                classification_reason = classification.reason

        entry_id = run_attention_entry_id(run.id)
        if self._attention_metadata_store is not None:
            if reason == "stall":
                self._attention_metadata_store.set(entry_id, "retrying provider", 30_000)
            elif approval_dismissed or trust_dismissed:
                self._attention_metadata_store.set(entry_id, "needs attention", 60_000)

        if reason is None or kind is None:
            await self._resolve_lifecycle_wait(entry_id)
            await self._clear_if_current(entry_id)
            return

        prompt_payload = (
            detected
            if detected is not None and detected.kind == reason
            else prompt_detector.classification_payload(
                kind=reason,
                label=classification_reason or reason,
            )
        )
        await self._enter_lifecycle_wait(
            run.child_session_id,
            reason,
            prompt_payload.fingerprint,
            run.provider,
        )
        await manager.transition_async(
            self._run_db,
            entry_id,
            state="blocked",
            run_id=run.id,
            session_id=run.child_session_id,
            reason=reason,
            kind=kind,
            fingerprint=prompt_payload.fingerprint,
            payload=prompt_payload.to_payload(),
        )

    async def clear_after_injection(self, run: AgentRun) -> None:
        """Clear the exact attention episode resolved by successful injection."""
        entry_id = run_attention_entry_id(run.id)
        await self._resolve_lifecycle_wait(entry_id)
        await self._clear_if_current(entry_id)
        if self._attention_metadata_store is not None:
            self._attention_metadata_store.clear(entry_id)

    async def clear(self, run: AgentRun) -> None:
        """Authoritatively clear attention when a run becomes terminal."""
        entry_id = run_attention_entry_id(run.id)
        if self._attention_manager is not None:
            await self._attention_manager.transition_async(
                self._run_db,
                entry_id,
                state=None,
            )
        if self._attention_metadata_store is not None:
            self._attention_metadata_store.clear(entry_id)
        self._stall_classifier.clear(run.id)

    async def _clear_if_current(self, entry_id: str) -> None:
        manager = self._attention_manager
        if manager is None:
            return
        current = await self._run_db(manager.get, entry_id)
        if current is None or current.state is None:
            return
        await manager.transition_async(
            self._run_db,
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )

    async def _enter_lifecycle_wait(
        self,
        session_id: str | None,
        reason: PromptKind,
        fingerprint: str,
        provider: str,
    ) -> None:
        if session_id is None or reason not in {"approval", "question"}:
            return
        get_sessions = self._get_session_manager
        sessions = get_sessions() if get_sessions is not None else None
        if sessions is None:
            return

        def enter() -> None:
            from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

            lifecycle = TurnLifecycleReducer(sessions)
            current = lifecycle.get(session_id)
            lifecycle.enter_wait(
                session_id,
                kind="approval" if reason == "approval" else "input",
                token=fingerprint,
                evidence=TurnEvidence(source=f"{provider}.pane", generation=current.generation),
            )

        await self._run_db(enter)

    async def _resolve_lifecycle_wait(self, entry_id: str) -> None:
        manager = self._attention_manager
        get_sessions = self._get_session_manager
        if manager is None or get_sessions is None:
            return
        current_attention = await self._run_db(manager.get, entry_id)
        if (
            current_attention is None
            or current_attention.reason not in {"approval", "question"}
            or current_attention.fingerprint is None
            or current_attention.session_id is None
        ):
            return
        sessions = get_sessions()
        if sessions is None:
            return

        def resolve() -> None:
            from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer

            lifecycle = TurnLifecycleReducer(sessions)
            state = lifecycle.get(current_attention.session_id)
            lifecycle.resolve_wait(
                current_attention.session_id,
                token=current_attention.fingerprint,
                resolution="ambiguous",
                evidence=TurnEvidence(source="pane.resolved", generation=state.generation),
            )

        await self._run_db(resolve)
