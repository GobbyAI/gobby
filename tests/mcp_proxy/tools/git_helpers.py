from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
