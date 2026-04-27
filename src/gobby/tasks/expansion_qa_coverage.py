"""Expansion QA plan-coverage integration."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.expansion_runs import ExpansionRun
    from gobby.storage.tasks import Task


CoverageEvaluator = Callable[..., Any]
ManifestWriter = Callable[..., Path | str | None]


class ExpansionQaCoverageError(RuntimeError):
    """Raised when expansion QA coverage cannot run."""


def run_expansion_qa_coverage(
    *,
    task_manager: LocalTaskManager,
    run: ExpansionRun,
    repo_path: str | Path | None,
    plan_path: str,
    plan_id: str,
    plan_hash: str,
    root_task_ref: str,
    project_id: str,
    task_tree: str = "db",
    regenerate: bool = False,
    evaluator: CoverageEvaluator | None = None,
    manifest_writer: ManifestWriter | None = None,
) -> dict[str, Any]:
    """Run A4 coverage for an expansion run and persist QA artifacts."""
    if task_tree != "db":
        return {"ok": False, "error": "unsupported_task_tree", "task_tree": task_tree}

    root_task = _resolve_root_task(task_manager, root_task_ref, project_id, run)
    repo_root = Path(repo_path) if repo_path else Path.cwd()
    resolved_plan_path = _resolve_path(repo_root, plan_path)
    actual_plan_hash = _sha256_file(resolved_plan_path)

    artifact_manager = TaskArtifactManager(task_manager.db)
    artifacts = artifact_manager.get_artifacts(root_task.id)
    expected_hash = artifacts.plan_file_hash or plan_hash
    if actual_plan_hash != expected_hash or plan_hash != expected_hash:
        return _fail_plan_hash_drift(
            task_manager=task_manager,
            run_id=run.id,
            expected_plan_hash=expected_hash,
            input_plan_hash=plan_hash,
            actual_plan_hash=actual_plan_hash,
            root_task_ref=root_task_ref,
            plan_path=resolved_plan_path,
        )

    artifact_manager.set_artifacts_atomic(
        root_task.id,
        plan_file_path=plan_path,
        plan_file_hash=actual_plan_hash,
        expansion_run_id=run.id,
    )

    report = (evaluator or _evaluate_with_a4)(
        db=task_manager.db,
        plan_path=resolved_plan_path,
        plan_id=plan_id,
        plan_hash=actual_plan_hash,
        task_tree=task_tree,
        root_task_ref=root_task_ref,
        project_id=project_id,
    )
    report_dict = _report_to_dict(report)
    manifest_path = _resolve_manifest_path(repo_root, project_id, root_task_ref, plan_id)
    written_manifest = _write_manifest(
        report,
        manifest_path,
        repo_root=repo_root,
        regenerate=regenerate,
        manifest_writer=manifest_writer,
    )
    display_manifest = _display_path(written_manifest, repo_root)
    failures = _coverage_failures(report)
    review_action = _review_action(root_task_ref, display_manifest, failures)
    qa_result = {
        "passed": not failures,
        "manifest_path": display_manifest,
        "coverage": report_dict,
        "failures": failures,
        "review_action": review_action,
        "scope": {
            "plan_path": str(resolved_plan_path),
            "plan_id": plan_id,
            "plan_hash": actual_plan_hash,
            "root_task": root_task_ref,
            "project_id": project_id,
            "task_tree": task_tree,
        },
        "artifacts": {
            "task_id": root_task.id,
            "plan_file_path": plan_path,
            "plan_file_hash": actual_plan_hash,
            "base_commit_sha": artifacts.base_commit_sha,
            "expansion_run_id": run.id,
        },
    }
    saved = LocalExpansionRunManager(task_manager.db).save_qa_result(run.id, qa_result)
    return {
        "ok": True,
        "run_id": run.id,
        "passed": not failures,
        "manifest_path": display_manifest,
        "review_action": review_action,
        "qa_result": qa_result,
        "run_status": saved.status if saved else run.status,
    }


def _evaluate_with_a4(
    *,
    db: DatabaseProtocol,
    plan_path: Path,
    plan_id: str,
    plan_hash: str,
    task_tree: str,
    root_task_ref: str,
    project_id: str,
) -> Any:
    try:
        from gobby.plans import coverage as coverage_module
    except ImportError as exc:
        raise ExpansionQaCoverageError("gobby.plans.coverage is unavailable") from exc

    evaluate = getattr(coverage_module, "evaluate", None)
    if evaluate is None:
        raise ExpansionQaCoverageError("gobby.plans.coverage.evaluate is unavailable")

    source_type = getattr(coverage_module, "TaskTreeSource", None)
    task_tree_value = getattr(source_type, task_tree, task_tree) if source_type else task_tree
    # Aliases plan/plan_path and root_task/root_task_ref so _call_with_supported_kwargs
    # can route to either parameter spelling depending on the evaluate signature.
    kwargs = {
        "db": db,
        "plan_path": plan_path,
        "plan": plan_path,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "task_tree": task_tree_value,
        "root_task_ref": root_task_ref,
        "root_task": root_task_ref,
        "project_id": project_id,
    }
    return _call_with_supported_kwargs(evaluate, kwargs)


def _call_with_supported_kwargs(func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    signature = inspect.signature(func)
    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return func(**kwargs)
    supported = {name: value for name, value in kwargs.items() if name in parameters}
    return func(**supported)


def _resolve_root_task(
    task_manager: LocalTaskManager,
    root_task_ref: str,
    project_id: str,
    run: ExpansionRun,
) -> Task:
    try:
        return task_manager.get_task(root_task_ref, project_id)
    except ValueError:
        return task_manager.get_task(run.parent_task_id)


def _resolve_path(repo_root: Path, path_value: str) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else repo_root / path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail_plan_hash_drift(
    *,
    task_manager: LocalTaskManager,
    run_id: str,
    expected_plan_hash: str,
    input_plan_hash: str,
    actual_plan_hash: str,
    root_task_ref: str,
    plan_path: Path,
) -> dict[str, Any]:
    reason = "plan_hash_drift"
    detail = (
        f"{reason}: expected {expected_plan_hash}, input {input_plan_hash}, "
        f"actual {actual_plan_hash}"
    )
    qa_result = {
        "passed": False,
        "reason": reason,
        "detail": detail,
        "expected_plan_hash": expected_plan_hash,
        "input_plan_hash": input_plan_hash,
        "actual_plan_hash": actual_plan_hash,
        "review_action": {
            "server": "gobby-tasks",
            "tool": "mark_task_review_rejected",
            "arguments": {
                "task_id": root_task_ref,
                "rejection_notes": f"plan_hash_drift for {plan_path}: {detail}",
            },
        },
    }
    run_manager = LocalExpansionRunManager(task_manager.db)
    run_manager.save_qa_result(run_id, qa_result)
    failed = run_manager.fail(run_id, detail)
    return {
        "ok": False,
        "error": reason,
        "reason": reason,
        "detail": detail,
        "expected_plan_hash": expected_plan_hash,
        "input_plan_hash": input_plan_hash,
        "actual_plan_hash": actual_plan_hash,
        "qa_result": qa_result,
        "run_status": failed.status if failed else "failed",
    }


def _resolve_manifest_path(
    repo_root: Path,
    project_id: str,
    root_task_ref: str,
    plan_id: str,
) -> Path:
    try:
        from gobby.plans.coverage_manifest import coverage_manifest_path
    except ImportError:
        relative = (
            Path(".gobby/plans/coverage")
            / _sanitize(project_id, kind="project_id")
            / _sanitize(root_task_ref, kind="root_task_ref")
            / f"{_sanitize(plan_id, kind='plan_id')}.coverage.yaml"
        )
        return repo_root / relative

    path = _call_with_supported_kwargs(
        coverage_manifest_path,
        {
            "project_root": repo_root,
            "project_id": project_id,
            "root_task_ref": root_task_ref,
            "root_task": root_task_ref,
            "plan_id": plan_id,
        },
    )
    path_obj = Path(path)
    return path_obj if path_obj.is_absolute() else repo_root / path_obj


def _sanitize(value: str, *, kind: str) -> str:
    cleaned = value.strip()
    if kind == "root_task_ref" and cleaned.startswith("#"):
        cleaned = cleaned[1:]
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in cleaned)
    cleaned = cleaned.strip("-._")
    if not cleaned:
        raise ExpansionQaCoverageError(f"{kind} sanitizes to an empty path component")
    return cleaned


def _write_manifest(
    report: Any,
    manifest_path: Path,
    *,
    repo_root: Path,
    regenerate: bool,
    manifest_writer: ManifestWriter | None,
) -> Path:
    writer = manifest_writer or _load_a4_manifest_writer()
    if writer is not None:
        result = _call_with_supported_kwargs(
            writer,
            {
                "report": report,
                "project_root": repo_root,
                "manifest_path": manifest_path,
                "path": manifest_path,
                "regenerate": regenerate,
            },
        )
        return Path(result) if result is not None else manifest_path

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        yaml.safe_dump(_report_to_dict(report), sort_keys=False),
        encoding="utf-8",
    )
    return manifest_path


def _load_a4_manifest_writer() -> ManifestWriter | None:
    try:
        from gobby.plans.coverage_manifest import write_manifest
    except ImportError:
        return None
    return write_manifest


def _report_to_dict(report: Any) -> dict[str, Any]:
    if isinstance(report, Mapping):
        return dict(report)
    if is_dataclass(report) and not isinstance(report, type):
        value = _to_plain(report)
        return dict(value) if isinstance(value, Mapping) else {"value": value}
    if hasattr(report, "to_dict"):
        value = report.to_dict()
        return dict(value) if isinstance(value, Mapping) else {"value": value}
    data: dict[str, Any] = {}
    for attr in ("header", "rows"):
        if hasattr(report, attr):
            data[attr] = _to_plain(getattr(report, attr))
    return data or {"value": str(report)}


def _to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple | list):
        return [_to_plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _coverage_failures(report: Any) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for row in _rows(report):
        status = str(_field(row, "status", "")).lower()
        if status not in {"missing", "invalid"}:
            continue
        leaves = [_leaf_ref(leaf) for leaf in _field(row, "leaves", []) or []]
        leaves = [leaf for leaf in leaves if leaf]
        failures.append(
            {
                "section_id": _field(row, "section_id", ""),
                "item_id": _field(row, "item_id", ""),
                "status": status,
                "detail": _field(row, "detail", f"coverage status {status}"),
                "leaves": leaves,
            }
        )
    return failures


def _rows(report: Any) -> list[Any]:
    rows = _field(report, "rows", [])
    return list(rows or [])


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        result = value.get(name, default)
    else:
        result = getattr(value, name, default)
    if hasattr(result, "value"):
        return result.value
    return result


def _leaf_ref(leaf: Any) -> str:
    value = _field(leaf, "leaf_task_ref", "")
    return str(value) if value else ""


def _review_action(
    root_task_ref: str,
    manifest_path: str,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    if not failures:
        return {
            "server": "gobby-tasks",
            "tool": "mark_task_review_approved",
            "arguments": {
                "task_id": root_task_ref,
                "approval_notes": f"Plan coverage passed. Manifest: {manifest_path}",
            },
        }

    notes = ["Plan coverage rejected mechanically.", f"Manifest: {manifest_path}"]
    for failure in failures:
        leaves = ", ".join(failure["leaves"]) if failure["leaves"] else "none"
        notes.append(
            "- "
            f"section_id={failure['section_id']} "
            f"item_id={failure['item_id']} "
            f"status={failure['status']} "
            f"detail={failure['detail']} "
            f"leaves={leaves}"
        )
    return {
        "server": "gobby-tasks",
        "tool": "mark_task_review_rejected",
        "arguments": {
            "task_id": root_task_ref,
            "rejection_notes": "\n".join(notes),
        },
    }


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


__all__ = [
    "ExpansionQaCoverageError",
    "run_expansion_qa_coverage",
]
