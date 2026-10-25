"""Label-quality study: prepare the blinded sheet, then score it (community-label-decisions 3.3)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.ai.text_generation import TextGenerationService
    from gobby.code_index.models import StoredCommunity
    from gobby.config.code_index import CodeIndexConfig


def main(
    argv: list[str] | None = None,
    *,
    load_communities: Callable[[str], Sequence[StoredCommunity]] | None = None,
    generation: tuple[TextGenerationService, CodeIndexConfig] | None = None,
    resolve_secret: Callable[[str], str] | None = None,
) -> int:
    """Run ``prepare`` or ``score``."""
    raise NotImplementedError("community-label-decisions 3.3")


if __name__ == "__main__":
    raise SystemExit(main())
