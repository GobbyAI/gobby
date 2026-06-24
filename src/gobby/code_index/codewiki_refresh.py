"""Shared codewiki refresh service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.gwiki_gateway import GwikiGateway

DEFAULT_CODEWIKI_OUT_DIR = "gobby-wiki"
CODEWIKI_AI_VALUES = {"auto", "daemon", "direct", "off"}


class CodewikiGenerator(Protocol):
    async def codewiki(
        self,
        project_root: Path,
        out_dir: Path,
        *,
        ai: str | None = None,
    ) -> dict[str, Any]: ...


class WikiIndexer(Protocol):
    async def ingest_file(self, path: str | Path) -> dict[str, Any]: ...

    async def index(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CodewikiRefreshRequest:
    root_path: str
    project_id: str | None = None
    out_dir: str | None = None
    ai: str = "auto"


@dataclass(frozen=True)
class CodewikiRefreshResult:
    root: Path
    out_dir: Path
    changed_paths: tuple[Path, ...]
    ingested_paths: tuple[Path, ...]
    indexed: bool

    @property
    def changed_count(self) -> int:
        return len(self.changed_paths)


class CodewikiGatewayConstructionError(RuntimeError):
    """Raised when refresh gateway construction fails."""

    def __init__(self, target: str, original: BaseException) -> None:
        self.target = target
        self.original = original
        super().__init__(f"codewiki refresh gateway construction failed for {target}: {original}")


class CodewikiRefreshService:
    """Runs gcode codewiki and indexes generated docs through gwiki."""

    def __init__(
        self,
        *,
        gcode_gateway_factory: Callable[[], CodewikiGenerator] = GcodeGateway,
        gwiki_gateway_factory: Callable[[Path], WikiIndexer] | None = None,
        default_out_dir: str = DEFAULT_CODEWIKI_OUT_DIR,
    ) -> None:
        self._gcode_gateway_factory = gcode_gateway_factory
        self._gwiki_gateway_factory = gwiki_gateway_factory or (
            # Codewiki refresh can ingest many generated docs; keep the longer
            # timeout local to this path instead of widening route defaults.
            lambda root: GwikiGateway(
                project_root=root,
                timeout_seconds=120.0,
            )
        )
        self._default_out_dir = default_out_dir

    async def refresh(self, request: CodewikiRefreshRequest) -> CodewikiRefreshResult:
        root = Path(request.root_path).resolve(strict=False)
        out_dir = self.resolve_out_dir(root, request.out_dir)
        try:
            gcode = self._gcode_gateway_factory()
            gwiki = self._gwiki_gateway_factory(root)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CodewikiGatewayConstructionError(request.project_id or str(root), exc) from exc

        result = await gcode.codewiki(root, out_dir, ai=normalize_codewiki_ai(request.ai))
        changed_paths = tuple(changed_doc_paths(out_dir, result))
        ingested_paths: list[Path] = []
        if changed_paths:
            if not out_dir.is_relative_to(self.default_out_dir(root)):
                for path in changed_paths:
                    await gwiki.ingest_file(path)
                    ingested_paths.append(path)
            await gwiki.index()
        return CodewikiRefreshResult(
            root=root,
            out_dir=out_dir,
            changed_paths=changed_paths,
            ingested_paths=tuple(ingested_paths),
            indexed=bool(changed_paths),
        )

    def resolve_out_dir(self, root: Path, out_dir: str | None) -> Path:
        value = out_dir or self._default_out_dir
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        return path.resolve(strict=False)

    def default_out_dir(self, root: Path) -> Path:
        return (root / self._default_out_dir).resolve(strict=False)


def normalize_codewiki_ai(value: str | None) -> str:
    ai = (value or "auto").strip().lower()
    if ai not in CODEWIKI_AI_VALUES:
        raise ValueError("ai must be one of auto, daemon, direct, off")
    return ai


def changed_doc_paths(out_dir: Path, result: dict[str, Any]) -> list[Path]:
    changed = result.get("changed_paths")
    if not isinstance(changed, list):
        return []

    paths: list[Path] = []
    for value in changed:
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value)
        if not path.is_absolute():
            path = out_dir / path
        resolved = path.resolve(strict=False)
        if resolved.is_relative_to(out_dir):
            paths.append(resolved)
    return paths
