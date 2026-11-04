"""The compose stack that gobby install writes disables Qdrant telemetry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.cli.installers import postgres
from gobby.cli.installers.postgres import reconcile_unified_compose

pytestmark = pytest.mark.unit

TELEMETRY_SETTING = "QDRANT__TELEMETRY_DISABLED=true"


def _qdrant_environment(compose_text: str) -> list[str]:
    document: dict[str, Any] = yaml.safe_load(compose_text)
    environment: list[str] = document["services"]["qdrant"]["environment"]
    return environment


def test_bundled_stack_disables_qdrant_telemetry() -> None:
    compose_text = postgres._COMPOSE_SRC.read_text(encoding="utf-8")

    assert TELEMETRY_SETTING in _qdrant_environment(compose_text)


def test_stack_written_by_install_disables_qdrant_telemetry(tmp_path: Path) -> None:
    reconciliation = reconcile_unified_compose(tmp_path / "services")

    written = reconciliation.compose_file.read_text(encoding="utf-8")
    assert TELEMETRY_SETTING in _qdrant_environment(written)


def test_deployed_stack_without_the_setting_gains_it_on_reinstall(tmp_path: Path) -> None:
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    stale = postgres._COMPOSE_SRC.read_text(encoding="utf-8").replace(
        f"      - {TELEMETRY_SETTING}\n", ""
    )
    assert TELEMETRY_SETTING not in _qdrant_environment(stale)
    (services_dir / "docker-compose.yml").write_text(stale, encoding="utf-8")

    reconciliation = reconcile_unified_compose(services_dir)

    assert reconciliation.refreshed is True
    written = reconciliation.compose_file.read_text(encoding="utf-8")
    assert TELEMETRY_SETTING in _qdrant_environment(written)
