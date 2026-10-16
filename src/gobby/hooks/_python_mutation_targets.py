"""Which inline-Python calls mutate the filesystem, and the literal paths they name."""

import ast
from collections.abc import Mapping

_OPEN_READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})
# Module-level filesystem mutations, each with the positional arguments that
# name the paths it mutates (a copy's source is only read).
_PYTHON_FILESYSTEM_MUTATION_CALL_TARGETS: Mapping[str, tuple[int, ...]] = {
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.makedirs": (0,),
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.renames": (0, 1),
    "os.replace": (0, 1),
    "os.rmdir": (0,),
    "os.truncate": (0,),
    "os.unlink": (0,),
    "shutil.copy": (1,),
    "shutil.copy2": (1,),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (1,),
    "shutil.rmtree": (0,),
    "shutil.unpack_archive": (1,),
}
_ARCHIVE_MODULES = frozenset({"tarfile", "zipfile"})
_TARFILE_OPEN_CALLS = frozenset({"tarfile.open", "tarfile.TarFile", "tarfile.TarFile.open"})
# `r`, then `r:` (random access) or `r|` (stream) with an optional compression.
_TARFILE_READ_MODES = frozenset(
    {"r"} | {f"r{kind}{codec}" for kind in ":|" for codec in ("", "*", "gz", "bz2", "xz", "zst")}
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


def _imported_bindings(tree: ast.AST) -> Mapping[str, str] | None:
    """Map stable import names to canonical paths, or refuse conflicting identities."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                canonical_name = alias.name if alias.asname else bound_name
                if bindings.get(bound_name, canonical_name) != canonical_name:
                    return None
                bindings[bound_name] = canonical_name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound_name = alias.asname or alias.name
                canonical_name = f"{node.module}.{alias.name}"
                if bindings.get(bound_name, canonical_name) != canonical_name:
                    return None
                bindings[bound_name] = canonical_name
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


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_path_call(node: ast.Call) -> str | None:
    """Return the literal argument of a single-argument ``Path('...')`` call."""
    if len(node.args) != 1 or node.keywords:
        return None
    return _literal_string(node.args[0])


def _literal_path(
    node: ast.expr,
    imported_bindings: Mapping[str, str],
    path_value_names: Mapping[str, str | None],
) -> str | None:
    """Resolve a string literal, a literal ``Path(...)``, or a name bound to one."""
    if isinstance(node, ast.Name):
        return path_value_names.get(node.id)
    if isinstance(node, ast.Call) and _call_name(node, imported_bindings) == "pathlib.Path":
        return _literal_path_call(node)
    return _literal_string(node)


def _literal_path_arguments(
    node: ast.Call,
    indexes: tuple[int, ...],
    imported_bindings: Mapping[str, str],
    path_value_names: Mapping[str, str | None],
) -> tuple[str, ...]:
    """Return the literal paths at ``indexes``; empty when any of them is not literal."""
    literals: list[str] = []
    for index in indexes:
        if index >= len(node.args):
            return ()
        literal = _literal_path(node.args[index], imported_bindings, path_value_names)
        if literal is None:
            return ()
        literals.append(literal)
    return tuple(literals)


def _mutation_call_targets(
    node: ast.Call,
    imported_bindings: Mapping[str, str],
    path_value_names: Mapping[str, str | None],
) -> tuple[str, ...] | None:
    """Return the literal paths a call mutates.

    None means the call is not a proven mutation; an empty tuple means it is one
    whose target is not a string literal, so its scope stays unknown.
    """
    call_name = _call_name(node, imported_bindings)
    target_indexes = _PYTHON_FILESYSTEM_MUTATION_CALL_TARGETS.get(call_name or "")
    if target_indexes is not None:
        return _literal_path_arguments(node, target_indexes, imported_bindings, path_value_names)
    if isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        path_receiver = (
            isinstance(receiver, ast.Call)
            and _call_name(receiver, imported_bindings) == "pathlib.Path"
        ) or (isinstance(receiver, ast.Name) and receiver.id in path_value_names)
        receiver_literal = (
            _literal_path(receiver, imported_bindings, path_value_names) if path_receiver else None
        )
        if node.func.attr in _PYTHON_FILESYSTEM_MUTATION_METHODS and (
            node.func.attr != "replace" or path_receiver
        ):
            if receiver_literal is None:
                return ()
            if node.func.attr in {"rename", "replace"}:
                destination = _literal_path_arguments(
                    node, (0,), imported_bindings, path_value_names
                )
                return (receiver_literal, *destination) if destination else ()
            return (receiver_literal,)
        if (
            node.func.attr == "open"
            and path_receiver
            and not _is_read_only_open_call(node, mode_index=0)
        ):
            # `Path(...).open('w')` writes the receiver; a read mode falls through.
            return (receiver_literal,) if receiver_literal is not None else ()
        # `extract` is a common name, so it counts only beside an archive import.
        if node.func.attr == "extractall" or (
            node.func.attr == "extract"
            and any(
                name.partition(".")[0] in _ARCHIVE_MODULES for name in imported_bindings.values()
            )
        ):
            return _extraction_destination(node, imported_bindings, path_value_names)
    if call_name in _TARFILE_OPEN_CALLS:
        if _is_read_only_open_call(node, modes=_TARFILE_READ_MODES):
            return None
        return _literal_path_arguments(node, (0,), imported_bindings, path_value_names)
    if call_name == "open" or (call_name or "").endswith(".open"):
        if _is_read_only_open_call(node):
            return None
        if call_name == "open":
            return _literal_path_arguments(node, (0,), imported_bindings, path_value_names)
        return ()
    if call_name == "zipfile.ZipFile" and not _is_read_only_zipfile_call(node):
        return _literal_path_arguments(node, (0,), imported_bindings, path_value_names)
    if call_name in _PYTHON_PROCESS_CALLS:
        command = _literal_process_command(node)
        if command in _PYTHON_MUTATING_COMMANDS:
            return ()
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
                return ()
    return None


def _is_read_only_open_call(
    node: ast.Call, *, mode_index: int = 1, modes: frozenset[str] = _OPEN_READ_MODES
) -> bool:
    """Allow ``open(...)`` only when the mode is absent or a literal read mode.

    ``mode_index`` is 1 for the builtin (``open(path, mode)``) and 0 for the
    ``Path.open(mode)`` method form.
    """
    mode: ast.expr | None = None
    if len(node.args) > mode_index:
        mode = node.args[mode_index]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return True
    return isinstance(mode, ast.Constant) and mode.value in modes


def _extraction_destination(
    node: ast.Call,
    imported_bindings: Mapping[str, str],
    path_value_names: Mapping[str, str | None],
) -> tuple[str, ...]:
    """Return an archive extraction's literal destination; empty when it is not literal.

    ``extractall(path)`` and ``extract(member, path)`` default to the working
    directory, which is not a literal target either.
    """
    index = 0 if isinstance(node.func, ast.Attribute) and node.func.attr == "extractall" else 1
    destination = node.args[index] if len(node.args) > index else None
    for keyword in node.keywords:
        if keyword.arg == "path":
            destination = keyword.value
    literal = (
        _literal_path(destination, imported_bindings, path_value_names) if destination else None
    )
    return (literal,) if literal is not None else ()


def _is_read_only_zipfile_call(node: ast.Call) -> bool:
    """Allow ``ZipFile(...)`` only when its mode is absent or literal ``r``."""
    mode: ast.expr | None = node.args[1] if len(node.args) >= 2 else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    return mode is None or (isinstance(mode, ast.Constant) and mode.value == "r")


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
