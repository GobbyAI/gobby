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
    ) -> None:
        self._run_db = run_db
        self._prompt_detector = prompt_detector
        self._stall_classifier = stall_classifier
        self._tmux_config = tmux_config
        self._attention_manager = attention_manager
        self._attention_metadata_store = attention_metadata_store

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

        entry_id = run_attention_entry_id(run.id)
        if self._attention_metadata_store is not None:
            if reason == "stall":
                self._attention_metadata_store.set(entry_id, "retrying provider", 30_000)
            elif approval_dismissed or trust_dismissed:
                self._attention_metadata_store.set(entry_id, "needs attention", 60_000)

        if reason is None or kind is None:
            await self._clear_if_current(entry_id)
            return

        prompt_payload = (
            detected
            if detected is not None and detected.kind == reason
            else prompt_detector.prompt_payload(pane_output, kind=reason)
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
        await self._clear_if_current(run_attention_entry_id(run.id))

    async def clear(self, run: AgentRun) -> None:
        """Authoritatively clear attention when a run becomes terminal."""
        if self._attention_manager is not None:
            await self._attention_manager.transition_async(
                self._run_db,
                run_attention_entry_id(run.id),
                state=None,
            )
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
