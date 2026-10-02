"""Normalize provider tool arguments for transcript-derived task evidence."""

from __future__ import annotations

import ast
import io
import os
import tokenize
from typing import Any

_COMMAND_KEYS = ("cmd", "command", "script")
_PATH_KEYS = ("file_path", "target_file", "path", "notebook_path", "TargetFile")


def extract_command(arguments: dict[str, Any]) -> str:
    """Return the first non-empty shell command exposed by a provider tool."""
    for key in _COMMAND_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def edited_source(tool_name: str, arguments: dict[str, Any], previous: str | None) -> str | None:
    """Replay a structured source edit; unknown or ambiguous changes lose the snapshot."""
    if tool_name == "write":
        content = arguments.get("content")
        return content if isinstance(content, str) else None
    if previous is None or tool_name not in {"edit", "multiedit"}:
        return None
    changes = arguments.get("edits") if tool_name == "multiedit" else [arguments]
    if not isinstance(changes, list):
        return None
    for change in changes:
        if not isinstance(change, dict):
            return None
        old, new = change.get("old_string"), change.get("new_string")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return None
        count = previous.count(old)
        if count == 0 or (count != 1 and not change.get("replace_all")):
            return None
        previous = previous.replace(old, new, -1 if change.get("replace_all") else 1)
    return previous


def python_edit_tokens(text: str) -> list[tuple[int, str]] | None:
    """Compare edit snippets, including a function prefix ending in an open call."""
    ignored = {
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.COMMENT,
        tokenize.ENDMARKER,
    }
    tokens: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type not in ignored:
                tokens.append((token.type, token.string))
    except tokenize.TokenError as exc:
        if exc.args[0] not in {
            "EOF in multi-line statement",
            "unexpected EOF in multi-line statement",
        }:
            return None
    except (IndentationError, SyntaxError):
        return None
    return tokens


def python_keyword_stub(old: str, new: str) -> tuple[str, tuple[str, ...]] | None:
    """Prove newly optional, immediately discarded keywords leave the old body intact."""
    first_parameter, separator, _ = old.lstrip().partition(":")
    partial_header = bool(separator and first_parameter.strip().isidentifier())
    if partial_header:
        old_tokens = python_edit_tokens("def __partial_stub__(*, " + old.lstrip())
        new_tokens = python_edit_tokens("def __partial_stub__(*, " + new.lstrip())
    else:
        old_tokens, new_tokens = python_edit_tokens(old), python_edit_tokens(new)
    if not old_tokens or not new_tokens:
        return None
    headers: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    bodies: list[list[tuple[int, str]]] = []
    for tokens in (old_tokens, new_tokens):
        if tokens[0][1] not in {"def", "async"}:
            return None
        depth = 0
        for index, (_, token) in enumerate(tokens):
            depth += int(token in {"(", "[", "{"}) - int(token in {")", "]", "}"})
            if token == ":" and depth == 0:
                try:
                    tree = ast.parse(tokenize.untokenize(tokens[: index + 1]) + "\n    pass")
                except (SyntaxError, ValueError):
                    return None
                node = tree.body[0]
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    return None
                headers.append(node)
                bodies.append(tokens[index + 1 :])
                break
        else:
            return None
    before, after = headers
    old_names = {arg.arg for arg in before.args.kwonlyargs}
    added = tuple(arg.arg for arg in after.args.kwonlyargs if arg.arg not in old_names)
    if not added:
        return None
    keep_args, keep_defaults = [], []
    for arg, default in zip(after.args.kwonlyargs, after.args.kw_defaults, strict=True):
        if arg.arg in added:
            if not isinstance(default, ast.Constant):
                return None
        else:
            keep_args.append(arg)
            keep_defaults.append(default)
    after.args.kwonlyargs, after.args.kw_defaults = keep_args, keep_defaults
    if ast.dump(before) != ast.dump(after):
        return None
    body = bodies[1]
    start = 1 if body and body[0][0] == tokenize.STRING else 0
    if len(body) <= start or body[start] != (tokenize.NAME, "del"):
        return None
    end, names = start + 1, []
    while end < len(body) and body[end][0] == tokenize.NAME:
        names.append(body[end][1])
        end += 1
        if end >= len(body) or body[end][1] != ",":
            break
        end += 1
    if tuple(names) != added or body[:start] + body[end:] != bodies[0]:
        return None
    # A signature tail proves body preservation, but cannot name a test's API.
    return ("" if partial_header else before.name), added


def python_noop_module(source: str) -> dict[str, frozenset[str] | None] | None:
    """Recognize a new API module containing declarations and inert class bodies only."""
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    classes: dict[str, frozenset[str] | None] = {}
    for statement in module.body:
        if isinstance(statement, ast.Import | ast.ImportFrom) or _is_docstring(statement):
            continue
        if (
            isinstance(statement, ast.Assign)
            and all(isinstance(target, ast.Name) for target in statement.targets)
            and _shape_expression(statement.value)
        ):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    classes[target.id] = None
            continue
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if not _noop_method(statement):
                return None
            classes[statement.name] = frozenset()
            continue
        if not isinstance(statement, ast.ClassDef) or statement.bases or statement.keywords:
            return None
        if any(
            not isinstance(item, ast.Name) or item.id != "dataclass"
            for item in statement.decorator_list
        ):
            return None
        methods: set[str] = set()
        for member in statement.body:
            if _is_docstring(member) or isinstance(member, ast.Pass):
                continue
            if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                if _shape_expression(member.annotation) and _literal_expression(member.value):
                    continue
                return None
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) or not _noop_method(
                member
            ):
                return None
            methods.add(member.name)
        classes[statement.name] = frozenset(methods)
    return classes or None


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _shape_expression(node: ast.expr) -> bool:
    return all(
        isinstance(
            item,
            ast.Name
            | ast.Subscript
            | ast.Tuple
            | ast.List
            | ast.Constant
            | ast.Load
            | ast.BinOp
            | ast.BitOr,
        )
        for item in ast.walk(node)
    )


def _literal_expression(node: ast.expr | None) -> bool:
    if node is None:
        return True
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return False
    return True


def _noop_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if any(not isinstance(item, ast.Name) or item.id != "property" for item in node.decorator_list):
        return False
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    arguments.extend(
        argument for argument in (node.args.vararg, node.args.kwarg) if argument is not None
    )
    if any(
        argument.annotation is not None and not _shape_expression(argument.annotation)
        for argument in arguments
    ):
        return False
    if node.returns is not None and not _shape_expression(node.returns):
        return False
    if not all(
        _literal_expression(value) for value in [*node.args.defaults, *node.args.kw_defaults]
    ):
        return False
    body = [statement for statement in node.body if not _is_docstring(statement)]
    if node.name == "__init__":
        parameters = {argument.arg for argument in arguments if argument.arg != "self"}
        value: ast.expr | None
        for statement in body:
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign):
                if not _shape_expression(statement.annotation):
                    return False
                targets, value = [statement.target], statement.value
            else:
                return False
            if not all(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                for target in targets
            ):
                return False
            if not (
                _literal_expression(value) or isinstance(value, ast.Name) and value.id in parameters
            ):
                return False
        return bool(body)
    if len(body) != 1:
        return False
    statement = body[0]
    if isinstance(statement, ast.Raise) and statement.cause is None:
        error = statement.exc
        return (
            isinstance(error, ast.Name)
            and error.id == "NotImplementedError"
            or (
                isinstance(error, ast.Call)
                and isinstance(error.func, ast.Name)
                and error.func.id == "NotImplementedError"
                and not error.keywords
                and all(isinstance(value, ast.Constant) for value in error.args)
            )
        )
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Return)
        and (
            statement.value is None
            or isinstance(statement.value, ast.Constant)
            and (statement.value.value is None or statement.value.value is False)
        )
    )


def normalize_tool_name(tool_name: str) -> str:
    """Reduce provider-qualified tool names to their normalized basename."""
    normalized = tool_name.casefold().replace("-", "_")
    for separator in ("__", ".", "/"):
        if separator in normalized:
            normalized = normalized.rsplit(separator, 1)[-1]
    return normalized


def extract_edit_paths(
    tool_name: str,
    arguments: dict[str, Any],
    repo_path: str,
    *,
    require_proven_checkout: bool = False,
) -> set[str]:
    """Extract edits, retaining call-time checkout provenance when required."""
    values: set[str] = set()
    for key in _PATH_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            resolved = resolve_edit_path(value, arguments, repo_path, require_proven_checkout)
            if resolved is not None:
                values.add(resolved)
    if tool_name in {"apply_patch", "exec"}:
        raw = arguments.get("raw") or arguments.get("patch") or arguments.get("input")
        if isinstance(raw, str):
            if tool_name == "exec":
                if "tools.apply_patch" not in raw:
                    return values
                raw = raw.replace(r"\r", "\r").replace(r"\n", "\n")
            for line in raw.splitlines():
                for prefix in ("*** Add File: ", "*** Delete File: ", "*** Update File: "):
                    if line.startswith(prefix):
                        resolved = resolve_edit_path(
                            line.removeprefix(prefix), arguments, repo_path, require_proven_checkout
                        )
                        if resolved is not None:
                            values.add(resolved)
    return values


def resolve_edit_path(
    path: str,
    arguments: dict[str, Any],
    repo_path: str,
    require_proven_checkout: bool,
) -> str | None:
    """Resolve a relative edit only when the tool call names an absolute cwd."""
    if not require_proven_checkout:
        return normalize_known_path(path, repo_path)
    if os.path.isabs(path):
        return os.path.realpath(path)
    for key in ("workdir", "cwd"):
        cwd = arguments.get(key)
        if isinstance(cwd, str) and os.path.isabs(cwd):
            return os.path.realpath(os.path.join(cwd, path))
    return None


def match_task_file(
    path: str,
    task_files: set[str],
    repo_path: str | None = None,
    task_checkout_paths: frozenset[tuple[str, str]] | None = None,
) -> str | None:
    """Map an edit only from an exact task-attributed checkout/path pair."""
    if task_checkout_paths is not None:
        if not os.path.isabs(path):
            return None
        absolute = os.path.realpath(path)
        for root, task_path in task_checkout_paths:
            try:
                relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            except ValueError:
                continue
            if relative == task_path and relative in task_files:
                return relative
        return None
    if path in task_files:
        return path
    if not (os.path.isabs(path) or path.startswith("../")):
        return None
    return max(
        (task_file for task_file in task_files if path.endswith(f"/{task_file}")),
        key=len,
        default=None,
    )


def normalize_known_path(path: str, repo_path: str) -> str:
    """Normalize a provider path relative to the repository when possible."""
    normalized = os.path.normpath(path)
    if os.path.isabs(normalized):
        try:
            normalized = os.path.relpath(normalized, repo_path)
        except ValueError:
            pass
    while normalized.startswith(f".{os.sep}"):
        normalized = normalized[2:]
    return normalized.replace(os.sep, "/")


__all__ = [
    "extract_command",
    "extract_edit_paths",
    "match_task_file",
    "normalize_known_path",
    "normalize_tool_name",
]
