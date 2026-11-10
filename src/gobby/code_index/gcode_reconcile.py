"""``gcode vector reconcile`` as a gateway call, mixed into ``GcodeGateway``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.code_index.gcode_gateway import GcodeCommandResult


class GcodeReconcileMixin:
    """Provides ``vector_reconcile``; the host class supplies the command runner."""

    if TYPE_CHECKING:

        async def _ensure_version(self) -> str: ...

        async def _run_command_result(
            self,
            command: Sequence[str],
            *,
            timeout: float | None = None,
            env: Mapping[str, str] | None = None,
        ) -> GcodeCommandResult: ...

    async def vector_reconcile(
        self,
        project_root: Path,
        *,
        apply: bool = False,
        expect_digest: str | None = None,
        receipt_path: Path | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> GcodeCommandResult:
        """Run ``gcode vector reconcile``: a dry run unless ``apply`` is set.

        Apply deletes only the inventory the dry run observed, so it needs that run's
        digest and a receipt path for gcode to write.
        """
        apply_args: list[str] = []
        if apply:
            if expect_digest is None or receipt_path is None:
                raise ValueError("apply requires expect_digest and receipt_path")
            apply_args = [
                "--apply",
                "--expect-digest",
                expect_digest,
                "--receipt",
                str(receipt_path),
            ]
        binary = await self._ensure_version()
        return await self._run_command_result(
            [
                binary,
                "vector",
                "reconcile",
                "--project",
                str(project_root),
                "--format",
                "json",
                *apply_args,
            ],
            timeout=timeout,
            env=env,
        )
