"""Classification for inline Python diagnostics and pipeline stages.

A script is read-only when every node is drawn from a closed allowlist: pure
stdlib modules, a short builtin set, in-memory methods, ``open`` in read modes,
read-only workbook-inspection APIs, and the standard streams. Nothing
reachable from those can mutate the filesystem or start a process. Explicit
filesystem mutations and reflection escapes are classified separately from
unsupported dynamic execution.
"""

import ast
from collections.abc import Mapping
from enum import StrEnum

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
    "elements encode end endswith expandtabs extend find findall finditer format fullmatch get glob "
    "group groupdict groups index insert intersection isalnum isalpha isdecimal isdigit "
    "isdisjoint islower isnumeric isspace issubset issuperset istitle isupper items join keys "
    "ljust lower lstrip match most_common namelist partition pop popitem read readline readlines "
    "relative_to remove "
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
_PYTHON_DIAGNOSTIC_VALUE_ATTRIBUTES = frozenset({"tag", "tail", "text"})
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
_PYTHON_REFLECTION_ESCAPE_NAMES = frozenset(
    {"__import__", "attrgetter", "compile", "delattr", "eval", "exec", "getattr", "setattr"}
)
_PYTHON_FILESYSTEM_MUTATION_CALLS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.makedirs",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.renames",
        "os.replace",
        "os.rmdir",
        "os.truncate",
        "os.unlink",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
    }
)
_PYTHON_FILESYSTEM_MUTATION_METHODS = frozenset(
    {
        "chmod",
        "lchmod",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
_PYTHON_PROCESS_CALLS = frozenset(
    {
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.system",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.run",
    }
)
_PYTHON_MUTATING_COMMANDS = frozenset(
    {"cp", "install", "mkdir", "mv", "patch", "rm", "rmdir", "touch", "truncate"}
)


class _PythonExecutionClassification(StrEnum):
    READ_ONLY = "read_only"
    MUTATION = "mutation"
    INDETERMINATE = "indeterminate"


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


def _imported_bindings(tree: ast.AST) -> Mapping[str, str]:
    """Map a bound import name to its canonical module or member path."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                bindings[bound_name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound_name = alias.asname or alias.name
                bindings[bound_name] = f"{node.module}.{alias.name}"
    return bindings


def _canonical_imported_name(
    name: str | None,
    imported_bindings: Mapping[str, str],
) -> str | None:
    if name is None:
        return None
    root, separator, remainder = name.partition(".")
    canonical_root = imported_bindings.get(root)
    if canonical_root is None:
        return name
    return f"{canonical_root}.{remainder}" if separator else canonical_root


def _call_name(node: ast.Call, imported_bindings: Mapping[str, str]) -> str | None:
    if isinstance(node.func, ast.Name):
        return _canonical_imported_name(node.func.id, imported_bindings)
    if isinstance(node.func, ast.Attribute):
        return _canonical_imported_name(_python_attribute_name(node.func), imported_bindings)
    return None


def _path_value_names(
    tree: ast.AST,
    imported_bindings: Mapping[str, str],
) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.NamedExpr):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Call)
            and _call_name(value, imported_bindings) == "pathlib.Path"
        ):
            names.add(target.id)
    return frozenset(names)


def _is_safe_python_pipeline_callable(
    node: ast.expr,
    local_names: frozenset[str],
    imported_bindings: Mapping[str, str],
    *,
    allow_none: bool = False,
) -> bool:
    if allow_none and isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in _PYTHON_PIPELINE_BUILTINS or node.id in local_names
    return isinstance(node, ast.Lambda) and _is_safe_python_pipeline_node(
        node, local_names, imported_bindings
    )


def _has_safe_python_pipeline_callbacks(
    node: ast.Call,
    call_name: str,
    local_names: frozenset[str],
    imported_bindings: Mapping[str, str],
) -> bool:
    if call_name in {"filter", "map"}:
        return bool(node.args) and _is_safe_python_pipeline_callable(
            node.args[0],
            local_names,
            imported_bindings,
            allow_none=call_name == "filter",
        )
    if call_name == "iter" and len(node.args) >= 2:
        return _is_safe_python_pipeline_callable(node.args[0], local_names, imported_bindings)
    if call_name in _PYTHON_PIPELINE_KEY_CALLBACK_BUILTINS:
        return all(
            keyword.arg != "key"
            or _is_safe_python_pipeline_callable(
                keyword.value,
                local_names,
                imported_bindings,
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


def _is_read_only_zipfile_call(node: ast.Call) -> bool:
    """Allow ``ZipFile(...)`` only when its mode is absent or literal ``r``."""
    mode: ast.expr | None = node.args[1] if len(node.args) >= 2 else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    return mode is None or (isinstance(mode, ast.Constant) and mode.value == "r")


def _is_safe_python_pipeline_call(
    node: ast.Call,
    local_names: frozenset[str],
    imported_bindings: Mapping[str, str],
) -> bool:
    if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    ):
        return False

    call_name: str
    if isinstance(node.func, ast.Name):
        call_name = node.func.id
        canonical_name = _canonical_imported_name(call_name, imported_bindings)
        if call_name == "open":
            if not _is_read_only_open_call(node):
                return False
        elif canonical_name == "pathlib.Path":
            pass
        elif call_name not in _PYTHON_PIPELINE_BUILTINS and call_name not in local_names:
            return False
    elif isinstance(node.func, ast.Attribute):
        attribute_name = _python_attribute_name(node.func)
        if _has_dunder_name(attribute_name or node.func.attr):
            return False
        call_name = attribute_name or node.func.attr
        canonical_name = _canonical_imported_name(attribute_name, imported_bindings)
        if call_name in _PYTHON_PIPELINE_STREAM_CALLS or call_name in {
            "json.dumps",
            "json.loads",
        }:
            pass
        elif canonical_name == "zipfile.ZipFile":
            if not _is_read_only_zipfile_call(node):
                return False
        elif canonical_name == "xml.etree.ElementTree.fromstring":
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
                source, local_names, imported_bindings
            )
            local_source = isinstance(source, ast.Name) and source.id in local_names
            if not (stdin_source or open_source or local_source):
                return False
        elif _is_pure_module_attribute(attribute_name):
            pass
        elif node.func.attr not in _PYTHON_PIPELINE_METHODS or not (
            _is_safe_python_pipeline_node(node.func.value, local_names, imported_bindings)
        ):
            return False
    else:
        return False

    return _has_safe_python_pipeline_callbacks(
        node, call_name, local_names, imported_bindings
    ) and all(
        _is_safe_python_pipeline_node(argument, local_names, imported_bindings)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
    )


def _is_safe_python_pipeline_node(
    node: ast.AST,
    local_names: frozenset[str],
    imported_bindings: Mapping[str, str],
) -> bool:
    if isinstance(node, _PYTHON_PIPELINE_BLOCKED_NODES):
        return False
    if isinstance(node, ast.Import):
        return all(
            (
                alias.name in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES
                and alias.asname in {None, alias.name}
            )
            or (alias.name == "zipfile" and alias.asname in {None, "zipfile"})
            or (alias.name == "xml.etree.ElementTree" and alias.asname in {None, "ET"})
            for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        return (
            node.level == 0
            and (
                node.module in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES
                or node.module == "pathlib"
            )
            and all(
                alias.name != "*"
                and not _has_dunder_name(alias.name)
                and alias.asname in {None, alias.name}
                and (node.module != "pathlib" or alias.name == "Path")
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
        if _has_dunder_name(name or node.attr):
            return False
        if node.attr in _PYTHON_DIAGNOSTIC_VALUE_ATTRIBUTES:
            return _is_safe_python_pipeline_node(node.value, local_names, imported_bindings)
        if name is None:
            return False
        return name in _PYTHON_PIPELINE_SAFE_ATTRIBUTES or _is_pure_module_attribute(name)
    if isinstance(node, ast.Call):
        return _is_safe_python_pipeline_call(node, local_names, imported_bindings)
    if isinstance(node, ast.Lambda | ast.FunctionDef):
        scoped_names = local_names | _argument_names(node.args)
        if not _is_safe_python_pipeline_node(node.args, local_names, imported_bindings):
            return False
        if isinstance(node, ast.Lambda):
            return _is_safe_python_pipeline_node(node.body, scoped_names, imported_bindings)
        return (
            all(
                _is_safe_python_pipeline_node(item, local_names, imported_bindings)
                for item in node.decorator_list
            )
            and (
                node.returns is None
                or _is_safe_python_pipeline_node(node.returns, local_names, imported_bindings)
            )
            and all(
                _is_safe_python_pipeline_node(child, scoped_names, imported_bindings)
                for child in node.body
            )
        )
    if isinstance(node, ast.comprehension) and node.is_async:
        return False
    if isinstance(node, ast.With):
        for item in node.items:
            if not (
                isinstance(item.context_expr, ast.Call)
                and _is_safe_python_pipeline_call(item.context_expr, local_names, imported_bindings)
            ):
                return False
            if item.optional_vars is not None and not (
                isinstance(item.optional_vars, ast.Name)
                and _is_safe_python_pipeline_node(
                    item.optional_vars, local_names, imported_bindings
                )
            ):
                return False
        return all(
            _is_safe_python_pipeline_node(child, local_names, imported_bindings)
            for child in node.body
        )
    return all(
        _is_safe_python_pipeline_node(child, local_names, imported_bindings)
        for child in ast.iter_child_nodes(node)
    )


def _literal_process_command(node: ast.Call) -> str | None:
    if not node.args:
        return None
    command = node.args[0]
    if isinstance(command, ast.Constant) and isinstance(command.value, str):
        return command.value.split(maxsplit=1)[0] if command.value.strip() else None
    if isinstance(command, ast.List | ast.Tuple) and command.elts:
        executable = command.elts[0]
        if isinstance(executable, ast.Constant) and isinstance(executable.value, str):
            return executable.value.rsplit("/", 1)[-1]
    return None


def _has_proven_python_mutation(
    tree: ast.AST,
    rebound_names: frozenset[str],
    imported_bindings: Mapping[str, str],
    path_value_names: frozenset[str],
) -> bool:
    """Return whether rejected source contains an evidenced mutation or escape."""
    if rebound_names & (_PYTHON_PIPELINE_RESERVED_NAMES | imported_bindings.keys()):
        return True

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if _has_dunder_name(node.id) or node.id in _PYTHON_REFLECTION_ESCAPE_NAMES:
                return True
            if node.id == "open":
                parent = parents.get(node)
                if not (isinstance(parent, ast.Call) and parent.func is node):
                    return True
        elif isinstance(node, ast.Attribute):
            attribute_name = _python_attribute_name(node)
            if _has_dunder_name(attribute_name or node.attr):
                return True
            if (
                node.attr in _PYTHON_REFLECTION_ESCAPE_NAMES
                and not _is_pure_module_attribute(attribute_name)
            ) or node.attr == "modules":
                return True
            if not isinstance(node.ctx, ast.Load):
                root = (attribute_name or "").partition(".")[0]
                if root in imported_bindings or root in _PYTHON_PIPELINE_RESERVED_NAMES:
                    return True
            if attribute_name:
                parts = attribute_name.split(".")
                if parts[0] in _PYTHON_PIPELINE_PURE_MODULES and any(
                    part in {"io", "sys"} for part in parts[1:]
                ):
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    alias.name in _PYTHON_PIPELINE_MODULES | _PYTHON_PIPELINE_PURE_MODULES
                    and alias.asname not in {None, alias.name}
                ):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" or alias.name.startswith("_") for alias in node.names):
                return True
        elif isinstance(node, ast.Call):
            call_name = _call_name(node, imported_bindings)
            if call_name in _PYTHON_FILESYSTEM_MUTATION_CALLS:
                return True
            if isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                path_receiver = (
                    isinstance(receiver, ast.Call)
                    and _call_name(receiver, imported_bindings) == "pathlib.Path"
                ) or (isinstance(receiver, ast.Name) and receiver.id in path_value_names)
                if node.func.attr in _PYTHON_FILESYSTEM_MUTATION_METHODS and (
                    node.func.attr != "replace" or path_receiver
                ):
                    return True
            if call_name == "open" or (call_name or "").endswith(".open"):
                if not _is_read_only_open_call(node):
                    return True
            if call_name == "zipfile.ZipFile" and not _is_read_only_zipfile_call(node):
                return True
            if call_name in _PYTHON_PROCESS_CALLS:
                command = _literal_process_command(node)
                if command in _PYTHON_MUTATING_COMMANDS:
                    return True
                if command == "git" and isinstance(node.args[0], ast.List | ast.Tuple):
                    values = [
                        item.value
                        for item in node.args[0].elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                    if any(
                        value in {"add", "apply", "checkout", "commit", "restore", "revert"}
                        for value in values[1:2]
                    ):
                        return True
    return False


def _classify_python_source(script: str) -> _PythonExecutionClassification:
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return _PythonExecutionClassification.INDETERMINATE
    rebound_names = _rebound_names(tree)
    imported_bindings = _imported_bindings(tree)
    path_value_names = _path_value_names(tree, imported_bindings)
    if _has_proven_python_mutation(
        tree,
        rebound_names,
        imported_bindings,
        path_value_names,
    ):
        return _PythonExecutionClassification.MUTATION
    local_names = rebound_names | imported_bindings.keys()
    safe = not (
        rebound_names & (_PYTHON_PIPELINE_RESERVED_NAMES | imported_bindings.keys())
    ) and _is_safe_python_pipeline_node(tree, local_names, imported_bindings)
    if safe:
        return _PythonExecutionClassification.READ_ONLY
    return _PythonExecutionClassification.INDETERMINATE


def _classify_python_pipeline(parts: list[str]) -> _PythonExecutionClassification:
    script = _python_inline_script(parts)
    if script is None:
        return _PythonExecutionClassification.INDETERMINATE
    return _classify_python_source(script)


def _is_read_only_python_pipeline(parts: list[str]) -> bool:
    return _classify_python_pipeline(parts) is _PythonExecutionClassification.READ_ONLY


def _is_read_only_python_source(script: str) -> bool:
    """Return True when a Python program provably performs no mutation."""
    return _classify_python_source(script) is _PythonExecutionClassification.READ_ONLY
