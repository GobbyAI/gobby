"""Detect newly introduced test dependencies on bundled workflow definitions."""

import ast
import re
from collections import Counter
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

_SELECTION_FIELDS = frozenset(
    {
        "agent",
        "agent_name",
        "agent_type",
        "agent_definition",
        "agent_definition_name",
        "definition_name",
        "definition",
        "pipeline",
        "pipeline_name",
        "workflow",
        "workflow_name",
        "step_workflow",
        "step_workflow_name",
        "rule",
        "rule_name",
        "agents",
        "rules",
        "pipelines",
        "workflows",
    }
)
_DEFINITION_CONTEXT = re.compile(
    r"agent|definition|pipeline|workflow|rule|\b(?:defs|registry|loader)\b", re.I
)
_TEMPLATE_TREE = "install/shared/workflows"


def discover_definition_names(templates: Path | None = None) -> frozenset[str]:
    """Read identifiers from the shipped catalog, never from a name allowlist."""
    root = templates or Path(str(files("gobby").joinpath(_TEMPLATE_TREE)))
    names: set[str] = set()
    for path in sorted(root.rglob("*.yaml")):
        if "deprecated" in path.relative_to(root).parts:
            continue
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("name"), str):
            names.add(data["name"])
        rules = data.get("rules")
        if isinstance(rules, dict):
            names.update(str(name) for name in rules)
    return frozenset(names - {"default"})


def _string(node: ast.AST, bindings: dict[str, ast.AST], seen: frozenset[str] = frozenset()) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _string(bindings[node.id], bindings, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Div):
        separator = "/" if isinstance(node.op, ast.Div) else ""
        return _string(node.left, bindings, seen) + separator + _string(node.right, bindings, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path":
        return _string(node.args[0], bindings, seen) if node.args else ""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "joinpath":
            return "/".join(
                [_string(node.func.value, bindings, seen)]
                + [_string(arg, bindings, seen) for arg in node.args]
            )
    return ""


def _yaml_pins(data: Any, names: frozenset[str]) -> list[str]:
    pins: list[str] = []
    if isinstance(data, dict):
        definition = bool({"prompts", "steps", "effects", "event"} & data.keys()) or data.get(
            "type"
        ) in ("agent", "rule", "workflow", "pipeline")
        for key, value in data.items():
            selector = key in {"include", "exclude"}
            if key in _SELECTION_FIELDS or (key == "name" and definition) or selector:
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str) and (not selector or item.startswith("name:")):
                        identifier = item.removeprefix("name:")
                        if identifier in names:
                            pins.append(f"definition:{identifier}")
            if key == "rules" and isinstance(value, dict):
                pins.extend(f"definition:{name}" for name in value if name in names)
            if key != "prompts":
                pins.extend(_yaml_pins(value, names))
    elif isinstance(data, list):
        for item in data:
            pins.extend(_yaml_pins(item, names))
    return pins


def _context(
    node: ast.AST, bindings: dict[str, ast.AST], seen: frozenset[str] = frozenset()
) -> str:
    """Trace receiver types and aliases without treating their prose as type names."""
    parts: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            parts.append(item.id)
            if item.id in bindings and item.id not in seen:
                parts.append(_context(bindings[item.id], bindings, seen | {item.id}))
        elif isinstance(item, ast.Attribute):
            parts.append(item.attr)
    return " ".join(parts)


def _selection_values(
    node: ast.AST,
    bindings: dict[str, ast.AST],
    names: frozenset[str],
    seen: frozenset[str] = frozenset(),
    *,
    selector: bool = False,
) -> list[str]:
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _selection_values(
            bindings[node.id], bindings, names, seen | {node.id}, selector=selector
        )
    if isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict):
        children = node.keys if isinstance(node, ast.Dict) else node.elts
        return [
            pin
            for child in children
            if child is not None
            for pin in _selection_values(child, bindings, names, seen, selector=selector)
        ]
    value = _string(node, bindings)
    if selector and not value.startswith("name:"):
        return []
    value = value.removeprefix("name:")
    return [f"definition:{value}"] if value in names else []


def _prose_nodes(node: ast.AST) -> set[ast.AST]:
    """Prose containers do not exempt executable expressions embedded in them."""
    if isinstance(node, ast.Call | ast.Subscript):
        return set()
    result = {node}
    for child in ast.iter_child_nodes(node):
        result.update(_prose_nodes(child))
    return result


def definition_pins(source: str, names: frozenset[str]) -> list[str]:
    """Find selections and template reads, excluding prose and protocol role fields."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Non-Python fixtures and temporarily incomplete source still get explicit-key checks.
        pins = _yaml_pins(_load_yaml(source), names)
        pattern = (
            r"\b(" + "|".join(sorted(_SELECTION_FIELDS)) + r")[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']"
        )
        pins.extend(f"definition:{m[2]}" for m in re.finditer(pattern, source) if m[2] in names)
        return pins
    bindings: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            bindings[node.target.id] = node.value
        elif isinstance(node, ast.arg) and node.annotation is not None:
            bindings[node.arg] = node.annotation
        elif isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = ast.Name(id=alias.name)
    prose: set[ast.AST] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and "AgentPromptBlocks" in _context(node.func, bindings).split()
        ):
            prose.add(node)
            for prompt_expression in [*node.args, *(keyword.value for keyword in node.keywords)]:
                prose.update(_prose_nodes(prompt_expression))
        elif isinstance(node, ast.keyword) and node.arg == "prompts":
            prose.update(_prose_nodes(node.value))
        elif isinstance(node, ast.Dict):
            for key, prompt_expression in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "prompts":
                    prose.update(_prose_nodes(prompt_expression))
    pins = []
    for node in ast.walk(tree):
        if node in prose:
            continue
        if isinstance(node, ast.Call):
            call_context = _context(node.func, bindings)
            if isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                path = _string(receiver, bindings).replace("\\", "/")
                if _TEMPLATE_TREE in path and node.func.attr in {"read_text", "read_bytes", "open"}:
                    pins.append(f"template:{path[path.index(_TEMPLATE_TREE) :]}")
                elif _TEMPLATE_TREE in path and node.func.attr in {"glob", "rglob"}:
                    # Name-neutral discovery is required for generic catalog validation.
                    pattern = _string(node.args[0], bindings) if node.args else ""
                    named = names.intersection(re.split(r"[/.*?\[\]]", pattern))
                    if pattern and (named or not any(char in pattern for char in "*?[")):
                        pins.append(f"template:{path[path.index(_TEMPLATE_TREE) :]}/{pattern}")
            selection = bool(_DEFINITION_CONTEXT.search(call_context))
            callee = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            ).lstrip("_")
            lookup = (
                bool(
                    re.match(
                        r"(?:load|resolve|lookup|select|get|find|run)"
                        r"(?:$|_(?:by_name|agent|definition|pipeline|workflow|step_workflow|rule)(?:_|$))",
                        callee,
                    )
                )
                or "definition" in callee.lower()
            )
            creator = callee in {"create", "register", "insert", "upsert"} and bool(
                re.search(r"definition|pipeline|workflow|rule", call_context, re.I)
            )
            if selection and (lookup or creator):
                for arg in node.args:
                    pins.extend(_selection_values(arg, bindings, names))
            for keyword in node.keywords:
                if keyword.arg in _SELECTION_FIELDS or (keyword.arg == "name" and selection):
                    pins.extend(_selection_values(keyword.value, bindings, names))
                elif keyword.arg in {"include", "exclude"}:
                    pins.extend(_selection_values(keyword.value, bindings, names, selector=True))
            if re.search(r"load|read|open|parse|source_texts", call_context, re.I):
                for arg in node.args:
                    value = _string(arg, bindings).replace("\\", "/")
                    if _TEMPLATE_TREE in value:
                        pins.append(f"template:{value[value.index(_TEMPLATE_TREE) :]}")
        elif isinstance(node, ast.Subscript):
            context = _context(node.value, bindings)
            value = _string(node.slice, bindings)
            if value in names and _DEFINITION_CONTEXT.search(context):
                pins.append(f"definition:{value}")
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            definition = bool({"prompts", "steps", "effects", "event"} & keys)
            definition |= any(
                isinstance(key, ast.Constant)
                and key.value == "type"
                and _string(value, bindings) in {"agent", "rule", "workflow", "pipeline"}
                for key, value in zip(node.keys, node.values, strict=True)
            )
            for key, value_node in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant | ast.Name):
                    continue
                field = key.value if isinstance(key, ast.Constant) else key.id
                if field in _SELECTION_FIELDS or (field == "name" and definition):
                    pins.extend(_selection_values(value_node, bindings, names))
                elif field in {"include", "exclude"}:
                    pins.extend(_selection_values(value_node, bindings, names, selector=True))
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(
                isinstance(target, ast.Name) and target.id in _SELECTION_FIELDS
                for target in targets
            ):
                pins.extend(_selection_values(node.value, bindings, names))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.replace("\\", "/")
            if _TEMPLATE_TREE in value and names.intersection(re.split(r"[/.*?\[\]]", value)):
                pins.append(f"template:{value[value.index(_TEMPLATE_TREE) :]}")
            if "\n" in node.value:
                pins.extend(_yaml_pins(_load_yaml(node.value), names))
    return pins


def _load_yaml(source: str) -> Any:
    try:
        return yaml.safe_load(source)
    except yaml.YAMLError:
        return None


def _test_path(root: Path, raw: Any) -> Path | None:
    if not isinstance(raw, str):
        return None
    path = (root / raw).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return path if "tests" in relative.parts[:-1] else None


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _edit(text: str, edit: Mapping[str, Any]) -> str:
    old = edit.get("old_string", edit.get("old_str", edit.get("old_text")))
    new = edit.get("new_string", edit.get("new_str", edit.get("new_text")))
    if not isinstance(old, str) or not isinstance(new, str) or not old or old not in text:
        raise ValueError("could not project the targeted edit")
    return text.replace(old, new, -1 if edit.get("replace_all") else 1)


def _patches(patch: str) -> list[tuple[str, str, str | None, list[list[str]]]]:
    operations: list[tuple[str, str, str | None, list[list[str]]]] = []
    kind = path = ""
    destination: str | None = None
    hunks: list[list[str]] = []
    for line in patch.splitlines(keepends=True):
        header = re.match(r"\*\*\* (Add|Update|Delete) File: (.+)", line.rstrip("\r\n"))
        if header:
            if path:
                operations.append((kind, path, destination, hunks))
            kind, path = header[1], header[2]
            destination, hunks = None, []
        elif line.startswith("*** Move to: "):
            destination = line.removeprefix("*** Move to: ").strip()
        elif line.startswith("@@"):
            hunks.append([])
        elif line[:1] in {" ", "+", "-"} and not line.startswith(("---", "+++")):
            if not hunks:
                hunks.append([])
            hunks[-1].append(line)
    if path:
        operations.append((kind, path, destination, hunks))
    return operations


def _apply_hunks(text: str, hunks: list[list[str]], *, create: bool) -> str:
    if create:
        return "".join(line[1:] for hunk in hunks for line in hunk if line.startswith("+"))
    cursor = 0
    for hunk in hunks:
        old = "".join(line[1:] for line in hunk if not line.startswith("+"))
        new = "".join(line[1:] for line in hunk if not line.startswith("-"))
        index = text.find(old, cursor)
        if index < 0:
            # apply_patch also accepts the final source line without its trailing newline.
            old, new = old.rstrip("\n"), new.rstrip("\n")
            index = text.find(old, cursor)
        if index < 0:
            raise ValueError("could not project the patch hunk")
        text = text[:index] + new + text[index + len(old) :]
        cursor = index + len(new)
    return text


def projected_definition_pins(
    tool_input: Any,
    project_path: str | Path | None,
    event_data: Mapping[str, Any] | None = None,
    *,
    names: frozenset[str] | None = None,
) -> list[str]:
    """Return added semantic pins; existing violations do not freeze their files."""
    if project_path is None:
        return []
    root = Path(project_path).resolve()
    payload = tool_input if isinstance(tool_input, Mapping) else {}
    if not payload and event_data and isinstance(event_data.get("tool_input"), Mapping):
        payload = event_data["tool_input"]
    before: dict[Path, str] = {}
    after: dict[Path, str] = {}
    failures: list[str] = []
    handled: set[Path] = set()

    def current(path: Path) -> str:
        if path not in before:
            before[path] = _read(path)
        return after.get(path, before[path])

    patch = tool_input if isinstance(tool_input, str) else payload.get("patch")
    if isinstance(patch, str):
        for kind, raw, destination, hunks in _patches(patch):
            origin_test = _test_path(root, raw)
            if origin_test is not None:
                handled.add(origin_test)
            target = _test_path(root, destination or raw)
            if target is None:
                continue
            handled.add(target)
            if kind == "Delete":
                after.pop(target, None)
                continue
            try:
                original = current(target)
                if destination:
                    origin = (root / raw).resolve()
                    if not origin.is_relative_to(root):
                        raise ValueError("move source is outside the project")
                    source = current(origin)
                else:
                    source = original
                after[target] = _apply_hunks(source, hunks, create=kind == "Add")
            except (OSError, UnicodeError, ValueError) as exc:
                failures.append(f"{target.relative_to(root)}: {exc}; use a projectable write")
    else:
        changes = payload.get("changes")
        for change in changes if isinstance(changes, list) else [payload]:
            if not isinstance(change, Mapping):
                continue
            target = _test_path(root, change.get("file_path") or change.get("path"))
            if target is None:
                continue
            handled.add(target)
            try:
                text = current(target)
                if isinstance(change.get("content"), str):
                    after[target] = change["content"]
                else:
                    edits = change.get("edits")
                    for edit in edits if isinstance(edits, list) else [change]:
                        if isinstance(edit, Mapping):
                            text = _edit(text, edit)
                    after[target] = text
            except (OSError, UnicodeError, ValueError) as exc:
                failures.append(f"{target.relative_to(root)}: {exc}; use a projectable write")
    canonical_paths = event_data.get("canonical_file_paths", []) if event_data else []
    if isinstance(canonical_paths, list):
        for raw in canonical_paths:
            target = _test_path(root, raw)
            if target is not None and target not in handled:
                failures.append(f"{target.relative_to(root)}: use a projectable write")
    catalog = (
        (names if names is not None else discover_definition_names()) if after else frozenset()
    )
    for path, content in after.items():
        added = Counter(definition_pins(content, catalog)) - Counter(
            definition_pins(before[path], catalog)
        )
        failures.extend(f"{path.relative_to(root)}: {pin}" for pin in added)
    return failures
