"""Release guards for warning-free daemon runtime dependencies."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATHS = (
    _REPO_ROOT / "src/gobby/data/docker-compose.services.yml",
    _REPO_ROOT / "crates/gcore/assets/docker-compose.services.yml",
)


def _runtime_requirement(name: str) -> Requirement:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    dependencies = project["dependencies"]
    return next(
        requirement
        for value in dependencies
        if (requirement := Requirement(value)).name == name
    )


def test_warning_sensitive_runtime_dependencies_are_exactly_pinned() -> None:
    """Fresh wheel installs must preserve the validated warning-free versions."""
    assert str(_runtime_requirement("pydantic-settings").specifier) == "==2.14.2"
    assert str(_runtime_requirement("qdrant-client").specifier) == "==1.17.1"


def test_qdrant_service_matches_client_pin() -> None:
    """Managed Qdrant server and Python client must use the same release."""
    expected_image = "qdrant/qdrant:v1.17.1"
    for compose_path in _COMPOSE_PATHS:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        assert compose["services"]["qdrant"]["image"] == expected_image
