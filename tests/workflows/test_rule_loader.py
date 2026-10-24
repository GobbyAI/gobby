"""Red tests for bundled rule loader tombstones."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def test_deprecated_rules_not_synced(temp_db: HubDatabase, tmp_path: Path) -> None:
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.workflows.sync_rules import sync_bundled_rules

    rules_dir = tmp_path / "rules"
    deprecated_dir = rules_dir / "build" / "deprecated"
    deprecated_dir.mkdir(parents=True)
    (rules_dir / "build").mkdir(exist_ok=True)
    (rules_dir / "build" / "active.yaml").write_text(
        """
rules:
  active-build-rule:
    event: turn_end
    effect:
      type: block
      reason: active
""",
        encoding="utf-8",
    )
    (deprecated_dir / "old.yaml").write_text(
        """
rules:
  old-build-rule:
    event: turn_end
    effect:
      type: block
      reason: old
""",
        encoding="utf-8",
    )

    sync_bundled_rules(temp_db, rules_path=rules_dir)
    manager = RuleDefinitionManager(temp_db)

    assert manager.get_by_name("active-build-rule") is not None
    assert manager.get_by_name("old-build-rule") is None


def test_build_rules_are_retired(temp_db: HubDatabase) -> None:
    from gobby.paths import get_install_dir
    from gobby.storage.definitions.rules import RuleDefinitionManager
    from gobby.workflows.sync_rules import get_bundled_rules_paths, sync_bundled_rules

    assert get_bundled_rules_paths() == [get_install_dir() / "shared" / "workflows" / "rules"]
    sync_bundled_rules(temp_db)
    manager = RuleDefinitionManager(temp_db)

    assert manager.get_by_name("build-agent-block-full-pytest") is None
    # The worker-safety rule keeps blocking a full pytest run for every agent.
    assert manager.get_by_name("no-full-pytest-suite") is not None
