"""Red tests for bundled rule loader tombstones."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_deprecated_rules_not_synced(temp_db, tmp_path) -> None:
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
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
    manager = LocalWorkflowDefinitionManager(temp_db)

    assert manager.get_by_name("active-build-rule") is not None
    assert manager.get_by_name("old-build-rule") is None
