"""Tests for the shared codewiki refresh service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.codewiki_refresh import (
    CodewikiGatewayConstructionError,
    CodewikiRefreshRequest,
    CodewikiRefreshService,
    changed_doc_paths,
)
from gobby.code_index.gcode_gateway import GcodeGatewayError

pytestmark = pytest.mark.unit


class FakeGcodeGateway:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[Path, Path, str | None, list[str] | None]] = []

    async def codewiki(
        self,
        project_root: Path,
        out_dir: Path,
        *,
        ai: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((project_root, out_dir, ai, scopes))
        return self.result


class FailingGcodeGateway:
    async def codewiki(
        self,
        _project_root: Path,
        _out_dir: Path,
        *,
        ai: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        _ = ai, scopes
        raise GcodeGatewayError("gcode failed")


class FakeGwikiGateway:
    def __init__(self) -> None:
        self.ingested: list[Path] = []
        self.index_count = 0

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        self.ingested.append(Path(path))
        return {"status": "ok"}

    async def index(self) -> dict[str, Any]:
        self.index_count += 1
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_refresh_runs_codewiki_indexes_changed_vault_docs(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md", "files/src/lib.rs.md"]})
    gwiki = FakeGwikiGateway()
    service = CodewikiRefreshService(
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
    )

    result = await service.refresh(
        CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1", ai="daemon")
    )

    assert gcode.calls == [(tmp_path, tmp_path / "wiki", "daemon", None)]
    assert gwiki.ingested == []
    assert gwiki.index_count == 1
    assert result.changed_count == 2
    assert result.indexed is True


@pytest.mark.asyncio
async def test_refresh_default_out_dir_honors_fallback_vault(tmp_path: Path) -> None:
    """A non-vault wiki collision routes generation into the gobby-wiki vault."""
    (tmp_path / "wiki").mkdir()
    fallback = tmp_path / "gobby-wiki"
    (fallback / "_gwiki").mkdir(parents=True)
    (fallback / "_gwiki" / "scope.json").write_text("{}\n", encoding="utf-8")
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md"]})
    gwiki = FakeGwikiGateway()
    service = CodewikiRefreshService(
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
    )

    result = await service.refresh(
        CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1", ai="daemon")
    )

    assert gcode.calls == [(tmp_path, fallback, "daemon", None)]
    assert result.out_dir == fallback.resolve()


@pytest.mark.asyncio
async def test_refresh_with_external_out_dir_ingests_changed_docs(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": ["repo.md", "files/src/lib.rs.md"]})
    gwiki = FakeGwikiGateway()
    out_dir = tmp_path / "external-codewiki"
    service = CodewikiRefreshService(
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: gwiki,
    )

    result = await service.refresh(
        CodewikiRefreshRequest(
            root_path=str(tmp_path),
            project_id="proj-1",
            out_dir=str(out_dir),
            ai="auto",
        )
    )

    assert gcode.calls == [(tmp_path, out_dir, "auto", None)]
    assert gwiki.ingested == [
        out_dir / "repo.md",
        out_dir / "files/src/lib.rs.md",
    ]
    assert gwiki.index_count == 1
    assert result.ingested_paths == tuple(gwiki.ingested)


async def test_refresh_passes_scopes_to_gcode_gateway(tmp_path: Path) -> None:
    gcode = FakeGcodeGateway({"changed_paths": []})
    service = CodewikiRefreshService(
        gcode_gateway_factory=lambda: gcode,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
    )

    result = await service.refresh(
        CodewikiRefreshRequest(
            root_path=str(tmp_path),
            project_id="proj-1",
            ai="daemon",
            scopes=["crates", "web", "src"],
        )
    )

    assert gcode.calls == [(tmp_path, tmp_path / "wiki", "daemon", ["crates", "web", "src"])]
    assert result.indexed is False


@pytest.mark.asyncio
async def test_refresh_raises_gateway_construction_error(tmp_path: Path) -> None:
    def fail_factory() -> FakeGcodeGateway:
        raise RuntimeError("constructor failed")

    service = CodewikiRefreshService(
        gcode_gateway_factory=fail_factory,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
    )

    with pytest.raises(CodewikiGatewayConstructionError, match="constructor failed"):
        await service.refresh(CodewikiRefreshRequest(root_path=str(tmp_path), project_id="proj-1"))


@pytest.mark.asyncio
async def test_refresh_propagates_gcode_failure(tmp_path: Path) -> None:
    service = CodewikiRefreshService(
        gcode_gateway_factory=FailingGcodeGateway,
        gwiki_gateway_factory=lambda _root: FakeGwikiGateway(),
    )

    with pytest.raises(GcodeGatewayError, match="gcode failed"):
        await service.refresh(CodewikiRefreshRequest(root_path=str(tmp_path)))


def test_changed_doc_paths_ignores_invalid_and_escaping_paths(tmp_path: Path) -> None:
    out_dir = tmp_path / "gobby-wiki"

    paths = changed_doc_paths(
        out_dir,
        {
            "changed_paths": [
                "repo.md",
                "",
                None,
                "../outside.md",
                str(out_dir / "nested.md"),
            ]
        },
    )

    assert paths == [out_dir / "repo.md", out_dir / "nested.md"]
