"""Operation-local linked-commit reachability from one Git history walk."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from gobby.utils.daemon_git import GitOk, daemon_git


@dataclass(frozen=True, slots=True)
class CommitGraph:
    """Snapshot whose ancestor masks contain only the requested commits."""

    resolved: dict[str, str]
    parents: dict[str, tuple[str, ...]]
    members: dict[str, int]
    reachable: dict[str, int]

    @classmethod
    async def load(
        cls, refs: list[str], *, cwd: str | Path, timeout: float
    ) -> "CommitGraph | None":
        if not refs:
            return None
        canonical = await daemon_git.run(
            [
                "rev-parse",
                "--revs-only",
                "--end-of-options",
                *(f"{ref}^{{commit}}" for ref in refs),
            ],
            cwd=cwd,
            timeout=timeout,
        )
        if not isinstance(canonical, GitOk):
            return None
        shas = canonical.stdout.splitlines()
        if len(shas) != len(refs):
            return None
        history = await daemon_git.run(
            ["rev-list", "--topo-order", "--reverse", "--parents", *dict.fromkeys(shas)],
            cwd=cwd,
            timeout=timeout,
        )
        if not isinstance(history, GitOk):
            return None
        return await asyncio.to_thread(cls._parse, refs, shas, history.stdout)

    @classmethod
    def _parse(cls, refs: list[str], shas: list[str], history: str) -> "CommitGraph | None":
        members = {sha: 1 << index for index, sha in enumerate(dict.fromkeys(shas))}
        parents: dict[str, tuple[str, ...]] = {}
        reachable: dict[str, int] = {}
        for line in history.splitlines():
            fields = line.split()
            if not fields or fields[0] in parents:
                return None
            sha, *ancestors = fields
            if any(parent not in reachable for parent in ancestors):
                return None
            mask = members.get(sha, 0)
            for parent in ancestors:
                mask |= reachable[parent]
            parents[sha] = tuple(ancestors)
            reachable[sha] = mask
        if not members.keys() <= parents.keys():
            return None
        return cls(dict(zip(refs, shas, strict=True)), parents, members, reachable)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        ancestor = self.resolved.get(ancestor, ancestor)
        descendant = self.resolved.get(descendant, descendant)
        return bool(self.reachable[descendant] & self.members[ancestor])

    def is_merge(self, sha: str) -> bool:
        return len(self.parents[sha]) >= 2

    def is_sync_merge(self, sha: str, linked: list[str]) -> bool:
        """Require first-parent/post-merge proof and reject second-parent-only work."""
        if not self.is_merge(sha):
            return False
        first, second = self.parents[sha][:2]
        first_parent_work = False
        for other in linked:
            if other == sha:
                continue
            on_first = self.is_ancestor(other, first)
            if not on_first and self.is_ancestor(other, second):
                return False
            first_parent_work |= on_first or self.is_ancestor(sha, other)
        return first_parent_work

    def merge_view(self, sha: str, linked: list[str]) -> str:
        """The ``git show`` merge option for ``sha``: a sync merge shows only what it authored."""
        return "--remerge-diff" if self.is_sync_merge(sha, linked) else "--diff-merges=first-parent"
