"""Knowledge graph service facade.

This package preserves the historical ``gobby.memory.services.knowledge_graph``
import path while keeping the implementation split across focused modules.
"""

from __future__ import annotations

from .models import (
    Entity,
    KnowledgeGraphResult,
    KnowledgeGraphStatus,
    Relationship,
)
from .service import KnowledgeGraphService

__all__ = [
    "Entity",
    "KnowledgeGraphResult",
    "KnowledgeGraphService",
    "KnowledgeGraphStatus",
    "Relationship",
]
