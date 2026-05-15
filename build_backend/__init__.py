"""PEP 517 build backend wrapper.

Wraps setuptools.build_meta to stage the web UI into the wheel.

Before every wheel/sdist build:
  1. Honor ``GOBBY_SKIP_UI_BUILD=1`` to skip npm, while still requiring staged
     UI assets for wheel builds.
  2. If ``web/`` has ``package.json`` and ``npm`` is on PATH, run
     ``npm ci && npm run build`` in ``web/`` to produce ``web/dist/``.
  3. Copy ``web/dist/`` into ``src/gobby/ui/web/dist/`` so the
     ``ui/web/dist/**/*`` package-data glob picks the assets up.
  4. Verify built wheels contain ``gobby/ui/web/dist/index.html`` so release
     artifacts cannot silently ship without the production UI.

Editable installs skip the UI build entirely; the dev workflow uses
``gobby ui dev`` against ``web/`` directly.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # nosec B404
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _orig() -> Any:
    """Lazy-import setuptools.build_meta.

    Kept lazy so importing this module (e.g., from tests that only exercise
    `_stage_ui`) does not require setuptools to be installed at runtime.
    """
    from setuptools import build_meta

    return build_meta


def __getattr__(name: str) -> Any:
    """Forward PEP 517 hook attributes to setuptools.build_meta on first access."""
    if name in {
        "get_requires_for_build_wheel",
        "get_requires_for_build_sdist",
        "get_requires_for_build_editable",
        "prepare_metadata_for_build_wheel",
        "prepare_metadata_for_build_editable",
    }:
        return getattr(_orig(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_WEB_SRC: Path = _REPO_ROOT / "web"
_DIST_SRC: Path = _WEB_SRC / "dist"
_WHEEL_DEST: Path = _REPO_ROOT / "src" / "gobby" / "ui" / "web" / "dist"
_WHEEL_UI_INDEX: str = "gobby/ui/web/dist/index.html"


def _parse_npm_build_timeout(raw_value: str | None) -> int:
    if raw_value is None:
        return 600
    try:
        parsed = int(raw_value)
    except ValueError:
        logger.warning(
            "Invalid GOBBY_NPM_BUILD_TIMEOUT=%r; using default 600 seconds",
            raw_value,
        )
        return 600
    if parsed <= 0:
        logger.warning(
            "Non-positive GOBBY_NPM_BUILD_TIMEOUT=%r; using default 600 seconds",
            raw_value,
        )
        return 600
    return parsed


def _init_npm_build_timeout_seconds() -> int:
    return _parse_npm_build_timeout(os.environ.get("GOBBY_NPM_BUILD_TIMEOUT"))


_NPM_BUILD_TIMEOUT_SECONDS: int = _init_npm_build_timeout_seconds()


def _run_npm_command(command: list[str]) -> None:
    command_text = " ".join(command)
    try:
        logger.debug(
            "Running %s in %s with timeout=%s",
            command_text,
            _WEB_SRC,
            _NPM_BUILD_TIMEOUT_SECONDS,
        )
        result = subprocess.run(  # nosec B603 B607
            command,
            cwd=_WEB_SRC,
            check=False,
            capture_output=True,
            text=True,
            timeout=_NPM_BUILD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.error(
                "Failed %s in %s with return code %s\nstdout:\n%s\nstderr:\n%s",
                command_text,
                _WEB_SRC,
                result.returncode,
                result.stdout or "",
                result.stderr or "",
            )
            raise RuntimeError(
                f"Failed running {command_text!r} in {_WEB_SRC} "
                f"(return code {result.returncode})\n"
                f"stdout:\n{result.stdout or ''}\n"
                f"stderr:\n{result.stderr or ''}"
            )
        logger.info(
            "Completed %s in %s with return code %s",
            command_text,
            _WEB_SRC,
            result.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Timed out running {command_text!r} in {_WEB_SRC} "
            f"after {_NPM_BUILD_TIMEOUT_SECONDS} seconds"
        ) from exc


def _stage_ui() -> None:
    if os.environ.get("GOBBY_SKIP_UI_BUILD") == "1":
        logger.info("GOBBY_SKIP_UI_BUILD=1 - skipping UI build step")
        return

    have_source = (_WEB_SRC / "package.json").exists()
    have_npm = shutil.which("npm") is not None

    if have_source and have_npm:
        logger.info("Building web UI in %s", _WEB_SRC)
        _run_npm_command(["npm", "ci"])
        _run_npm_command(["npm", "run", "build"])

    if not _DIST_SRC.exists():
        if _WHEEL_DEST.exists():
            logger.info("web/dist not available; reusing pre-staged %s", _WHEEL_DEST)
            return
        logger.warning(
            "web/dist not found and npm build not possible - wheel UI asset verification will fail."
        )
        return

    if _WHEEL_DEST.exists():
        shutil.rmtree(_WHEEL_DEST)
    _WHEEL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_DIST_SRC, _WHEEL_DEST)
    logger.info("Staged web UI assets at %s", _WHEEL_DEST)


def _verify_wheel_contains_ui(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        if _WHEEL_UI_INDEX not in wheel.namelist():
            raise RuntimeError(
                f"Built wheel is missing {_WHEEL_UI_INDEX}; build web/dist before publishing."
            )


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    _stage_ui()
    wheel_name = str(_orig().build_wheel(wheel_directory, config_settings, metadata_directory))
    wheel_path = Path(wheel_name)
    if not wheel_path.is_absolute():
        wheel_path = Path(wheel_directory) / wheel_path
    _verify_wheel_contains_ui(wheel_path)
    return wheel_name


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    _stage_ui()
    return str(_orig().build_sdist(sdist_directory, config_settings))


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return str(_orig().build_editable(wheel_directory, config_settings, metadata_directory))
