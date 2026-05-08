"""Merge conflict resolver with tiered resolution strategy.

Implements a four-tier resolution strategy:
1. Git auto-merge (no conflicts)
2. Conflict-only AI resolution (sends only conflict hunks to LLM)
3. Full-file AI resolution (sends entire file for complex conflicts)
4. Human review fallback (marks as needs-human-review)
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.llm.service import LLMService

logger = logging.getLogger(__name__)


def _conflict_file_path(conflict: dict[str, Any]) -> Path:
    file_path = Path(str(conflict.get("file", "unknown")))
    worktree_path = conflict.get("worktree_path")
    if file_path.is_absolute() or not worktree_path:
        return file_path
    return Path(str(worktree_path)) / file_path


# Patterns for files that always conflict trivially in merges
# These are append-only sync files that get re-synced from the DB anyway
TRIVIAL_CONFLICT_PATTERNS = (
    ".gobby/tasks.jsonl",
    ".gobby/memories.jsonl",
)


async def auto_resolve_trivial_conflicts(
    conflicted_files: list[str],
    worktree_path: str,
) -> list[str]:
    """Auto-resolve trivial conflicts (.gobby/*.jsonl) and return remaining conflicts.

    Trivial conflicts are append-only sync files that always conflict in merges
    but don't carry meaningful merge semantics — they get re-synced from the DB.
    We resolve them by accepting the incoming (theirs) version.

    Args:
        conflicted_files: List of conflicted file paths (relative to worktree)
        worktree_path: Absolute path to the git worktree

    Returns:
        List of remaining non-trivial conflicted files
    """
    trivial = []
    remaining = []

    for f in conflicted_files:
        if any(f == pattern or f.endswith(pattern) for pattern in TRIVIAL_CONFLICT_PATTERNS):
            trivial.append(f)
        else:
            remaining.append(f)

    if not trivial:
        return conflicted_files

    resolved: list[str] = []
    for f in trivial:
        # Accept incoming version for trivial files
        checkout = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "--theirs",
            f,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, co_stderr = await checkout.communicate()
        if checkout.returncode != 0:
            logger.error(f"git checkout --theirs failed for {f}: {co_stderr.decode().strip()}")
            remaining.append(f)
            continue

        # Stage the resolution
        add = await asyncio.create_subprocess_exec(
            "git",
            "add",
            f,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, add_stderr = await add.communicate()
        if add.returncode != 0:
            logger.error(f"git add failed for {f}: {add_stderr.decode().strip()}")
            remaining.append(f)
            continue

        resolved.append(f)

    logger.info(
        f"Auto-resolved {len(resolved)} trivial conflict(s) in worktree {worktree_path}: {resolved}"
    )

    return remaining


_CONFLICT_BLOCK_RE = re.compile(
    r"<<<<<<< [^\n]*\n.*?\n=======[ \t]*\n.*?\n>>>>>>> [^\n]*\n",
    re.DOTALL,
)


def splice_resolutions_into_file(
    file_content: str,
    hunk_resolutions: list[str],
) -> str | None:
    """Splice LLM-resolved hunks back into a file with conflict markers.

    Replaces each `<<<<<<<...=======...>>>>>>>` block with the corresponding
    entry from hunk_resolutions, preserving surrounding content.

    Returns None when the conflict-block count does not match the resolution
    count — caller should fall through to a different tier.
    """
    matches = list(_CONFLICT_BLOCK_RE.finditer(file_content))
    if len(matches) != len(hunk_resolutions):
        return None

    out: list[str] = []
    last_end = 0
    for match, replacement in zip(matches, hunk_resolutions, strict=True):
        out.append(file_content[last_end : match.start()])
        normalized = replacement.strip("\n")
        if normalized:
            out.append(normalized + "\n")
        last_end = match.end()
    out.append(file_content[last_end:])
    return "".join(out)


class ResolutionTier(Enum):
    """Resolution strategy tiers, from fastest to most expensive."""

    GIT_AUTO = "git_auto"
    CONFLICT_ONLY_AI = "conflict_only_ai"
    FULL_FILE_AI = "full_file_ai"
    HUMAN_REVIEW = "human_review"


# Alias for spec compatibility
ResolutionStrategy = ResolutionTier


@dataclass
class MergeResult:
    """Result of a merge resolution attempt.

    Attributes:
        success: Whether the merge was fully resolved
        tier: The tier that completed the resolution (or escalated to)
        conflicts: List of conflicts found during merge
        resolved_files: List of files that were successfully resolved
        unresolved_conflicts: List of conflicts that could not be resolved
        needs_human_review: Whether manual intervention is required
        resolved_content_by_file: Map of file path -> full resolved file content,
            populated by AI tiers so callers can write the resolution to disk.
    """

    success: bool
    tier: ResolutionTier
    conflicts: list[dict[str, Any]]
    resolved_files: list[str] = field(default_factory=list)
    unresolved_conflicts: list[dict[str, Any]] = field(default_factory=list)
    needs_human_review: bool = False
    resolved_content_by_file: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "tier": self.tier.value,
            "conflicts": self.conflicts,
            "resolved_files": self.resolved_files,
            "unresolved_conflicts": self.unresolved_conflicts,
            "needs_human_review": self.needs_human_review,
            "resolved_content_by_file": self.resolved_content_by_file,
        }


# Alias for spec compatibility
ResolutionResult = MergeResult


class MergeResolver:
    """Merge conflict resolver with tiered strategy.

    Attempts resolution in order of increasing complexity/cost:
    1. Git auto-merge
    2. Conflict-only AI resolution
    3. Full-file AI resolution
    4. Human review fallback
    """

    def __init__(
        self,
        conflict_size_threshold: int = 100,
        max_parallel_files: int = 5,
        *,
        llm_service: "LLMService | None" = None,
        config: Any | None = None,
    ):
        """Initialize MergeResolver.

        Args:
            conflict_size_threshold: Lines of conflict above which to escalate to full-file
            max_parallel_files: Maximum files to resolve in parallel
            llm_service: Optional LLM service for AI conflict resolution
            config: Optional MergeResolutionConfig for provider/model selection
        """
        self.conflict_size_threshold = conflict_size_threshold
        self.max_parallel_files = max_parallel_files
        self._llm_service: LLMService | None = llm_service
        self._config: Any | None = config

    @property
    def llm_service(self) -> "LLMService | None":
        return self._llm_service

    @llm_service.setter
    def llm_service(self, service: "LLMService | None") -> None:
        self._llm_service = service

    @property
    def config(self) -> Any | None:
        return self._config

    @config.setter
    def config(self, config: Any | None) -> None:
        self._config = config

    async def resolve_file(
        self,
        path: Path | str,
        conflict_hunks: list[Any],
        worktree_path: Path | str | None = None,
    ) -> "ResolutionResult":
        """Resolve conflicts in a single file using tiered strategy.

        Args:
            path: Path to the file with conflicts
            conflict_hunks: List of ConflictHunk objects or conflict dicts
            worktree_path: Worktree root used to resolve relative file paths

        Returns:
            ResolutionResult with resolution status
        """
        file_path = str(path) if isinstance(path, Path) else path

        # Convert hunks to conflict dict format
        conflict = {
            "file": file_path,
            "hunks": conflict_hunks,
            "worktree_path": str(worktree_path) if worktree_path is not None else None,
        }

        # Check if conflict is too large for conflict-only resolution
        def get_hunk_lines(h: Any) -> int:
            """Get line count from hunk, handling both objects and dicts."""
            if isinstance(h, dict):
                ours = h.get("ours", "")
                theirs = h.get("theirs", "")
            else:
                ours = getattr(h, "ours", "")
                theirs = getattr(h, "theirs", "")
            return len(ours.split("\n")) + len(theirs.split("\n"))

        total_lines = sum(get_hunk_lines(h) for h in conflict_hunks)

        # Tier 2: Try conflict-only if under threshold
        if total_lines <= self.conflict_size_threshold:
            result = await self._resolve_conflicts_only([conflict])
            if result["success"]:
                content_by_file = {
                    r["file"]: r["content"]
                    for r in result.get("resolutions", [])
                    if r.get("content")
                }
                return ResolutionResult(
                    success=True,
                    tier=ResolutionTier.CONFLICT_ONLY_AI,
                    conflicts=[conflict],
                    resolved_files=[file_path],
                    unresolved_conflicts=[],
                    needs_human_review=False,
                    resolved_content_by_file=content_by_file,
                )

        # Tier 3: Full-file resolution
        result = await self._resolve_full_file([conflict])
        if result["success"]:
            content_by_file = {
                r["file"]: r["content"] for r in result.get("resolutions", []) if r.get("content")
            }
            return ResolutionResult(
                success=True,
                tier=ResolutionTier.FULL_FILE_AI,
                conflicts=[conflict],
                resolved_files=[file_path],
                unresolved_conflicts=[],
                needs_human_review=False,
                resolved_content_by_file=content_by_file,
            )

        # Tier 4: Human review fallback
        return ResolutionResult(
            success=False,
            tier=ResolutionTier.HUMAN_REVIEW,
            conflicts=[conflict],
            resolved_files=[],
            unresolved_conflicts=[conflict],
            needs_human_review=True,
        )

    async def resolve(
        self,
        worktree_path: str,
        source_branch: str,
        target_branch: str,
        force_tier: ResolutionTier | None = None,
    ) -> MergeResult:
        """Resolve merge conflicts using tiered strategy.

        Args:
            worktree_path: Path to the git worktree
            source_branch: Branch being merged in
            target_branch: Target branch (e.g., main)
            force_tier: Optional tier to force (skips lower tiers)

        Returns:
            MergeResult with resolution status and details
        """
        # Tier 1: Git auto-merge (unless forcing a higher tier)
        if force_tier is None or force_tier == ResolutionTier.GIT_AUTO:
            git_result = await self._git_merge(worktree_path, source_branch, target_branch)

            if git_result["success"]:
                return MergeResult(
                    success=True,
                    tier=ResolutionTier.GIT_AUTO,
                    conflicts=[],
                    resolved_files=[],
                    unresolved_conflicts=[],
                    needs_human_review=False,
                )

            conflicts = git_result.get("conflicts", [])
        else:
            # Skipping git merge, assume conflicts exist
            conflicts = []

        # If forcing full-file AI, skip tier 2
        if force_tier == ResolutionTier.FULL_FILE_AI:
            return await self._try_full_file_resolution(worktree_path, conflicts or [{}])

        # Tier 2: Conflict-only AI resolution
        if conflicts:
            tier2_result = await self._resolve_conflicts_only(conflicts)

            if tier2_result["success"]:
                return MergeResult(
                    success=True,
                    tier=ResolutionTier.CONFLICT_ONLY_AI,
                    conflicts=conflicts,
                    resolved_files=[c.get("file", "") for c in conflicts],
                    unresolved_conflicts=[],
                    needs_human_review=False,
                )

            # Tier 3: Full-file AI resolution
            return await self._try_full_file_resolution(worktree_path, conflicts)

        # No conflicts from git, but no git result - unusual state
        return MergeResult(
            success=True,
            tier=ResolutionTier.GIT_AUTO,
            conflicts=[],
            resolved_files=[],
            unresolved_conflicts=[],
            needs_human_review=False,
        )

    async def _try_full_file_resolution(
        self,
        worktree_path: str,
        conflicts: list[dict[str, Any]],
    ) -> MergeResult:
        """Attempt Tier 3 full-file resolution, fallback to human review."""
        tier3_result = await self._resolve_full_file(conflicts)

        if tier3_result["success"]:
            return MergeResult(
                success=True,
                tier=ResolutionTier.FULL_FILE_AI,
                conflicts=conflicts,
                resolved_files=[c.get("file", "") for c in conflicts],
                unresolved_conflicts=[],
                needs_human_review=False,
            )

        # Tier 4: Human review fallback
        return MergeResult(
            success=False,
            tier=ResolutionTier.HUMAN_REVIEW,
            conflicts=conflicts,
            resolved_files=[],
            unresolved_conflicts=conflicts,
            needs_human_review=True,
        )

    async def _git_merge(
        self,
        worktree_path: str,
        source_branch: str,
        target_branch: str,
    ) -> dict[str, Any]:
        """Attempt git auto-merge.

        Args:
            worktree_path: Path to git worktree
            source_branch: Branch to merge in
            target_branch: Target branch

        Returns:
            Dict with 'success' bool and 'conflicts' list if any
        """
        # Run git merge without committing
        # Merge target INTO the worktree branch (worktree is on source_branch)
        merge_ref = (
            f"origin/{target_branch}" if not target_branch.startswith("origin/") else target_branch
        )
        process = await asyncio.create_subprocess_exec(
            "git",
            "merge",
            "--no-commit",
            "--no-ff",
            merge_ref,
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()

        if process.returncode == 0:
            return {"success": True, "conflicts": []}

        # Merge failed, find conflicting files
        diff_process = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--name-only",
            "--diff-filter=U",
            cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await diff_process.communicate()
        conflicted_files = stdout.decode().strip().splitlines()

        from gobby.worktrees.merge.conflict_parser import extract_conflict_hunks

        conflicts = []
        for file_rel_path in conflicted_files:
            file_path = Path(worktree_path) / file_rel_path
            try:
                content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
                hunks = extract_conflict_hunks(content)
                if hunks:
                    conflicts.append(
                        {
                            "file": str(file_rel_path),
                            "hunks": hunks,
                            "worktree_path": worktree_path,
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to parse conflicts in {file_rel_path}: {e}")

        return {"success": False, "conflicts": conflicts}

    async def _resolve_conflicts_only(
        self,
        conflicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve conflicts by sending only conflict hunks to LLM.

        Args:
            conflicts: List of conflict dicts with hunks

        Returns:
            Dict with 'success' bool and 'resolutions' list
        """
        if not self._llm_service:
            logger.warning("No LLM service available for resolution")
            return {"success": False, "resolutions": []}

        resolutions = []
        for conflict in conflicts:
            file_path = conflict.get("file", "unknown")
            hunks = conflict.get("hunks", [])

            prompt = f"Resolve the following merge conflicts in {file_path}. Return ONLY the resolved code content for each hunk.\n\n"

            for i, hunk in enumerate(hunks):
                # Handle both dict and object hunk formats
                if isinstance(hunk, dict):
                    ours = hunk.get("ours", "")
                    theirs = hunk.get("theirs", "")
                else:
                    ours = getattr(hunk, "ours", "")
                    theirs = getattr(hunk, "theirs", "")

                prompt += f"CONFLICT {i + 1}:\n"
                prompt += f"<<<<<<< HEAD\n{ours}\n=======\n{theirs}\n>>>>>>> INCOMING\n\n"

            prompt += "Provide the resolved code for each conflict hunk, separated by '---HUNK SEPARATOR---'."

            try:
                if self._config:
                    try:
                        provider, model, _ = self._llm_service.get_provider_for_feature(
                            self._config
                        )
                    except (ValueError, Exception):
                        provider = self._llm_service.get_default_provider()
                        model = None
                else:
                    provider = self._llm_service.get_default_provider()
                    model = None
                response = await provider.generate_text(
                    prompt,
                    model=model,
                    caller="worktrees.merge.resolve_hunks",
                )

                if not response:
                    return {"success": False, "resolutions": []}

                resolved_hunks = [
                    chunk.strip("\n") for chunk in response.split("---HUNK SEPARATOR---")
                ]
                resolved_hunks = [h for h in resolved_hunks if h]
                if not resolved_hunks:
                    resolved_hunks = [response.strip("\n")]

                try:
                    file_with_markers = await asyncio.to_thread(
                        _conflict_file_path(conflict).read_text, encoding="utf-8"
                    )
                except OSError as read_err:
                    logger.error(f"Failed to read {file_path} for hunk splicing: {read_err}")
                    return {"success": False, "resolutions": []}

                spliced = splice_resolutions_into_file(file_with_markers, resolved_hunks)
                if spliced is None:
                    logger.warning(
                        f"Hunk count mismatch splicing {file_path}: "
                        f"file has {len(_CONFLICT_BLOCK_RE.findall(file_with_markers))} "
                        f"conflict blocks, LLM returned {len(resolved_hunks)} hunks"
                    )
                    return {"success": False, "resolutions": []}

                resolutions.append(
                    {
                        "file": file_path,
                        "content": spliced,
                        "hunks_resolved": len(resolved_hunks),
                    }
                )
            except Exception as e:
                logger.error(f"LLM resolution failed for {file_path}: {e}")
                return {"success": False, "resolutions": []}

        return {"success": True, "resolutions": resolutions}

    async def _resolve_full_file(
        self,
        conflicts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve conflicts by sending full file content to LLM.

        Args:
            conflicts: List of conflict dicts

        Returns:
            Dict with 'success' bool and 'resolutions' list
        """
        if not self._llm_service:
            logger.warning("No LLM service available for resolution")
            return {"success": False, "resolutions": []}

        resolutions = []
        for conflict in conflicts:
            file_path = conflict.get("file", "unknown")

            try:
                # In a real scenario, we'd read the file content with markers here
                # But typically the file on disk already has markers if git merge failed
                content_with_markers = await asyncio.to_thread(
                    _conflict_file_path(conflict).read_text, encoding="utf-8"
                )

                prompt = f"Resolve all merge conflicts in the following file {file_path}. Return the FULL resolved file content.\n\n"
                prompt += content_with_markers

                if self._config:
                    try:
                        provider, model, _ = self._llm_service.get_provider_for_feature(
                            self._config
                        )
                    except (ValueError, Exception):
                        provider = self._llm_service.get_default_provider()
                        model = None
                else:
                    provider = self._llm_service.get_default_provider()
                    model = None
                response = await provider.generate_text(
                    prompt,
                    model=model,
                    caller="worktrees.merge.resolve_full_file",
                )

                if response:
                    resolutions.append({"file": file_path, "content": response})
                else:
                    return {"success": False, "resolutions": []}
            except Exception as e:
                logger.error(f"Full file resolution failed for {file_path}: {e}")
                return {"success": False, "resolutions": []}

        return {"success": True, "resolutions": resolutions}

    async def _resolve_file_conflict(
        self,
        conflict: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a single file's conflicts.

        Args:
            conflict: Conflict dict for one file

        Returns:
            Dict with 'success' bool
        """
        # Try conflict-only first
        result = await self._resolve_conflicts_only([conflict])
        if result["success"]:
            return {"success": True}

        # Escalate to full-file
        result = await self._resolve_full_file([conflict])
        return result

    async def resolve_conflicts_parallel(
        self,
        worktree_path: str,
        conflicts: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Resolve multiple file conflicts in parallel.

        Args:
            worktree_path: Path to git worktree
            conflicts: List of conflicts to resolve

        Returns:
            Tuple of (resolved_files, unresolved_conflicts)
        """
        semaphore = asyncio.Semaphore(self.max_parallel_files)

        async def resolve_with_limit(conflict: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                result = await self._resolve_file_conflict(
                    {**conflict, "worktree_path": worktree_path}
                )
                return {"conflict": conflict, "result": result}

        tasks = [resolve_with_limit(c) for c in conflicts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        resolved_files: list[str] = []
        unresolved: list[dict[str, Any]] = []

        for r in results:
            if isinstance(r, BaseException):
                logger.error(f"Error resolving conflict: {r}")
                continue

            # r is now dict[str, Any] after the isinstance check
            result_dict: dict[str, Any] = r
            if result_dict["result"].get("success"):
                resolved_files.append(result_dict["conflict"].get("file", ""))
            else:
                unresolved.append(result_dict["conflict"])

        return resolved_files, unresolved
