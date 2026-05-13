from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_cross_repo_submit_profile_plan_documents_implemented_fields() -> None:
    body = Path("docs/plans/cross-repo-submit-profile.md").read_text(encoding="utf-8")

    for text in (
        "delivery_mode",
        "delivery_target_repo",
        "task_delivery_campaigns.source_repo",
        "task_delivery_campaigns.target_repo",
        "task_delivery_units.pr_url",
        "task_delivery_units.github_pr_number",
        "open_delivery_pr",
        "head_repo",
    ):
        assert text in body
