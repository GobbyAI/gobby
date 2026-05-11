"""PEP 517 build backend wrapper.

Wraps setuptools.build_meta to stage the web UI into the wheel.

Before every wheel/sdist build:
  1. Honor ``GOBBY_SKIP_UI_BUILD=1`` as an escape hatch (CI/contributors without npm).
  2. If ``web/`` has ``package.json`` and ``npm`` is on PATH, run
     ``npm ci && npm run build`` in ``web/`` to produce ``web/dist/``.
  3. Copy ``web/dist/`` into ``src/gobby/ui/web/dist/`` so the
     ``ui/web/dist/**/*`` package-data glob picks the assets up.
  4. If neither a fresh build nor a pre-staged ``src/gobby/ui/web/dist/``
     is available, emit a warning - the wheel will install but the UI
     will 404 (same as before this wrapper existed).

Editable installs skip the UI build entirely; the dev workflow uses
``gobby ui dev`` against ``web/`` directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from setuptools import build_meta as _orig

logger = logging.getLogger(__name__)

get_requires_for_build_wheel = _orig.get_requires_for_build_wheel
get_requires_for_build_sdist = _orig.get_requires_for_build_sdist
get_requires_for_build_editable = _orig.get_requires_for_build_editable
prepare_metadata_for_build_wheel = _orig.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = _orig.prepare_metadata_for_build_editable


_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEB_SRC = _REPO_ROOT / "web"
_DIST_SRC = _WEB_SRC / "dist"
_WHEEL_DEST = _REPO_ROOT / "src" / "gobby" / "ui" / "web" / "dist"


def _stage_ui() -> None:
    if os.environ.get("GOBBY_SKIP_UI_BUILD") == "1":
        logger.info("GOBBY_SKIP_UI_BUILD=1 - skipping UI build step")
        return

    have_source = (_WEB_SRC / "package.json").exists()
    have_npm = shutil.which("npm") is not None

    if have_source and have_npm:
        logger.info("Building web UI in %s", _WEB_SRC)
        subprocess.run(["npm", "ci"], cwd=_WEB_SRC, check=True)  # nosec B603 B607
        subprocess.run(["npm", "run", "build"], cwd=_WEB_SRC, check=True)  # nosec B603 B607

    if not _DIST_SRC.exists():
        if _WHEEL_DEST.exists():
            logger.info("web/dist not available; reusing pre-staged %s", _WHEEL_DEST)
            return
        logger.warning(
            "web/dist not found and npm build not possible - wheel will not contain UI assets. "
            "Set GOBBY_SKIP_UI_BUILD=1 to silence this warning."
        )
        return

    if _WHEEL_DEST.exists():
        shutil.rmtree(_WHEEL_DEST)
    _WHEEL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_DIST_SRC, _WHEEL_DEST)
    logger.info("Staged web UI assets at %s", _WHEEL_DEST)


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    _stage_ui()
    return str(_orig.build_wheel(wheel_directory, config_settings, metadata_directory))


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    _stage_ui()
    return str(_orig.build_sdist(sdist_directory, config_settings))


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return str(_orig.build_editable(wheel_directory, config_settings, metadata_directory))
