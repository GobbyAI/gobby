"""Multimodal content ingestion for memory system."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from gobby.memory.protocol import MediaAttachment
from gobby.storage.memories import Memory

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.protocol import MemoryBackendProtocol
    from gobby.storage.memories import LocalMemoryManager

logger = logging.getLogger(__name__)


class MultimodalIngestor:
    """
    Handles ingestion of multimodal content (images, screenshots) into memory.

    Extracts image handling from MemoryManager to provide focused
    multimodal processing capabilities.
    """

    def __init__(
        self,
        storage: LocalMemoryManager,
        backend: MemoryBackendProtocol,
        llm_service: LLMService | None = None,
    ):
        """
        Initialize the multimodal ingestor.

        Args:
            storage: Memory storage manager for persistence
            backend: Memory backend protocol for creating records
            llm_service: LLM service for image description
        """
        self._storage = storage
        self._backend = backend
        self._llm_service = llm_service

    @property
    def llm_service(self) -> LLMService | None:
        """Get the LLM service for image description."""
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: LLMService | None) -> None:
        """Set the LLM service for image description."""
        self._llm_service = service

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

        Uses the configured LLM provider to generate a description of the image,
        then stores the memory with the description as content and the image
        as a media attachment.

        Args:
            image_path: Path to the image file
            context: Optional context to guide the image description
            memory_type: Type of memory (fact, preference, etc)
            project_id: Optional project context
            source_type: Origin of memory
            source_session_id: Origin session
            tags: Optional tags

        Returns:
            The created Memory object

        Raises:
            ValueError: If LLM service is not configured or image not found
        """
        path = Path(image_path)
        if not path.exists():
            raise ValueError(f"Image not found: {image_path}")

        # Get LLM provider for image description
        if not self._llm_service:
            raise ValueError(
                "LLM service not configured. Pass llm_service to MemoryManager "
                "to enable remember_with_image."
            )

        provider = self._llm_service.get_default_provider()

        # Generate image description
        description = await provider.describe_image(image_path, context=context)

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "application/octet-stream"

        # Create media attachment
        media = MediaAttachment(
            media_type="image",
            content_path=str(path.absolute()),
            mime_type=mime_type,
            description=description,
            description_model=provider.provider_name,
        )

        # Store memory with media attachment via backend
        record = await self._backend.create(
            content=description,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
            media=[media],
        )

        # Return as Memory object for backward compatibility
        # Note: The backend returns MemoryRecord, but we need Memory
        memory = self._storage.get_memory(record.id)
        if memory is not None:
            return memory

        # Fallback: construct Memory from MemoryRecord if storage lookup fails
        # This can happen with synthetic records from failed backend calls
        return Memory(
            id=record.id,
            content=record.content,
            memory_type=record.memory_type,
            created_at=record.created_at.isoformat(),
            updated_at=record.updated_at.isoformat()
            if record.updated_at
            else record.created_at.isoformat(),
            project_id=record.project_id,
            source_type=record.source_type,
            source_session_id=record.source_session_id,
            tags=record.tags,
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

        Saves the screenshot to .gobby/resources/ with a timestamp-based filename,
        then delegates to remember_with_image() for LLM description and storage.

        Args:
            screenshot_bytes: Raw PNG screenshot bytes (from Playwright/Puppeteer)
            context: Optional context to guide the image description
            memory_type: Type of memory (default: "observation")
            project_id: Optional project context
            source_type: Origin of memory
            source_session_id: Origin session
            tags: Optional tags

        Returns:
            The created Memory object

        Raises:
            ValueError: If LLM service is not configured or screenshot bytes are empty
        """
        if not screenshot_bytes:
            raise ValueError("Screenshot bytes cannot be empty")

        # Determine resources directory using centralized utility
        from datetime import datetime as dt

        from gobby.cli.utils import get_resources_dir
        from gobby.utils.project_context import get_project_context

        ctx = get_project_context()
        project_path = ctx.get("path") if ctx else None
        resources_dir = get_resources_dir(project_path)

        # Generate timestamp-based filename
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"screenshot_{timestamp}.png"
        filepath = resources_dir / filename

        # Write screenshot to file
        filepath.write_bytes(screenshot_bytes)
        logger.debug(f"Saved screenshot to {filepath}")

        # Delegate to remember_with_image
        return await self.remember_with_image(
            image_path=str(filepath),
            context=context,
            memory_type=memory_type,
            project_id=project_id,
            source_type=source_type,
            source_session_id=source_session_id,
            tags=tags,
        )
