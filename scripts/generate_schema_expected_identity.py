#!/usr/bin/env python3
"""Generate Python's packaged identity projection from gdaemon."""

from __future__ import annotations

import argparse
from pathlib import Path

from gobby.storage.schema_identity_pin import SchemaIdentityError, pin_bytes, probe_identity
from gobby.utils.native_bin import resolve_native_bin

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT = _ROOT / "src/gobby/storage/schema_expected_identity.json"


def generate(binary: Path, output: Path) -> None:
    """Query one gdaemon binary and write its validated identity deterministically."""
    output.write_bytes(pin_bytes(probe_identity(binary)))


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
    except SchemaIdentityError as exc:
        print(f"generate_schema_expected_identity: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
