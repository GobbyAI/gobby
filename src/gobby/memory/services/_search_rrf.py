"""Reciprocal Rank Fusion helpers for memory search."""


def rrf_scores(*ranked_lists: list[str], k: int = 60) -> dict[str, float]:
    """Compute Reciprocal Rank Fusion scores for one or more ranked lists."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, memory_id in enumerate(ranked):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def rrf_merge(*ranked_lists: list[str], k: int = 60) -> list[str]:
    """Merge ranked lists using Reciprocal Rank Fusion."""
    scores = rrf_scores(*ranked_lists, k=k)
    return sorted(scores, key=lambda memory_id: scores[memory_id], reverse=True)
