"""Tests for shared plan validation CLI output."""

from __future__ import annotations

import click
import pytest

from gobby.cli._plan_validation_output import raise_plan_validation_failed


def test_raise_plan_validation_failed_emits_warnings_before_click_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(click.ClickException, match="Plan validation failed"):
        raise_plan_validation_failed(
            {
                "errors": ["missing acceptance item"],
        "warnings": ["semantic validation warning"],
            }
        )

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "Error: missing acceptance item",
        "Warning: semantic validation warning",
    ]
