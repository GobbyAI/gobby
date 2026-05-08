"""Expansion prompt context must read the stage manifest, not skip labels."""

from __future__ import annotations

import inspect

import pytest

from gobby.tasks.expansion import _compile

pytestmark = pytest.mark.unit


def test_prompt_context_reads_stages_not_labels() -> None:
    source = inspect.getsource(_compile._build_prompt_context)

    reads_manifest = "task.stages" in source or "getattr(task, 'stages'" in source
    reads_manifest = reads_manifest or 'getattr(task, "stages"' in source
    assert reads_manifest
    assert "_skipped_stages" not in source
    assert "stage-:" not in source
    assert ".labels" not in source
