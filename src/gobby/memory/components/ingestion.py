"""
Component for handling Memory Manager's multimodal ingestion logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gobby.memory.ingestion import MultimodalIngestor
from gobby.memory.protocol import MemoryBackendProtocol
from gobby.storage.memories import LocalMemoryManager, Memory

if TYPE_CHECKING:
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


class IngestionService:
    """Service for handling memory ingestion, particularly multimodal content."""

    def __init__(
        self,
        storage: LocalMemoryManager,
        backend: MemoryBackendProtocol,
        llm_service: LLMService | None = None,
    ):
        self.storage = storage
        self._backend = backend
        self._llm_service = llm_service

        self._multimodal_ingestor = MultimodalIngestor(
            storage=storage,
            backend=backend,
            llm_service=llm_service,
        )

    @property
    def llm_service(self) -> LLMService | None:
        """Get the LLM service."""
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        """Set the LLM service and propagate to ingestor."""
        self._llm_service = service
        self._multimodal_ingestor.llm_service = service

    async def remember_with_image(
        self,
        image_path: str,
        context: str | None = None,
        memory_type: str = "fact",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """
        Store a memory with an image attachment.
        """
        return await self._multimodal_ingestor.remember_with_image(
            image_path=image_path,
            context=context,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )

    async def remember_screenshot(
        self,
        screenshot_bytes: bytes,
        context: str | None = None,
        memory_type: str = "observation",
        project_id: str | None = None,
        source_type: str = "agent",
        source_session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Memory:
        """
        Store a memory from raw screenshot bytes.
        """
        return await self._multimodal_ingestor.remember_screenshot(
            screenshot_bytes=screenshot_bytes,
            context=context,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
