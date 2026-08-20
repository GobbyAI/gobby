"""Safety classification for inline Python pipeline stages.

A script is read-only when every node is drawn from a closed allowlist: pure
stdlib modules, a short builtin set, in-memory methods, ``open`` in read modes,
and the standard streams. Nothing reachable from those can touch the
filesystem or process, so the classifier never needs to reason about intent.
"""

import ast

from gobby.hooks.code_navigation import shell_command_name

_PYTHON_PIPELINE_MODULES = frozenset({"json", "sys"})
# Pure stdlib modules: no filesystem, process, network, or string-addressed
# reflection surface (``operator.attrgetter`` is why ``operator`` is absent).
# Their attributes are admitted one level deep (``re.compile``), never chained
# (``csv.io.open``), so a module's own imports stay out of reach.
_PYTHON_PIPELINE_PURE_MODULES = frozenset(
    "collections csv datetime decimal fractions functools itertools math re statistics string "
    "textwrap".split()
)
_PYTHON_PIPELINE_BUILTINS = frozenset(
    "abs all any bin bool bytes chr dict divmod enumerate filter float frozenset hash hex int "
    "isinstance iter len list map max min next oct ord pow print range repr reversed round set "
    "slice sorted str sum tuple zip "
    "AttributeError Exception IndexError KeyError StopIteration TypeError UnicodeDecodeError "
    "UnicodeEncodeError ValueError ZeroDivisionError".split()
)
# In-memory methods on values a read-only pipeline can hold: strings, bytes,
# containers, regex matches, and read-mode file handles.
_PYTHON_PIPELINE_METHODS = frozenset(
    "add append capitalize casefold center clear close copy count decode difference discard "
    "elements encode end endswith expandtabs extend find findall finditer format fullmatch get "
    "group groupdict groups index insert intersection isalnum isalpha isdecimal isdigit "
    "isdisjoint islower isnumeric isspace issubset issuperset istitle isupper items join keys "
    "ljust lower lstrip match most_common partition pop popitem read readline readlines remove "
    "removeprefix removesuffix replace reverse rfind rindex rjust rpartition rsplit rstrip "
    "search setdefault sort span split splitlines start startswith strip sub subn swapcase "
    "symmetric_difference title total union update upper values zfill".split()
)
_OPEN_READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})
_PYTHON_PIPELINE_STREAM_CALLS = frozenset(
    "sys.stdin.buffer.read sys.stdin.buffer.readline sys.stdin.buffer.readlines sys.stdin.read "
    "sys.stdin.readline sys.stdin.readlines sys.stderr.flush sys.stderr.write sys.stdout.flush "
    "sys.stdout.write sys.stdout.writelines".split()
)
_PYTHON_PIPELINE_SAFE_ATTRIBUTES = frozenset(
    {"sys.stdin", "sys.stdin.buffer", "sys.stderr", "sys.stdout"}
)
_PYTHON_PIPELINE_KEY_CALLBACK_BUILTINS = frozenset({"max", "min", "sorted"})
# Names a script may never rebind: a rebinding would let the module and builtin
# allowances above apply to an arbitrary value.
_PYTHON_PIPELINE_RESERVED_NAMES = (
    _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES | _PYTHON_PIPELINE_BUILTINS
)
_PYTHON_PIPELINE_BLOCKED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
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


def _is_pure_module_attribute(name: str | None) -> bool:
    """``module.attr`` exactly one level below an admitted pure module."""
    if name is None:
        return False
    root, _, attribute = name.partition(".")
    return root in _PYTHON_PIPELINE_PURE_MODULES and bool(attribute) and "." not in attribute


def _argument_names(arguments: ast.arguments) -> frozenset[str]:
    return frozenset(
        argument.arg
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *([arguments.vararg] if arguments.vararg else []),
            *([arguments.kwarg] if arguments.kwarg else []),
        ]
    )


def _rebound_names(tree: ast.AST) -> frozenset[str]:
    """Every name the script binds other than through ``from ... import``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return frozenset(names)


def _imported_names(tree: ast.AST) -> frozenset[str]:
    return frozenset(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )


def _is_safe_python_pipeline_callable(
    node: ast.expr,
    local_names: frozenset[str],
    *,
    allow_none: bool = False,
) -> bool:
    if allow_none and isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in _PYTHON_PIPELINE_BUILTINS or node.id in local_names
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
        elif call_name not in _PYTHON_PIPELINE_BUILTINS and call_name not in local_names:
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
        elif _is_pure_module_attribute(attribute_name):
            pass
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
            alias.name in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES
            and alias.asname in {None, alias.name}
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        return (
            node.level == 0
            and node.module in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES
            and all(
                alias.name != "*"
                and not _has_dunder_name(alias.name)
                and alias.asname in {None, alias.name}
                for alias in node.names
            )
        )
    if isinstance(node, ast.Name):
        if _has_dunder_name(node.id) or isinstance(node.ctx, ast.Del):
            return False
        if isinstance(node.ctx, ast.Store):
            return node.id not in _PYTHON_PIPELINE_RESERVED_NAMES
        return node.id in local_names | _PYTHON_PIPELINE_RESERVED_NAMES
    if isinstance(node, ast.Attribute):
        if not isinstance(node.ctx, ast.Load):
            return False
        name = _python_attribute_name(node)
        if name is None or _has_dunder_name(name):
            return False
        return name in _PYTHON_PIPELINE_SAFE_ATTRIBUTES or _is_pure_module_attribute(name)
    if isinstance(node, ast.Call):
        return _is_safe_python_pipeline_call(node, local_names)
    if isinstance(node, ast.Lambda | ast.FunctionDef):
        scoped_names = local_names | _argument_names(node.args)
        if not _is_safe_python_pipeline_node(node.args, local_names):
            return False
        if isinstance(node, ast.Lambda):
            return _is_safe_python_pipeline_node(node.body, scoped_names)
        return (
            all(_is_safe_python_pipeline_node(item, local_names) for item in node.decorator_list)
            and (node.returns is None or _is_safe_python_pipeline_node(node.returns, local_names))
            and all(_is_safe_python_pipeline_node(child, scoped_names) for child in node.body)
        )
    if isinstance(node, ast.comprehension) and node.is_async:
        return False
    if isinstance(node, ast.With):
        for item in node.items:
            if not (
                isinstance(item.context_expr, ast.Call)
                and _is_safe_python_pipeline_call(item.context_expr, local_names)
            ):
                return False
            if item.optional_vars is not None and not (
                isinstance(item.optional_vars, ast.Name)
                and _is_safe_python_pipeline_node(item.optional_vars, local_names)
            ):
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
    rebound_names = _rebound_names(tree)
    if rebound_names & _PYTHON_PIPELINE_RESERVED_NAMES:
        return False
    return _is_safe_python_pipeline_node(tree, rebound_names | _imported_names(tree))
