"""Safety classification for inline Python pipeline stages."""

import ast

from gobby.hooks.code_navigation import shell_command_name

_PYTHON_PIPELINE_MODULES = frozenset({"json", "sys"})
_PYTHON_PIPELINE_BUILTINS = frozenset(
    "all any bool dict enumerate filter float int iter len list map max min next print range "
    "reversed set sorted str sum tuple zip".split()
)
_PYTHON_PIPELINE_METHODS = frozenset(
    "casefold close count decode encode endswith format get index items join keys lower lstrip "
    "read readline readlines replace rsplit rstrip split splitlines startswith strip upper "
    "values".split()
)
_OPEN_READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})
_PYTHON_PIPELINE_STREAM_CALLS = frozenset(
    "sys.stdin.buffer.read sys.stdin.buffer.readline sys.stdin.buffer.readlines sys.stdin.read "
    "sys.stdin.readline sys.stdin.readlines sys.stdout.flush sys.stdout.write "
    "sys.stdout.writelines".split()
)
_PYTHON_PIPELINE_SAFE_ATTRIBUTES = frozenset({"sys.stdin", "sys.stdin.buffer", "sys.stdout"})
_PYTHON_PIPELINE_KEY_CALLBACK_BUILTINS = frozenset({"max", "min", "sorted"})
_PYTHON_PIPELINE_BLOCKED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.ClassDef,
    ast.Delete,
    ast.FunctionDef,
    ast.Global,
    ast.ImportFrom,
    ast.Nonlocal,
)


def _inline_interpreter_parts(parts: list[str]) -> list[str]:
    if not parts:
        return []
    if shell_command_name(parts[0]) != "uv" or parts[1:2] != ["run"]:
        return parts
    for index, part in enumerate(parts[2:], start=2):
        if shell_command_name(part) in {"python", "python3", "node", "ruby"}:
            return parts[index:]
    return []


def _python_inline_script(parts: list[str]) -> str | None:
    interpreter_parts = _inline_interpreter_parts(parts)
    if not interpreter_parts or shell_command_name(interpreter_parts[0]) not in {
        "python",
        "python3",
    }:
        return None
    try:
        script_index = interpreter_parts.index("-c") + 1
    except ValueError:
        return None
    if script_index >= len(interpreter_parts):
        return None
    return interpreter_parts[script_index]


def _python_attribute_name(node: ast.Attribute) -> str | None:
    parts = [node.attr]
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return None
    parts.append(value.id)
    return ".".join(reversed(parts))


def _has_dunder_name(name: str) -> bool:
    return any(part.startswith("__") for part in name.split("."))


def _is_safe_python_pipeline_callable(
    node: ast.expr,
    local_names: frozenset[str],
    *,
    allow_none: bool = False,
) -> bool:
    if allow_none and isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in _PYTHON_PIPELINE_BUILTINS
    return isinstance(node, ast.Lambda) and _is_safe_python_pipeline_node(node, local_names)


def _has_safe_python_pipeline_callbacks(
    node: ast.Call,
    call_name: str,
    local_names: frozenset[str],
) -> bool:
    if call_name in {"filter", "map"}:
        return bool(node.args) and _is_safe_python_pipeline_callable(
            node.args[0], local_names, allow_none=call_name == "filter"
        )
    if call_name == "iter" and len(node.args) >= 2:
        return _is_safe_python_pipeline_callable(node.args[0], local_names)
    if call_name in _PYTHON_PIPELINE_KEY_CALLBACK_BUILTINS:
        return all(
            keyword.arg != "key"
            or _is_safe_python_pipeline_callable(
                keyword.value,
                local_names,
                allow_none=True,
            )
            for keyword in node.keywords
        )
    return True


def _is_read_only_open_call(node: ast.Call) -> bool:
    """Allow ``open(...)`` only when the mode is absent or a literal read mode."""
    mode: ast.expr | None = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return True
    return isinstance(mode, ast.Constant) and mode.value in _OPEN_READ_MODES


def _is_safe_python_pipeline_call(node: ast.Call, local_names: frozenset[str]) -> bool:
    if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    ):
        return False

    call_name: str
    if isinstance(node.func, ast.Name):
        call_name = node.func.id
        if call_name == "open":
            if not _is_read_only_open_call(node):
                return False
        elif call_name not in _PYTHON_PIPELINE_BUILTINS:
            return False
    elif isinstance(node.func, ast.Attribute):
        attribute_name = _python_attribute_name(node.func)
        if _has_dunder_name(attribute_name or node.func.attr):
            return False
        call_name = attribute_name or node.func.attr
        if call_name in _PYTHON_PIPELINE_STREAM_CALLS or call_name in {
            "json.dumps",
            "json.loads",
        }:
            pass
        elif call_name == "json.load":
            if not node.args:
                return False
            source = node.args[0]
            stdin_source = isinstance(source, ast.Attribute) and _python_attribute_name(source) in {
                "sys.stdin",
                "sys.stdin.buffer",
            }
            open_source = isinstance(source, ast.Call) and _is_safe_python_pipeline_call(
                source, local_names
            )
            local_source = isinstance(source, ast.Name) and source.id in local_names
            if not (stdin_source or open_source or local_source):
                return False
        elif node.func.attr not in _PYTHON_PIPELINE_METHODS or not (
            _is_safe_python_pipeline_node(node.func.value, local_names)
        ):
            return False
    else:
        return False

    return _has_safe_python_pipeline_callbacks(node, call_name, local_names) and all(
        _is_safe_python_pipeline_node(argument, local_names)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
    )


def _is_safe_python_pipeline_node(node: ast.AST, local_names: frozenset[str]) -> bool:
    if isinstance(node, _PYTHON_PIPELINE_BLOCKED_NODES):
        return False
    if isinstance(node, ast.Import):
        return all(
            alias.name in _PYTHON_PIPELINE_MODULES and alias.asname in {None, alias.name}
            for alias in node.names
        )
    if isinstance(node, ast.Name):
        if _has_dunder_name(node.id) or isinstance(node.ctx, ast.Del):
            return False
        if isinstance(node.ctx, ast.Store):
            return node.id not in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_BUILTINS
        return node.id in local_names | _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_BUILTINS
    if isinstance(node, ast.Attribute):
        if not isinstance(node.ctx, ast.Load):
            return False
        name = _python_attribute_name(node)
        return name in _PYTHON_PIPELINE_SAFE_ATTRIBUTES and not _has_dunder_name(name or "")
    if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
        return False
    if isinstance(node, ast.Call):
        return _is_safe_python_pipeline_call(node, local_names)
    if isinstance(node, ast.Lambda):
        arguments = node.args
        lambda_names = frozenset(
            argument.arg
            for argument in [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                *([arguments.vararg] if arguments.vararg else []),
                *([arguments.kwarg] if arguments.kwarg else []),
            ]
        )
        defaults = [*arguments.defaults, *(item for item in arguments.kw_defaults if item)]
        return all(
            _is_safe_python_pipeline_node(default, local_names) for default in defaults
        ) and _is_safe_python_pipeline_node(node.body, local_names | lambda_names)
    if isinstance(node, ast.comprehension) and node.is_async:
        return False
    if isinstance(node, ast.With):
        for item in node.items:
            if not (
                isinstance(item.context_expr, ast.Call)
                and _is_safe_python_pipeline_call(item.context_expr, local_names)
            ):
                return False
            if item.optional_vars is not None and not isinstance(item.optional_vars, ast.Name):
                return False
        return all(_is_safe_python_pipeline_node(child, local_names) for child in node.body)
    return all(
        _is_safe_python_pipeline_node(child, local_names) for child in ast.iter_child_nodes(node)
    )


def _is_read_only_python_pipeline(parts: list[str]) -> bool:
    script = _python_inline_script(parts)
    if script is None:
        return False
    return _is_read_only_python_source(script)


def _is_read_only_python_source(script: str) -> bool:
    """Return True when a Python program provably performs no mutation."""
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return False
    local_names = frozenset(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
    return _is_safe_python_pipeline_node(tree, local_names)
