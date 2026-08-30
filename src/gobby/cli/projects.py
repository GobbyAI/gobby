"""
Project management CLI commands.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import click

from gobby.cli.runtime import require_cli_database
from gobby.storage.project_checkouts import (
    CheckoutConflictError,
    CheckoutNotFoundError,
    CheckoutRootTakenError,
    CheckoutSentinelRejectedError,
    LocalProjectCheckoutManager,
    MissingMachineContextError,
    OverlayRegistrationRejectedError,
    ProjectCheckout,
    require_root,
)
from gobby.storage.projects import (
    CHECKOUT_FREE_PROJECT_IDS,
    SYSTEM_PROJECT_NAMES,
    AmbiguousProjectRefError,
    IsolatedAgentProjectPathError,
    LocalProjectManager,
    Project,
)
from gobby.storage.workspace_machine_scope import (
    MachineOwnershipMismatchError,
    require_local_machine_id,
)
from gobby.utils.checkout_root import (
    InvalidCheckoutRootError,
    MarkerMismatchError,
    validate_checkout_root,
)
from gobby.utils.json_helpers import json_dumps
from gobby.utils.project_context import find_project_root, get_project_context
from gobby.utils.project_init import refresh_marker_expected_id
from gobby.utils.uuid_validation import parse_uuid_reference

_CHECKOUT_CLI_ERRORS = (
    AmbiguousProjectRefError,
    OverlayRegistrationRejectedError,
    CheckoutSentinelRejectedError,
    MarkerMismatchError,
    InvalidCheckoutRootError,
    IsolatedAgentProjectPathError,
    CheckoutRootTakenError,
    CheckoutConflictError,
    CheckoutNotFoundError,
    MissingMachineContextError,
    MachineOwnershipMismatchError,
)


def get_project_manager() -> LocalProjectManager:
    """Get initialized project manager."""
    return LocalProjectManager(require_cli_database())


def resolve_project(manager: LocalProjectManager, ref: str) -> Project:
    """Resolve a project reference or exit with error."""
    project = manager.resolve_ref(ref)
    if not project:
        click.echo(f"Project not found: {ref}", err=True)
        raise SystemExit(1)
    return project


def _ordinary_root_candidate(path: str | None) -> str:
    raw = os.getcwd() if path is None else path
    if raw.startswith("~") or not os.path.isabs(raw):
        return raw
    return os.path.normpath(raw)


def _marker_id_at(path: str) -> str | None:
    if not os.path.isabs(path) or not os.path.isdir(path):
        return None
    context = get_project_context(Path(path))
    marker_id = None if context is None else context.get("id")
    return None if marker_id is None else str(marker_id)


def _local_machine_id(project_id: str) -> str:
    return require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)


@contextmanager
def _checkout_cli() -> Iterator[None]:
    try:
        yield
    except _CHECKOUT_CLI_ERRORS as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc


def _checkout_payload(checkout: ProjectCheckout | None) -> dict[str, str] | None:
    if checkout is None:
        return None
    return {"machine_id": checkout.machine_id, "root_path": checkout.root_path}


def _checkouts_by_project(manager: LocalProjectManager) -> Mapping[str, ProjectCheckout]:
    machine_id = _local_machine_id("projects.cli")
    rows = LocalProjectCheckoutManager(manager.db).list_for_machine(machine_id)
    return {row.project_id: row for row in rows}


def _optional_checkout(manager: LocalProjectManager, project_id: str) -> ProjectCheckout | None:
    try:
        return LocalProjectCheckoutManager(manager.db).get(
            _local_machine_id(project_id), project_id
        )
    except (TypeError, ValueError, KeyError):
        return None


def resolve_rebind_project(
    manager: LocalProjectManager,
    ref: str,
    *,
    marker_project_id: str | None,
) -> Project:
    """Resolve a UUID exactly, or a unique name including soft-deleted rows."""
    if parse_uuid_reference(ref) is not None:
        project = manager.get(ref)
        if project is None:
            click.echo(f"Project not found: {ref}", err=True)
            raise SystemExit(1)
        return project

    rows = manager.db.fetchall(
        "SELECT * FROM projects WHERE name = %s ORDER BY created_at, id",
        (ref,),
    )
    matches = [Project.from_row(row) for row in rows]
    active = [project for project in matches if project.deleted_at is None]
    if len(active) == 1:
        return active[0]
    deleted = [project for project in matches if project.deleted_at is not None]
    if not active and len(deleted) == 1:
        return deleted[0]
    if not active and len(deleted) >= 2:
        if marker_project_id is not None:
            selected = [project for project in deleted if project.id == marker_project_id]
            if len(selected) == 1:
                return selected[0]
        raise AmbiguousProjectRefError(
            f"ambiguous project name {ref!r}; use a UUID or a PATH whose marker selects one"
        )
    click.echo(f"Project not found: {ref}", err=True)
    raise SystemExit(1)


def resolve_refresh_root(project_ref: str | None) -> Path:
    """Resolve refresh target root from current directory or local checkout."""
    if project_ref is None:
        cwd = Path.cwd()
        root = find_project_root(cwd) or cwd.resolve()
    else:
        manager = get_project_manager()
        project = resolve_project(manager, project_ref)
        with _checkout_cli():
            machine_id = _local_machine_id(project.id)
            root = Path(require_root(manager.db, project.id, machine_id))

    project_json = root / ".gobby" / "project.json"
    if not project_json.exists():
        click.echo(f"No .gobby/project.json found in {root}.", err=True)
        click.echo(f"Run 'gobby init -C {root}' to initialize this project.", err=True)
        raise SystemExit(1)
    return root


@click.group()
def projects() -> None:
    """Manage Gobby projects."""
    pass


@projects.command("list")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.option("--all", "show_all", is_flag=True, help="Include system projects (prefixed with _)")
def list_projects(json_format: bool, show_all: bool) -> None:
    """List all known projects."""
    manager = get_project_manager()
    projects_list = manager.list()

    if not show_all:
        projects_list = [p for p in projects_list if not p.name.startswith("_")]

    checkouts = _checkouts_by_project(manager)

    if json_format:
        payload = []
        for project in projects_list:
            item = project.to_dict()
            item["checkout"] = _checkout_payload(checkouts.get(project.id))
            payload.append(item)
        click.echo(json_dumps(payload, indent=2, default=str))
        return

    if not projects_list:
        click.echo("No projects found.")
        click.echo("Use 'gobby init' in a project directory to register it.")
        return

    click.echo(f"Found {len(projects_list)} project(s):\n")
    for project in projects_list:
        checkout = checkouts.get(project.id)
        path_info = f"  {checkout.root_path}" if checkout is not None else ""
        click.echo(f"  {project.name:<20} {project.id[:12]}{path_info}")


@projects.command("show")
@click.argument("project_ref")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_project(project_ref: str, json_format: bool) -> None:
    """Show details for a project.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)
    checkout = _optional_checkout(manager, project.id)

    if json_format:
        payload = project.to_dict()
        payload["checkout"] = _checkout_payload(checkout)
        click.echo(json_dumps(payload, indent=2, default=str))
        return

    click.echo(f"Project: {project.name}")
    click.echo(f"  ID: {project.id}")
    if checkout is not None:
        click.echo(f"  Checkout: {checkout.root_path}")
    if project.github_url:
        click.echo(f"  GitHub: {project.github_url}")
    if project.github_repo:
        click.echo(f"  Repo: {project.github_repo}")
    if project.linear_team_id:
        click.echo(f"  Linear Team: {project.linear_team_id}")
    if project.linear_project_id:
        click.echo(f"  Linear Project: {project.linear_project_id}")
    click.echo(f"  Created: {project.created_at}")
    click.echo(f"  Updated: {project.updated_at}")


@projects.command("rename")
@click.argument("project_ref")
@click.argument("new_name")
def rename_project(project_ref: str, new_name: str) -> None:
    """Rename a project.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    if manager.is_protected(project):
        click.echo(f"Cannot rename protected project: {project.name}", err=True)
        raise SystemExit(1)

    if new_name in SYSTEM_PROJECT_NAMES:
        click.echo(f"Cannot use reserved name: {new_name}", err=True)
        raise SystemExit(1)

    existing = manager.get_by_name(new_name)
    if existing:
        click.echo(f"A project named '{new_name}' already exists.", err=True)
        raise SystemExit(1)

    old_name = project.name
    manager.update(project.id, name=new_name)

    checkout = _optional_checkout(manager, project.id)
    if checkout is not None:
        try:
            refresh_marker_expected_id(Path(checkout.root_path), project.id, new_name)
        except (MarkerMismatchError, OSError, RuntimeError, *_CHECKOUT_CLI_ERRORS) as exc:
            click.echo(f"Warning: Could not refresh checkout marker: {exc}", err=True)

    click.echo(f"Renamed '{old_name}' -> '{new_name}'")
    click.echo(f"Note: Existing commits with [{old_name}-#N] won't auto-link to the new name.")


@projects.command("delete")
@click.argument("project_ref")
@click.option("--confirm", required=True, help="Type the project name to confirm deletion")
def delete_project(project_ref: str, confirm: str) -> None:
    """Soft-delete a project.

    The project is marked as deleted but data is preserved. Use --confirm=<name> to confirm.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    if manager.is_protected(project):
        click.echo(f"Cannot delete protected project: {project.name}", err=True)
        raise SystemExit(1)

    if confirm != project.name:
        click.echo(f"Confirmation mismatch: expected '{project.name}', got '{confirm}'", err=True)
        raise SystemExit(1)

    if manager.soft_delete(project.id):
        click.echo(f"Deleted project: {project.name}")
    else:
        click.echo(f"Failed to delete project: {project.name}", err=True)
        raise SystemExit(1)


@projects.command("purge")
@click.argument("project_ref")
@click.option(
    "--confirm",
    required=True,
    help="Confirm permanent deletion by entering the project name.",
)
def purge_project(project_ref: str, confirm: str) -> None:
    """Permanently purge a project through the running daemon."""
    manager = get_project_manager()
    project = manager.get(project_ref) or manager.get_by_name(project_ref, include_deleted=True)
    if project is None:
        click.echo(f"Project not found: {project_ref}", err=True)
        raise SystemExit(1)
    if confirm != project.name:
        click.echo(
            f"Confirmation mismatch. Enter the exact project name: {project.name}",
            err=True,
        )
        raise SystemExit(1)

    from gobby.cli.utils_config import get_daemon_client

    response = get_daemon_client(timeout=300.0).call_http_api(
        f"/api/projects/{project.id}/purge",
        method="POST",
        timeout=300.0,
    )
    if response.status_code >= 400:
        try:
            error_payload = response.json()
        except ValueError:
            detail = response.text
        else:
            detail = (
                error_payload.get("detail", response.text)
                if isinstance(error_payload, dict)
                else response.text
            )
        click.echo(f"Purge failed: {detail}", err=True)
        raise SystemExit(1)
    payload = response.json()
    click.echo(json_dumps(payload, indent=2, sort_keys=True))


@projects.command("rebind")
@click.argument("project_ref")
@click.argument("path", required=False)
def rebind_project(project_ref: str, path: str | None) -> None:
    """Rebind this machine's checkout for PROJECT_REF to PATH (default: cwd).

    Verifies the local machine, validates the marker at PATH, then rebinds.
    Soft-deleted projects stay deleted. Ambiguous deleted names need a UUID
    or a PATH whose marker selects one row.
    """
    manager = get_project_manager()
    candidate = _ordinary_root_candidate(path)
    with _checkout_cli():
        project = resolve_rebind_project(
            manager, project_ref, marker_project_id=_marker_id_at(candidate)
        )
        if project.id in CHECKOUT_FREE_PROJECT_IDS:
            raise CheckoutSentinelRejectedError(
                f"checkout-free sentinel project {project.id} cannot own a checkout"
            )
        machine_id = _local_machine_id(project.id)
        root = validate_checkout_root(
            manager.db,
            project_id=project.id,
            machine_id=machine_id,
            candidate_path=candidate,
            expected_marker_id=project.id,
        )
        checkout = LocalProjectCheckoutManager(manager.db).rebind(machine_id, project.id, root)
    click.echo(f"Rebound {project.name} ({project.id}) to {checkout.root_path}")


@projects.command("update")
@click.argument("project_ref")
@click.option("--github-url", help="GitHub repository URL")
@click.option("--github-repo", help="GitHub repo in owner/repo format")
@click.option("--linear-team-id", help="Linear team ID")
@click.option("--linear-project-id", help="Linear project ID")
def update_project(
    project_ref: str,
    github_url: str | None,
    github_repo: str | None,
    linear_team_id: str | None,
    linear_project_id: str | None,
) -> None:
    """Update project fields.

    PROJECT_REF can be a project name or UUID.
    """
    manager = get_project_manager()
    project = resolve_project(manager, project_ref)

    fields: dict[str, str] = {}
    if github_url is not None:
        fields["github_url"] = github_url
    if github_repo is not None:
        fields["github_repo"] = github_repo
    if linear_team_id is not None:
        fields["linear_team_id"] = linear_team_id
    if linear_project_id is not None:
        fields["linear_project_id"] = linear_project_id

    if not fields:
        click.echo(
            "No fields to update. Use --github-url, --github-repo, "
            "--linear-team-id, or --linear-project-id."
        )
        return

    updated = manager.update(project.id, **fields)
    if updated:
        click.echo(f"Updated project: {updated.name}")
        for key, value in fields.items():
            click.echo(f"  {key}: {value}")
    else:
        click.echo(f"Failed to update project: {project.name}", err=True)
        raise SystemExit(1)


@projects.command("refresh-verification")
@click.argument("project_ref", required=False)
@click.option("--fix", is_flag=True, help="Write refreshed verification commands")
@click.option(
    "--ai",
    "ai_mode",
    type=click.Choice(["auto", "on", "off"]),
    default="auto",
    show_default=True,
    help="AI synthesis mode",
)
@click.option(
    "--profile",
    type=click.Choice(["feature_low", "feature_mid", "feature_high"]),
    default=None,
    help="Override synthesis feature profile",
)
@click.option(
    "--candidate",
    "candidates",
    multiple=True,
    help="Provider/model candidate for AI synthesis; repeatable",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def refresh_verification(
    project_ref: str | None,
    fix: bool,
    ai_mode: Literal["auto", "on", "off"],
    profile: str | None,
    candidates: tuple[str, ...],
    json_format: bool,
) -> None:
    """Refresh .gobby/project.json verification commands."""
    from gobby.project_verification.refresh import (
        ProjectVerificationAIError,
        ProjectVerificationReadError,
        refresh_project_verification,
        refresh_project_verification_deterministic,
    )

    root = resolve_refresh_root(project_ref)
    try:
        if ai_mode == "off":
            result = refresh_project_verification_deterministic(root, fix=fix)
        else:
            from gobby.ai import build_daemon_text_generation_service
            from gobby.cli.runtime import get_cli_runtime
            from gobby.config.feature_base import FeatureProfile
            from gobby.config.features import ProjectVerificationSynthesisConfig

            config = get_cli_runtime().require_config()
            synthesis_data = config.project_verification_synthesis.model_dump()
            if profile:
                synthesis_data["profile"] = FeatureProfile(profile)
                if not candidates:
                    synthesis_data["candidates"] = []
            if candidates:
                synthesis_data["candidates"] = list(candidates)
            synthesis_config = ProjectVerificationSynthesisConfig(**synthesis_data)
            service = build_daemon_text_generation_service(config)
            result = asyncio.run(
                refresh_project_verification(
                    root,
                    fix=fix,
                    ai_mode=ai_mode,
                    synthesis_config=synthesis_config,
                    text_generation_service=service,
                )
            )
    except (ProjectVerificationAIError, ProjectVerificationReadError) as exc:
        if json_format:
            click.echo(json_dumps({"error": str(exc)}, indent=2))
        else:
            click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    if json_format:
        click.echo(json_dumps(result.to_dict(), indent=2))
        return

    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)

    if result.changed:
        if result.written:
            click.echo(f"Updated verification commands in {result.project_json_path}.")
        else:
            click.echo(f"Previewing verification refresh for {result.project_json_path}.")
        if result.diff:
            click.echo(result.diff)
        if not fix:
            click.echo("Run with --fix to write changes.")
    else:
        click.echo("Verification commands already up to date.")

    if result.ai_error and ai_mode == "auto":
        click.echo(
            f"AI synthesis unavailable; used deterministic refresh: {result.ai_error}", err=True
        )
    if result.ai_rejected:
        click.echo(f"Rejected {len(result.ai_rejected)} AI command(s).", err=True)


@projects.command("repair")
@click.option("--fix", is_flag=True, help="Apply fixes (default is dry-run)")
def repair_project(fix: bool) -> None:
    """Repair checkout/marker drift from the current directory.

    Without --fix, prints issues found. With --fix, registers a missing
    checkout only when the cwd marker is valid at this root.
    """
    candidate = _ordinary_root_candidate(None)
    manager = get_project_manager()
    with _checkout_cli():
        machine_id = _local_machine_id("projects.repair")
        if (
            not candidate
            or candidate.startswith("~")
            or not os.path.isabs(candidate)
            or os.path.normpath(candidate) != candidate
            or not os.path.isdir(candidate)
        ):
            validate_checkout_root(
                manager.db,
                project_id="projects.repair",
                machine_id=machine_id,
                candidate_path=candidate,
                expected_marker_id="projects.repair",
            )

        project_json_path = Path(candidate) / ".gobby" / "project.json"
        if not project_json_path.exists():
            click.echo("No .gobby/project.json found in current directory.", err=True)
            click.echo("Run 'gobby init' to initialize a project here.")
            raise SystemExit(1)

        try:
            with open(project_json_path, encoding="utf-8") as f:
                local_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            click.echo(f"Failed to read project.json: {e}", err=True)
            raise SystemExit(1) from e

        project_id = local_data.get("id")
        if not project_id:
            click.echo("project.json missing 'id' field.", err=True)
            raise SystemExit(1)
        project_id = str(project_id)

        db_project = manager.get(project_id)
        if db_project is None:
            click.echo(f"Project {project_id} not found in database.", err=True)
            click.echo("The project may have been deleted. Run 'gobby init' to re-register.")
            raise SystemExit(1)

        if project_id in CHECKOUT_FREE_PROJECT_IDS:
            raise CheckoutSentinelRejectedError(
                f"checkout-free sentinel project {project_id} cannot own a checkout"
            )

        machine_id = _local_machine_id(project_id)
        root = validate_checkout_root(
            manager.db,
            project_id=project_id,
            machine_id=machine_id,
            candidate_path=candidate,
            expected_marker_id=project_id,
        )
        existing = LocalProjectCheckoutManager(manager.db).get(machine_id, project_id)
        if existing is None:
            if not fix:
                click.echo(f"Missing local checkout for {db_project.name} at {root}.")
                click.echo("Run with --fix to register this checkout.")
                return
            LocalProjectCheckoutManager(manager.db).register(machine_id, project_id, root)
            click.echo(f"Created local checkout for {db_project.name} at {root}.")
            return
        if existing.root_path != root:
            click.echo(
                f"Local checkout is {existing.root_path}; cwd is {root}. "
                f"Use `gobby projects rebind {db_project.name} {root}`.",
                err=True,
            )
            raise SystemExit(1)
        click.echo("No drift.")
