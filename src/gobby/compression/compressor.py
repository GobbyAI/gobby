"""OutputCompressor orchestrator.

Selects a compression pipeline based on the command string and applies
composable primitives (filter, group, truncate, dedup) to reduce output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from gobby.compression.primitives import dedup, filter_lines, group_lines, truncate

logger = logging.getLogger(__name__)


@dataclass
class CompressionResult:
    """Result of compressing command output."""

    compressed: str
    original_chars: int
    compressed_chars: int
    strategy_name: str

    @property
    def savings_pct(self) -> float:
        if self.original_chars == 0:
            return 0.0
        return (1 - self.compressed_chars / self.original_chars) * 100


# Each pipeline entry: (regex_pattern, pipeline_name, list_of_primitive_configs)
# Primitives are applied in order. Each is a (primitive_func, kwargs) tuple.
PipelineSpec = list[tuple[Any, dict[str, Any]]]

# Command pattern -> (name, pipeline)
_COMMAND_PIPELINES: list[tuple[str, str, PipelineSpec]] = [
    # === Git ===
    (
        r"\bgit\s+status\b",
        "git-status",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s*$",
                        r"^On branch ",
                        r"^Your branch is ",
                        r"^  \(use \"git ",
                        r"^Changes not staged",
                        r"^Changes to be committed",
                        r"^Untracked files:",
                        r"^no changes added",
                    ]
                },
            ),
            (group_lines, {"mode": "git_status"}),
        ],
    ),
    (
        r"\bgit\s+diff\b",
        "git-diff",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^index [0-9a-f]",
                        r"^diff --git",
                        r"^--- ",
                        r"^\+\+\+ ",
                    ]
                },
            ),
            (truncate, {"per_file_lines": 50, "file_marker": r"^@@\s"}),
        ],
    ),
    (
        r"\bgit\s+log\b",
        "git-log",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^Merge:",
                        r"^\s*$",
                    ]
                },
            ),
            (truncate, {"head": 40, "tail": 5}),
        ],
    ),
    (
        r"\bgit\s+(?:push|pull|fetch|clone)\b",
        "git-transfer",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^remote: Counting",
                        r"^remote: Compressing",
                        r"^remote: Total",
                        r"^Receiving objects:",
                        r"^Resolving deltas:",
                        r"^Unpacking objects:",
                        r"^remote: Enumerating",
                    ]
                },
            ),
            (truncate, {"head": 5, "tail": 5}),
        ],
    ),
    (
        r"\bgit\s+(?:add|commit|stash|tag|branch)\b",
        "git-mutation",
        [
            (filter_lines, {"patterns": [r"^\s*$"]}),
            (truncate, {"head": 5, "tail": 5}),
        ],
    ),
    # === Test runners ===
    (
        r"\b(?:pytest|py\.test)\b",
        "pytest",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^tests/.*PASSED",
                        r"^\s*$",
                        r"^=+ ?test session starts",
                        r"^platform ",
                        r"^plugins:",
                        r"^cachedir:",
                        r"^rootdir:",
                        r"^configfile:",
                        r"^collecting ",
                        r"^collected \d+ items?$",
                    ]
                },
            ),
            (group_lines, {"mode": "pytest_failures"}),
            (truncate, {"head": 50, "tail": 20}),
        ],
    ),
    (
        r"\bcargo\s+test\b",
        "cargo-test",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s+Running ",
                        r"^test .* \.\.\. ok$",
                        r"^\s*$",
                        r"^   Compiling ",
                        r"^    Finished ",
                    ]
                },
            ),
            (group_lines, {"mode": "test_failures"}),
            (truncate, {"head": 50, "tail": 20}),
        ],
    ),
    (
        r"\b(?:npm\s+test|vitest|jest|mocha|go\s+test)\b",
        "generic-test",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s*$",
                        r"^\s*✓",
                        r"^\s*PASS\s",
                        r"^ok\s+\S+\s+\d",
                    ]
                },
            ),
            (group_lines, {"mode": "test_failures"}),
            (truncate, {"head": 50, "tail": 20}),
        ],
    ),
    # === Linters ===
    (
        r"\b(?:ruff|mypy|pylint)\b",
        "python-lint",
        [
            (dedup, {}),
            (group_lines, {"mode": "lint_by_rule"}),
            (truncate, {"head": 60, "tail": 10}),
        ],
    ),
    (
        r"\b(?:eslint|tsc|biome|oxlint)\b",
        "js-lint",
        [
            (dedup, {}),
            (group_lines, {"mode": "lint_by_rule"}),
            (truncate, {"head": 60, "tail": 10}),
        ],
    ),
    (
        r"\b(?:golangci-lint|staticcheck)\b",
        "go-lint",
        [
            (dedup, {}),
            (group_lines, {"mode": "lint_by_rule"}),
            (truncate, {"head": 60, "tail": 10}),
        ],
    ),
    # === File operations ===
    (
        r"\b(?:ls|tree)\b",
        "ls-tree",
        [
            (group_lines, {"mode": "by_extension"}),
            (truncate, {"head": 40, "tail": 10}),
        ],
    ),
    (
        r"\bfind\b",
        "find",
        [
            (group_lines, {"mode": "by_directory"}),
            (truncate, {"head": 40, "tail": 10}),
        ],
    ),
    (
        r"\b(?:grep|rg|ripgrep)\b",
        "grep",
        [
            (dedup, {}),
            (group_lines, {"mode": "by_file"}),
            (truncate, {"head": 40, "tail": 10}),
        ],
    ),
    # === Build tools ===
    (
        r"\b(?:cargo\s+build|go\s+build|next\s+build|webpack|make)\b",
        "build",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s*Compiling ",
                        r"^\s*Building ",
                        r"^\s*Downloading ",
                        r"^\s*Downloaded ",
                        r"^\[.*\]\s*\d+%",
                        r"^\s*$",
                    ]
                },
            ),
            (group_lines, {"mode": "errors_warnings"}),
            (truncate, {"head": 30, "tail": 10}),
        ],
    ),
    # === Package management ===
    (
        r"\b(?:pip\s+(?:install|list)|npm\s+(?:install|ls|list)|uv\s+(?:pip|sync|add))\b",
        "package-mgmt",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s*Downloading ",
                        r"^\s*Installing ",
                        r"^\s*Using cached ",
                        r"^\s*Collecting ",
                        r"^npm warn",
                        r"^\s*$",
                        r"^added \d+ packages",
                    ]
                },
            ),
            (truncate, {"head": 10, "tail": 10}),
        ],
    ),
    # === Container/k8s ===
    (
        r"\bdocker\s+(?:ps|images)\b",
        "docker-list",
        [
            (truncate, {"head": 30, "tail": 5}),
        ],
    ),
    (
        r"\b(?:docker\s+logs|kubectl\s+logs)\b",
        "container-logs",
        [
            (dedup, {}),
            (truncate, {"head": 30, "tail": 20}),
        ],
    ),
    # === GitHub CLI ===
    (
        r"\bgh\s+(?:pr|issue)\s+(?:list|view)\b",
        "gh-cli",
        [
            (filter_lines, {"patterns": [r"^\s*$"]}),
            (truncate, {"head": 30, "tail": 5}),
        ],
    ),
    # === Utility ===
    (
        r"\b(?:wget|curl)\b",
        "download",
        [
            (
                filter_lines,
                {
                    "patterns": [
                        r"^\s*%\s+Total",
                        r"^\s*\d+\s+\d+",
                        r"^  % Total",
                        r"^\s*$",
                        r"^###",
                    ]
                },
            ),
            (truncate, {"head": 10, "tail": 10}),
        ],
    ),
]


@lru_cache(maxsize=1)
def _get_pipelines() -> list[tuple[re.Pattern[str], str, PipelineSpec]]:
    """Compile command pipeline regexes (cached, thread-safe via lru_cache)."""
    return [
        (re.compile(pattern, re.IGNORECASE), name, pipeline)
        for pattern, name, pipeline in _COMMAND_PIPELINES
    ]


class OutputCompressor:
    """Compresses command output using composable primitives."""

    def __init__(
        self,
        min_length: int = 2000,
        max_lines: int = 100,
        excluded_commands: list[str] | None = None,
    ):
        self.min_length = min_length
        self.max_lines = max_lines
        self._excluded_patterns = [re.compile(p) for p in (excluded_commands or [])]

    def compress(self, command: str, output: str) -> CompressionResult:
        """Compress command output using matched pipeline.

        Args:
            command: The command string that produced the output.
            output: The raw stdout+stderr output.

        Returns:
            CompressionResult with compressed text and stats.
        """
        original_chars = len(output)

        # Skip if too short
        if original_chars < self.min_length:
            return CompressionResult(
                compressed=output,
                original_chars=original_chars,
                compressed_chars=original_chars,
                strategy_name="passthrough",
            )

        # Skip excluded commands
        for pattern in self._excluded_patterns:
            if pattern.search(command):
                return CompressionResult(
                    compressed=output,
                    original_chars=original_chars,
                    compressed_chars=original_chars,
                    strategy_name="excluded",
                )

        # Find matching pipeline
        strategy_name = "fallback"
        lines = output.splitlines(keepends=True)

        for regex, name, pipeline in _get_pipelines():
            if regex.search(command):
                strategy_name = name
                for primitive_fn, kwargs in pipeline:
                    lines = primitive_fn(lines, **kwargs)
                break
        else:
            # Fallback: truncate to head+tail
            lines = truncate(lines, head=20, tail=20)

        # Apply max_lines as a final cap (post-pipeline)
        if self.max_lines and len(lines) > self.max_lines:
            cap_head = (self.max_lines * 3) // 5  # 60% head
            cap_tail = self.max_lines - cap_head  # 40% tail
            lines = truncate(lines, head=cap_head, tail=cap_tail)

        compressed = "".join(lines)
        compressed_chars = len(compressed)

        # If compression produced empty/whitespace-only output, pass through original
        if not compressed.strip():
            return CompressionResult(
                compressed=output,
                original_chars=original_chars,
                compressed_chars=original_chars,
                strategy_name="passthrough",
            )

        # If compression didn't help much, return original
        if compressed_chars >= original_chars * 0.95:
            return CompressionResult(
                compressed=output,
                original_chars=original_chars,
                compressed_chars=original_chars,
                strategy_name="passthrough",
            )

        return CompressionResult(
            compressed=compressed,
            original_chars=original_chars,
            compressed_chars=compressed_chars,
            strategy_name=strategy_name,
        )
