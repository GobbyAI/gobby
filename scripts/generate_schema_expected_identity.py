#!/usr/bin/env python3
"""Generate Python's packaged identity projection from gdaemon."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import cast

from gobby.utils.native_bin import resolve_native_bin

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _ROOT / "src/gobby/storage/schema_expected_identity.json"
_INTEGER_FIELDS = ("runner_protocol", "baseline_version", "latest_version")
_STRING_FIELDS = ("baseline_checksum", "latest_checksum", "assets_root_hash")


class IdentityGenerationError(RuntimeError):
    """Raised when gdaemon cannot provide the exact schema identity contract."""


def _validate_identity(parsed: object) -> dict[str, int | str]:
    if not isinstance(parsed, dict):
        raise IdentityGenerationError("gdaemon schema identity must be a JSON object")
    values = cast(dict[str, object], parsed)
    expected_fields = {*_INTEGER_FIELDS, *_STRING_FIELDS}
    if set(values) != expected_fields:
        raise IdentityGenerationError(
            f"gdaemon schema identity must contain exactly {sorted(expected_fields)}"
        )

    identity: dict[str, int | str] = {}
    for field in _INTEGER_FIELDS:
        value = values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise IdentityGenerationError(
                f"gdaemon schema identity field {field} must be an integer"
            )
        identity[field] = value
    for field in _STRING_FIELDS:
        value = values[field]
        if not isinstance(value, str) or not value:
            raise IdentityGenerationError(f"gdaemon schema identity field {field} must be a string")
        identity[field] = value
    return identity


def generate(binary: Path, output: Path) -> None:
    """Query one gdaemon binary and write its validated identity deterministically."""
    try:
        result = subprocess.run(  # nosec B603 - operator-supplied executable, fixed arguments
            [str(binary), "schema", "version", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise IdentityGenerationError("gdaemon schema version timed out after 30 seconds") from exc
    except OSError as exc:
        raise IdentityGenerationError(f"failed to launch gdaemon: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise IdentityGenerationError(f"gdaemon schema version failed: {detail}")
    try:
        parsed: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IdentityGenerationError("gdaemon schema version returned invalid JSON") from exc
    identity = _validate_identity(parsed)
    output.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gdaemon",
        type=Path,
        default=Path(resolve_native_bin("gdaemon") or "gdaemon"),
        help="gdaemon binary to query",
    )
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        generate(args.gdaemon, args.output)
    except IdentityGenerationError as exc:
        print(f"generate_schema_expected_identity: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
