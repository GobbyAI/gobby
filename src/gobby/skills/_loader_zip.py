"""ZIP archive helpers for skill loading."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from gobby.skills._loader_models import SkillLoadError


def _resolve_within_directory(base: Path, relative_path: str) -> Path:
    try:
        resolved_base = base.resolve()
        resolved = (resolved_base / relative_path).resolve()
        resolved.relative_to(resolved_base)
    except (OSError, RuntimeError, ValueError):
        raise SkillLoadError("Path would escape extracted ZIP", base) from None
    return resolved


@contextmanager
def extract_zip(zip_path: str | Path) -> Generator[Path]:
    """Extract a ZIP archive to a temporary directory."""
    zip_path = Path(zip_path)

    if not zip_path.exists():
        raise SkillLoadError("ZIP file not found", zip_path)

    if not zipfile.is_zipfile(zip_path):
        raise SkillLoadError("Invalid ZIP file", zip_path)

    temp_dir = tempfile.mkdtemp(prefix="gobby-skill-")
    temp_path = Path(temp_dir)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                target_path = (temp_path / member.filename).resolve()

                try:
                    target_path.relative_to(temp_path.resolve())
                except ValueError:
                    raise SkillLoadError(
                        f"Zip entry would extract outside target: {member.filename}",
                        zip_path,
                    ) from None

                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                else:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, open(target_path, "wb") as dest:
                        shutil.copyfileobj(source, dest)

        yield temp_path
    finally:
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
