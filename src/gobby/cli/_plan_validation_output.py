"""Shared CLI output helpers for plan validation results."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, NoReturn

import click


def _message_items(value: object) -> Iterable[str]:
    if not value or isinstance(value, str | bytes):
        return ()
    if isinstance(value, Iterable):
        return (str(item) for item in value)
    return ()


def emit_plan_validation_messages(result: Mapping[str, Any]) -> None:
    """Emit validation errors and warnings to stderr."""
    for error in _message_items(result.get("errors")):
        click.echo(f"Error: {error}", err=True)
    for warning in _message_items(result.get("warnings")):
        click.echo(f"Warning: {warning}", err=True)


def raise_plan_validation_failed(result: Mapping[str, Any]) -> NoReturn:
    """Emit validation diagnostics, then raise ClickException."""
    emit_plan_validation_messages(result)
    raise click.ClickException("Plan validation failed")
