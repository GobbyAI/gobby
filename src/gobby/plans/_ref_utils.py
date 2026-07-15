"""Shared normalization for references embedded in plan prose."""


def clean_ref(value: str) -> str:
    """Remove surrounding prose punctuation and balanced reference quotes."""
    cleaned = value.strip().rstrip(".;,")
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"`", '"', "'"}:
        cleaned = cleaned[1:-1]
    return cleaned.strip()
