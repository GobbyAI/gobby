"""Fail-closed pytest reuse classification, without importing candidate code.

The caller supplies candidate blobs, including its import closure. This module
does no filesystem, subprocess, plugin discovery or candidate-code execution.
Fixture providers are resolved in pytest precedence order. Only executed units
of conftests/plugins are inspected; ordinary test helpers are scanned whole.
"""

import ast
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

type Sources = Mapping[str, str | bytes]
type Function = ast.FunctionDef | ast.AsyncFunctionDef

_AMBIGUOUS_IMPORT = "<ambiguous-import>"


@dataclass(frozen=True)
class Disposition:
    path: str
    symbol: str
    kind: str
    pin: str


@dataclass(frozen=True)
class Classification:
    reasons: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.reasons


# Fixed providers, never inferred from whatever plugins happen to be installed
# in the closing process. cache deliberately has no bounded provider.
BUILTIN_FIXTURES = {
    **dict.fromkeys(
        (
            "request",
            "pytestconfig",
            "record_property",
            "record_testsuite_property",
            "record_xml_attribute",
            "capfd",
            "capfdbinary",
            "capsys",
            "capsysbinary",
            "caplog",
            "monkeypatch",
            "tmp_path",
            "tmp_path_factory",
            "tmpdir",
            "tmpdir_factory",
            "doctest_namespace",
            "recwarn",
            "capsysdisabled",
        ),
        "pytest",
    ),
    **dict.fromkeys(
        ("mocker", "class_mocker", "module_mocker", "package_mocker", "session_mocker"),
        "pytest-mock",
    ),
    **dict.fromkeys(("worker_id", "testrun_uid"), "pytest-xdist"),
    **dict.fromkeys(("cov", "no_cover"), "pytest-cov"),
    **dict.fromkeys(
        (
            "event_loop_policy",
            "unused_tcp_port",
            "unused_tcp_port_factory",
            "unused_udp_port",
            "unused_udp_port_factory",
        ),
        "pytest-asyncio",
    ),
    **dict.fromkeys(
        (
            "anyio_backend",
            "anyio_backend_name",
            "anyio_backend_options",
            "free_tcp_port",
            "free_tcp_port_factory",
            "free_udp_port",
            "free_udp_port_factory",
        ),
        "anyio",
    ),
}


def _text(sources: Sources, path: str) -> str:
    value = sources[path]
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _dotted(node: ast.AST, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value, aliases)}.{node.attr}"
    if isinstance(node, ast.Call):
        loader = _dotted(node.func, aliases)
        if loader in {"__import__", "importlib.import_module"}:
            name = (
                node.args[0]
                if node.args
                else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "name"), None
                )
            )
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                # __import__ returns the top-level package unless fromlist is
                # supplied; retain either namespace without executing the call.
                if loader == "__import__":
                    fromlist = (
                        node.args[3]
                        if len(node.args) > 3
                        else next(
                            (
                                keyword.value
                                for keyword in node.keywords
                                if keyword.arg == "fromlist"
                            ),
                            ast.Constant(None),
                        )
                    )
                    try:
                        if not ast.literal_eval(fromlist):
                            return name.value.split(".")[0]
                    except (ValueError, TypeError):
                        return _AMBIGUOUS_IMPORT
                return name.value
    return ""


def _module_name(path: str) -> str:
    path = path.removeprefix("src/").removesuffix(".py")
    return path.removesuffix("/__init__").replace("/", ".")


def _import_names(node: ast.AST, path: str) -> set[str]:
    """Resolve relative imports without importing their modules."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            names.update(alias.name for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            parts = _module_name(path).split(".")
            if not path.endswith("/__init__.py"):
                parts.pop()
            if child.level:
                parts = parts[: len(parts) - child.level + 1]
                base = ".".join((*parts, child.module or "")).strip(".")
            else:
                base = child.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in child.names)
    return names


def _repo_imports(
    node: ast.AST, path: str, modules: Mapping[str, str], aliases: Mapping[str, str]
) -> set[str]:
    paths: set[str] = set()
    names = _import_names(node, path)
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _dotted(child.func, aliases) in {
            "importlib.import_module",
            "__import__",
        }:
            if child.args and isinstance(child.args[0], ast.Constant):
                value = child.args[0].value
                if isinstance(value, str):
                    names.add(value)
    for name in names:
        parts = name.split(".")
        for end in range(1, len(parts) + 1):
            found = modules.get(".".join(parts[:end]))
            if found:
                paths.add(found)
    return paths


def _aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}

    def bind(name: str, target: str) -> None:
        # A file-wide map cannot prove which lexical binding a reference uses.
        # Preserve ambiguity instead of letting an unrelated local import hide
        # an unsafe module import. Import statements alone remain harmless.
        previous = aliases.get(name, target)
        aliases[name] = target if previous == target else _AMBIGUOUS_IMPORT

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind(
                    alias.asname or alias.name.split(".")[0],
                    alias.name if alias.asname else alias.name.split(".")[0],
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bind(alias.asname or alias.name, f"{node.module}.{alias.name}")
    return aliases


def _literal_strings(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tuple(part.strip() for part in node.value.split(",") if part.strip())
    if isinstance(node, (ast.Tuple, ast.List)):
        if all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
        ):
            return tuple(str(item.value) for item in node.elts if isinstance(item, ast.Constant))
    return None


def _fixture(node: Function, aliases: Mapping[str, str]) -> tuple[str, bool] | None:
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        name = _dotted(target, aliases)
        if name.startswith(_AMBIGUOUS_IMPORT):
            raise ValueError("ambiguous fixture decorator import")
        if name not in {"pytest.fixture", "pytest_asyncio.fixture"}:
            continue
        name, autouse = node.name, False
        if call:
            for keyword in call.keywords:
                if keyword.arg is None:
                    raise ValueError("unresolved fixture keyword unpacking")
                if keyword.arg == "name":
                    value = ast.literal_eval(keyword.value)
                    if not isinstance(value, str):
                        raise ValueError("non-literal fixture name")
                    name = value
                if keyword.arg == "autouse":
                    value = ast.literal_eval(keyword.value)
                    if not isinstance(value, bool):
                        raise ValueError("non-literal autouse")
                    autouse = value
        return name, autouse
    return None


@dataclass
class _File:
    path: str
    source: str
    tree: ast.Module
    aliases: dict[str, str]
    functions: dict[str, Function]
    fixtures: dict[str, Function]
    autouse: set[str]

    @classmethod
    def parse(cls, path: str, source: str) -> "_File":
        tree = ast.parse(source, filename=path)
        aliases = _aliases(tree)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        fixtures, autouse = {}, set()
        for node in functions.values():
            fixture = _fixture(node, aliases)
            if fixture:
                name, automatic = fixture
                fixtures[name] = node
                if automatic:
                    autouse.add(name)
        return cls(path, source, tree, aliases, functions, fixtures, autouse)

    def unit(self, symbol: str) -> tuple[ast.stmt, ...]:
        if symbol == "<module>":
            initial: list[ast.stmt] = [
                node
                for node in self.tree.body
                if not isinstance(
                    node,
                    (
                        ast.Import,
                        ast.ImportFrom,
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.ClassDef,
                    ),
                )
            ]
        else:
            initial = [self.functions[symbol]]
        seen: set[str] = {symbol}
        # Helpers called by name belong to the calling unit, including helpers
        # called from another helper. Recursion terminates at the symbol set.
        for root in initial:
            for node in ast.walk(root):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name in self.functions and name not in seen:
                        seen.add(name)
                        initial.append(self.functions[name])
        return tuple(initial)


def disposition_pin(disposition: Disposition, sources: Sources) -> str:
    """Hash complete unit segments and exact bytes of its repository imports.

    Missing units/import blobs are errors; callers convert these to stale pins.
    This function computes a proposed pin but never updates the reviewed table.
    """
    file = _File.parse(disposition.path, _text(sources, disposition.path))
    modules = {_module_name(path): path for path in sources if path.endswith(".py")}
    digest = hashlib.sha256()

    def include(label: str, value: str) -> None:
        for field in (label.encode(), value.encode()):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)

    imports: set[str] = set()
    for node in file.unit(disposition.symbol):
        start = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            start = min(start, *(decorator.lineno for decorator in node.decorator_list))
        segment = "\n".join(file.source.splitlines()[start - 1 : node.end_lineno])
        include(f"{file.path}::{getattr(node, 'name', '<module>')}", segment)
        imports.update(_repo_imports(node, file.path, modules, file.aliases))
    for path in sorted(imports):
        include(path, _text(sources, path))
    return digest.hexdigest()


# Each entry is a reviewed safety boundary, not a blanket conftest exemption.
# Literal pins are populated after reviewing the root chain at the task's base.
REVIEWED_DISPOSITIONS: tuple[Disposition, ...] = (
    # Operator-home comparisons and isolated temporary bootstrap provisioning.
    Disposition(
        "tests/conftest.py",
        "<module>",
        "isolation",
        "6195344a82aad347cd9db3c4a8a8ebae66968eb0fdca1213367da2f8c87b6d6a",
    ),
    Disposition(
        "tests/conftest.py",
        "pytest_configure",
        "isolation",
        "87b08c998a4241c876f24a9bcfca0c498454dcda77b7a21cc5d8ee66d340aa76",
    ),
    # These repository literals compare collected paths, not open new inputs.
    Disposition(
        "tests/conftest.py",
        "pytest_collection_modifyitems",
        "isolation",
        "4e3a843be6e1a0d879f8cbec83064b9b8bba6e3027fabf5a360eced0ee515948",
    ),
    # The bounded run's record fingerprints the installed native binary.
    Disposition(
        "tests/conftest.py",
        "_select_schema_contract_gdaemon",
        "environment:gdaemon_identity",
        "00223ecd0d20dd7329ae7153da6dd8b92bdac2c7ad334c1bcc1c6b918bc0778f",
    ),
    # Redirects writes to temporary fixture homes; expanduser is an isolation guard.
    Disposition(
        "tests/conftest.py",
        "protect_production_resources",
        "isolation",
        "2575ac7a504abeca51dd919a2dea6b2e5c5b6e2d8ad0134b5a7930d0745380cd",
    ),
    # Includes cleanup_run -> _stop_runner and exact lazy E2E helper bytes. This
    # hook drains only processes bearing this run's token and isolated home.
    # Re-reviewed at integration 0d6cb79c10: E2E readiness diagnostics changed;
    # the imported cleanup helpers and run ownership guards remain unchanged.
    Disposition(
        "tests/fixtures/e2e_run_cleanup.py",
        "pytest_sessionfinish",
        "isolation",
        "f155e43c74f5c8fbf5f5937af17416bdf78459833c997063df4e05e1fb0cecec",
    ),
)


def _sites(nodes: Sequence[ast.AST], aliases: Mapping[str, str]) -> tuple[str, ...]:
    sites: set[str] = set()
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, (ast.Name, ast.Attribute, ast.Call)):
                name = _dotted(node, aliases)
                if (
                    name.startswith(_AMBIGUOUS_IMPORT)
                    or name.split(".")[0] in {"subprocess", "pty", "runpy"}
                    or name == "__file__"
                    or name
                    in {
                        "pathlib.Path.cwd",
                        "pathlib.Path.home",
                        "os.getcwd",
                        "os.system",
                        "os.popen",
                        "os.path.expanduser",
                        "importlib.resources.files",
                        "importlib.resources.as_file",
                    }
                    or name.startswith(("os.exec", "os.spawn", "asyncio.create_subprocess_"))
                    or isinstance(node, ast.Attribute)
                    and node.attr == "expanduser"
                ):
                    sites.add(f"unbounded_input:{node.lineno}:{name or 'expanduser'}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(("src/", "tests/", "crates/", "web/", ".gobby/")):
                    sites.add(f"repository_literal:{node.lineno}:{node.value}")
            if isinstance(node, ast.Call):
                name = _dotted(node.func, aliases)
                if name in {
                    "importlib.import_module",
                    "__import__",
                    "pkgutil.iter_modules",
                    "pkgutil.walk_packages",
                    "importlib.metadata.entry_points",
                }:
                    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
                    if (
                        not arguments
                        and name != "importlib.metadata.entry_points"
                        or any(not isinstance(argument, ast.Constant) for argument in arguments)
                    ):
                        sites.add(f"dynamic_import:{node.lineno}:{name}")
    return tuple(sorted(sites))


def _requests(
    nodes: Sequence[ast.AST], aliases: Mapping[str, str], *, parameters: bool
) -> tuple[set[str], set[str]]:
    requests: set[str] = set()
    errors: set[str] = set()
    for root in nodes:
        if parameters and isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)):
            direct: set[str] = set()
            for decorator in root.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or _dotted(decorator.func, aliases) != "pytest.mark.parametrize"
                ):
                    continue
                names = _literal_strings(decorator.args[0]) if decorator.args else None
                if names is None:
                    errors.add("dynamic_parametrize")
                    continue
                indirect: ast.AST = ast.Constant(False)
                for keyword in decorator.keywords:
                    if keyword.arg == "indirect":
                        indirect = keyword.value
                if isinstance(indirect, ast.Constant) and indirect.value is False:
                    direct.update(names)
                elif isinstance(indirect, ast.Constant) and indirect.value is True:
                    pass
                else:
                    indirect_names = _literal_strings(indirect)
                    if indirect_names is None or isinstance(indirect, ast.Constant):
                        errors.add("dynamic_indirect")
                    else:
                        direct.update(set(names) - set(indirect_names))
            requests.update(
                arg.arg
                for arg in (*root.args.posonlyargs, *root.args.args, *root.args.kwonlyargs)
                if arg.arg not in direct | {"self", "cls"}
            )
        for node in ast.walk(root):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func, aliases)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "getfixturevalue":
                names = _literal_strings(node.args[0]) if node.args else None
                if names is None or len(names) != 1:
                    errors.add("dynamic_fixture")
                else:
                    requests.update(names)
            elif name == "pytest.mark.usefixtures":
                for arg in node.args:
                    names = _literal_strings(arg)
                    if names is None:
                        errors.add("dynamic_usefixtures")
                    else:
                        requests.update(names)
    return requests, errors


def classify_module(
    module: str,
    sources: Sources,
    *,
    closure: Sequence[str] = (),
    dispositions: Sequence[Disposition] = REVIEWED_DISPOSITIONS,
) -> Classification:
    """Return every refusal reason; an empty tuple means reuse is eligible.

    ``closure`` contains non-plugin test helpers found by the caller's candidate
    import graph. Direct/nested repository imports are also followed here.
    Unknown fixtures and malformed or missing candidate sources fail closed.
    """
    reasons: set[str] = set()
    files: dict[str, _File] = {}
    modules = {_module_name(path): path for path in sources if path.endswith(".py")}
    exempt = {(entry.path, entry.symbol) for entry in dispositions}
    for entry in dispositions:
        try:
            valid = entry.kind == "isolation" or entry.kind.startswith("environment:")
            valid = valid and disposition_pin(entry, sources) == entry.pin
        except (KeyError, SyntaxError, ValueError, UnicodeError, TypeError):
            valid = False
        if not valid:
            reasons.add(f"infrastructure_pin_stale:{entry.path}::{entry.symbol}")

    def load(path: str) -> _File | None:
        if path in files:
            return files[path]
        if path not in sources:
            reasons.add(f"missing_source:{path}")
            return None
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            reasons.add(f"invalid_path:{path}")
            return None
        try:
            files[path] = _File.parse(path, _text(sources, path))
        except (SyntaxError, UnicodeError, ValueError, TypeError) as error:
            reasons.add(f"invalid_source:{path}:{type(error).__name__}")
            return None
        return files[path]

    main = load(module)
    if main is None:
        return Classification(tuple(sorted(reasons)))
    chain: list[_File] = []
    for parent in reversed(PurePosixPath(module).parents):
        path = str(parent / "conftest.py")
        if path in sources:
            file = load(path)
            if file:
                chain.append(file)
    plugins: list[_File] = []
    plugin_queue = [main, *chain]
    for file in plugin_queue:
        for node in file.tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(
                isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets
            ):
                continue
            names = _literal_strings(node.value) if node.value else None
            if names is None:
                reasons.add(f"dynamic_plugins:{file.path}")
                continue
            for name in names:
                plugin_path = modules.get(name)
                if plugin_path is None:
                    reasons.add(f"missing_plugin:{name}")
                elif all(plugin.path != plugin_path for plugin in plugins):
                    plugin = load(plugin_path)
                    if plugin:
                        plugins.append(plugin)
                        plugin_queue.append(plugin)
    providers = [main, *reversed(chain), *plugins]
    infrastructure = {file.path for file in (*chain, *plugins)}
    helpers_seen: set[str] = set()

    def helper(path: str) -> None:
        if path in helpers_seen or path in infrastructure or not path.startswith("tests/"):
            return
        helpers_seen.add(path)
        file = load(path)
        if file:
            scan(file, "<whole>", (file.tree,))
            requests, errors = _requests((file.tree,), file.aliases, parameters=False)
            reasons.update(f"{file.path}::<whole>:{error}" for error in errors)
            for name in sorted(requests):
                request(name)

    def scan(file: _File, symbol: str, nodes: Sequence[ast.AST]) -> None:
        if (file.path, symbol) in exempt:
            return
        for site in _sites(nodes, file.aliases):
            reasons.add(f"{file.path}::{symbol}:{site}")
        for node in nodes:
            for imported in _repo_imports(node, file.path, modules, file.aliases):
                helper(imported)

    resolved: set[tuple[str, str]] = set()
    resolving: set[tuple[str, str]] = set()

    def request(name: str, trail: tuple[str, ...] = (), after: str | None = None) -> None:
        label = "->".join((*trail, name))
        start = next((i + 1 for i, file in enumerate(providers) if file.path == after), 0)
        owner = next((file for file in providers[start:] if name in file.fixtures), None)
        if owner is None:
            if name == "cache":
                reasons.add(f"unbounded_fixture:cache:{label}")
            elif name not in BUILTIN_FIXTURES:
                reasons.add(f"unresolved_fixture:{name}:{label}")
            return
        key = owner.path, name
        if key in resolving:
            reasons.add(f"fixture_cycle:{label}")
            return
        if key in resolved:
            return
        resolving.add(key)
        function = owner.fixtures[name]
        nodes = owner.unit(function.name)
        symbol = f"{function.name}[fixture:{label}]"
        # Use the real symbol for reviewed-unit lookup, retaining the fixture
        # request trail on diagnostics (not just an unresolved root name).
        if (owner.path, function.name) not in exempt:
            scan(owner, symbol, nodes)
        dependencies, errors = _requests(nodes, owner.aliases, parameters=False)
        parameters, parameter_errors = _requests((function,), owner.aliases, parameters=True)
        dependencies.update(parameters)
        for error in errors | parameter_errors:
            reasons.add(f"{owner.path}::{symbol}:{error}")
        for dependency in sorted(dependencies):
            request(dependency, (*trail, name), owner.path if dependency == name else None)
        resolving.remove(key)
        resolved.add(key)

    helpers_seen.add(module)
    scan(main, "<whole>", (main.tree,))
    for path in closure:
        helper(path)
    for file in (*chain, *plugins):
        for symbol in (
            "<module>",
            *(name for name in file.functions if name.startswith("pytest_")),
        ):
            nodes = file.unit(symbol)
            scan(file, symbol, nodes)
            requests, errors = _requests(nodes, file.aliases, parameters=False)
            reasons.update(f"{file.path}::{symbol}:{error}" for error in errors)
            for name in sorted(requests):
                request(name)
    roots, errors = _requests((main.tree,), main.aliases, parameters=False)
    reasons.update(f"{module}:{error}" for error in errors)
    for candidate in ast.walk(main.tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            candidate.name.startswith("test_") or _fixture(candidate, main.aliases)
        ):
            requests, errors = _requests((candidate,), main.aliases, parameters=True)
            roots.update(requests)
            reasons.update(f"{module}::{candidate.name}:{error}" for error in errors)
    for name in sorted(roots | set().union(*(file.autouse for file in providers))):
        request(name)
    return Classification(tuple(sorted(reasons)))
