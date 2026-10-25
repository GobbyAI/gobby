"""Operator-run live capture of every Jev decision shape (community-label-decisions 3.1)."""

from __future__ import annotations

from collections.abc import Callable


def main(
    argv: list[str] | None = None,
    *,
    resolve_secret: Callable[[str], str] | None = None,
) -> int:
    """Capture Choice, Noul, Score and Box round trips plus raw probes into a dated section."""
    raise NotImplementedError("community-label-decisions 3.1")


if __name__ == "__main__":
    raise SystemExit(main())
