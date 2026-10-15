"""Committed Rust module identities for acceptance-test transcript matching."""

from __future__ import annotations

import posixpath
import re
import shlex
import tomllib
from pathlib import PurePosixPath

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git

# Consume comments and literals before looking for real module declarations.
_MODULE_TOKEN_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|r(?P<hashes>\#*)".*?"(?P=hashes)|"(?:\\.|[^"\\])*"'
    r"|'(?:\\(?:u\{[0-9A-Fa-f]+\}|.)|[^'\\])'"
    r'|(?:#\s*\[\s*path\s*=\s*"(?P<path>[^"\n]+)"\s*\]'
    r"\s*(?:#\s*\[[^\]]*\]\s*)*)?(?:pub(?:\([^)]*\))?\s+)?"
    r"\bmod\s+(?P<module>[A-Za-z_]\w*)\s*(?P<delimiter>[{;])"
    r"|\bfn\s+(?P<function>[A-Za-z_]\w*)|(?P<brace>[{}])",
    re.DOTALL,
)


def rust_inline_test_module(source: str, symbol: str) -> tuple[str, ...]:
    """Find the enclosing inline modules of the uniquely resolved test function."""
    name = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    stack: list[str | None] = []
    for match in _MODULE_TOKEN_RE.finditer(source):
        if match.group("function") == name:
            return tuple(part for part in stack if part is not None)
        if match.group("module") is not None and match.group("delimiter") == "{":
            stack.append(match.group("module"))
        elif match.group("brace") == "{":
            stack.append(None)
        elif match.group("brace") == "}" and stack:
            stack.pop()
    return ()


type RustModuleDeclarations = dict[str, set[tuple[str, tuple[str, ...]]]]
type RustModuleCache = dict[tuple[str, str], RustModuleDeclarations]


def rust_source_module(path: str) -> tuple[str, ...]:
    """Derive the ordinary Rust module path, excluding crate source roots."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) >= 3 and parts[0] == "crates":
        parts = parts[2:]
    if parts and parts[0] in {"src", "tests"}:
        parts = parts[1:]
    if parts and parts[-1] in {"lib", "main", "mod"}:
        parts.pop()
    return tuple(parts)


async def _read_declarations(commit_sha: str, root: str, repo_path: str) -> RustModuleDeclarations:
    """Read only files containing path attributes from the close candidate."""
    result = await daemon_git.run(
        (
            "grep",
            "-l",
            "-E",
            r"#[[:space:]]*\[[[:space:]]*path([[:space:]]|=|$)",
            commit_sha,
            "--",
            f"{root}/*.rs",
        ),
        cwd=repo_path,
        timeout=30,
    )
    if isinstance(result, GitFailed) and result.returncode == 1:
        return {}
    if not isinstance(result, GitOk):
        raise RuntimeError("could not read committed Rust module declarations")
    declarations: RustModuleDeclarations = {}
    for line in result.stdout.splitlines():
        declaring_path = line.removeprefix(f"{commit_sha}:")
        source = await daemon_git.run(
            ("show", f"{commit_sha}:{declaring_path}"), cwd=repo_path, timeout=30
        )
        if not isinstance(source, GitOk):
            raise RuntimeError(f"could not read committed Rust module {declaring_path}")
        _collect_declarations(declarations, declaring_path, source.stdout)
    return declarations


def _collect_declarations(
    declarations: RustModuleDeclarations, declaring_path: str, source: str
) -> None:
    """Track inline names and the Rust path-attribute directory rules."""
    owner = PurePosixPath(declaring_path)
    inline_root = owner.parent if owner.stem in {"lib", "main", "mod"} else owner.with_suffix("")
    stack: list[tuple[str | None, PurePosixPath]] = []
    for match in _MODULE_TOKEN_RE.finditer(source):
        name = match.group("module")
        path = match.group("path")
        directory = stack[-1][1] if stack else owner.parent
        if name is not None:
            if match.group("delimiter") == "{":
                if path is None:
                    has_inline_module = any(part is not None for part, _ in stack)
                    directory = (directory if has_inline_module else inline_root) / name
                else:
                    directory = directory / path
                stack.append((name, directory))
            elif path is not None:
                target = posixpath.normpath(str(directory / path))
                inline_names = tuple(part for part, _ in stack if part is not None)
                declarations.setdefault(target, set()).add((declaring_path, (*inline_names, name)))
        elif match.group("brace") == "{":
            stack.append((None, directory))
        elif match.group("brace") == "}" and stack:
            stack.pop()


async def resolve_rust_module(
    path: str, commit_sha: str, repo_path: str, *, cache: RustModuleCache | None = None
) -> tuple[str, ...]:
    """Share committed declarations per (candidate, crate) within one resolution."""
    parts = PurePosixPath(path).parts
    root = str(PurePosixPath(*parts[:2])) if parts[:1] == ("crates",) else "."
    key = (commit_sha, root)
    if cache is None:
        cache = {}
    if key not in cache:
        cache[key] = await _read_declarations(commit_sha, root, repo_path)
    declarations = cache[key]

    def module_for(source_path: str, seen: frozenset[str]) -> tuple[str, ...]:
        if source_path in seen:
            raise ValueError(f"cyclic Rust path module: {source_path}")
        if source_path not in declarations:
            if PurePosixPath(source_path).parent == PurePosixPath(root) / "tests":
                return ()
            return rust_source_module(source_path)
        if len(declarations[source_path]) != 1:
            raise ValueError(f"ambiguous Rust path module: {source_path}")
        owner, suffix = next(iter(declarations[source_path]))
        owner_module = (
            ()
            if PurePosixPath(owner).parent == PurePosixPath(root) / "tests"
            else module_for(owner, seen | {source_path})
        )
        return (*owner_module, *suffix)

    return module_for(path, frozenset())


async def resolve_rust_target(
    path: str, commit_sha: str, repo_path: str, *, cache: dict[str, str]
) -> tuple[str, str | None] | None:
    """Resolve crate-root test identity from the committed Cargo manifest."""
    parts = PurePosixPath(path).parts
    root = PurePosixPath(*parts[:2]) if parts[:1] == ("crates",) else PurePosixPath(".")
    relative = str(PurePosixPath(path).relative_to(root))
    if relative != "src/lib.rs" and PurePosixPath(relative).parent != PurePosixPath("tests"):
        return None
    manifest_path = str(root / "Cargo.toml")
    if manifest_path not in cache:
        result = await daemon_git.run(
            ("show", f"{commit_sha}:{manifest_path}"), cwd=repo_path, timeout=30
        )
        cache[manifest_path] = result.stdout if isinstance(result, GitOk) else ""
    try:
        manifest = tomllib.loads(cache[manifest_path])
    except tomllib.TOMLDecodeError:
        return None
    package = manifest.get("package", {})
    if not isinstance(package, dict) or not isinstance(name := package.get("name"), str):
        return None
    if relative == "src/lib.rs":
        library = manifest.get("lib", {})
        return (
            (name, None)
            if isinstance(library, dict) and library.get("path", relative) == relative
            else None
        )
    targets = manifest.get("test", [])
    if not isinstance(targets, list):
        return None
    explicit = [
        target.get("name")
        for target in targets
        if isinstance(target, dict)
        and target.get("path", f"tests/{target.get('name')}.rs") == relative
    ]
    if explicit:
        return (name, explicit[0]) if len(explicit) == 1 and isinstance(explicit[0], str) else None
    if any(
        isinstance(target, dict) and target.get("name") == PurePosixPath(path).stem
        for target in targets
    ):
        return None
    if package.get("autotests") is False:
        return None
    return name, PurePosixPath(path).stem


def rust_test_needs_target(path: str, module: tuple[str, ...] | None) -> bool:
    """Identify resolved roots whose names require Cargo target attribution."""
    parts = PurePosixPath(path).parts
    parts = parts[2:] if parts[:1] == ("crates",) else parts
    return (
        PurePosixPath(path).suffix == ".rs"
        and module is not None
        and (not module or (len(parts) == 2 and parts[0] == "tests"))
    )


def _cargo_selects_test(
    command: str,
    target: tuple[str, str | None] | None,
    name: str,
    manifest_path: str | None = None,
) -> bool:
    """Bind an executed libtest row to unambiguous Cargo and libtest selectors."""
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    while args and re.match(r"^[A-Za-z_]\w*=", args[0]):
        args.pop(0)
    if (
        target is None
        and args
        and (PurePosixPath(args[0]).name != "cargo" or args[1:2] == ["nextest"])
    ):
        return True
    if len(args) < 2 or PurePosixPath(args[0]).name != "cargo" or args[1] != "test":
        return False
    separator = args.index("--") if "--" in args else len(args)
    cargo_args, libtest_args = args[2:separator], args[separator + 1 :]
    packages: list[str] = []
    manifests: list[str] = []
    binaries: list[str] = []
    filters: list[str] = []
    skips: list[str] = []
    library = False
    exact = False
    cargo_values = {
        "--target",
        "--manifest-path",
        "--target-dir",
        "--features",
        "-F",
        "--profile",
        "--jobs",
        "-j",
        "--message-format",
        "--config",
        "--color",
    }
    cargo_flags = {
        "--release",
        "--locked",
        "--offline",
        "--frozen",
        "--no-default-features",
        "--all-features",
        "--quiet",
        "-q",
        "--verbose",
        "-v",
        "--no-fail-fast",
        "--workspace",
        "--all-targets",
        "--tests",
    }
    libtest_values = {"--skip", "--test-threads", "--logfile", "--format", "--color"}
    libtest_flags = {
        "--nocapture",
        "--no-capture",
        "--show-output",
        "--include-ignored",
        "--ignored",
        "--quiet",
        "-q",
        "--exact",
    }
    for tokens, is_cargo in ((cargo_args, True), (libtest_args, False)):
        index = 0
        while index < len(tokens):
            argument = tokens[index]
            option, equals, value = argument.partition("=")
            value_options = (
                cargo_values | {"-p", "--package", "--test"} if is_cargo else libtest_values
            )
            if option in value_options:
                if not equals:
                    index += 1
                    if index == len(tokens) or tokens[index].startswith("-"):
                        return False
                    value = tokens[index]
                if not value:
                    return False
                if is_cargo and option in {"-p", "--package"}:
                    packages.append(value)
                elif is_cargo and option == "--manifest-path":
                    manifests.append(str(PurePosixPath(value)))
                elif is_cargo and option == "--test":
                    binaries.append(value)
                elif not is_cargo and option == "--skip":
                    skips.append(value)
            elif is_cargo and argument == "--lib":
                library = True
            elif argument in (cargo_flags if is_cargo else libtest_flags):
                exact = exact or (not is_cargo and argument == "--exact")
            elif argument.startswith("-"):
                return False
            else:
                filters.append(argument)
            index += 1
    if len(binaries) > 1 or len(filters) > 1:
        return False
    if target is not None:
        package, binary = target
        if not (packages == [package] or (not packages and manifests == [manifest_path])) or any(
            flag in cargo_args for flag in ("--workspace", "--all-targets", "--tests")
        ):
            return False
        if library and binaries:
            return False
        if (binary is None and not library) or (binary is not None and binaries != [binary]):
            return False
    return (not filters or (name == filters[0] if exact else filters[0] in name)) and not any(
        name == skip if exact else skip in name for skip in skips
    )


def _root_evidence_names_test(
    output: str,
    command: str | None,
    target: tuple[str, str | None] | None,
    name: str,
    manifest_path: str,
) -> bool:
    """Require an exact executed row; command tokens only establish target context."""
    if target is None:
        return False
    if command is not None and not _cargo_selects_test(command, None, name):
        return False
    package, binary = target
    label = f"{package}::{binary}" if binary is not None else package
    nextest = re.compile(
        rf"^\s*(?:PASS|FAIL)\s+\[[^]]+\]\s+(?:\(\s*\d+/\d+\)\s+)?"
        rf"{re.escape(label)}\s+{re.escape(name)}(?:\s|$)"
    )
    if any(nextest.search(line) for line in output.splitlines()):
        return True
    libtest = re.compile(rf"^\s*test {re.escape(name)} \.\.\. (?:ok|FAILED)\s*$")
    return bool(
        command is not None
        and _cargo_selects_test(command, target, name, manifest_path)
        and any(libtest.fullmatch(line) for line in output.splitlines())
    )


def rust_evidence_names_test(
    evidence: str,
    path: str,
    symbol: str,
    rust_module: tuple[str, ...] | None,
    rust_target: tuple[str, str | None] | None = None,
    core_command: str | None = None,
) -> bool:
    """Return whether one evidence line names a Rust test under its source module."""
    if PurePosixPath(path).suffix != ".rs":
        return False
    if rust_test_needs_target(path, rust_module):
        name = "::".join((*(rust_module or ()), symbol))
        parts = PurePosixPath(path).parts
        root = PurePosixPath(*parts[:2]) if parts[:1] == ("crates",) else PurePosixPath(".")
        return _root_evidence_names_test(
            evidence, core_command, rust_target, name, str(root / "Cargo.toml")
        )

    path_parts = rust_module if rust_module is not None else rust_source_module(path)
    source_parts = PurePosixPath(path).parts
    source_parts = source_parts[2:] if source_parts[:1] == ("crates",) else source_parts
    is_integration_test = bool(source_parts and source_parts[0] == "tests")
    if not path_parts and rust_module is None:
        return False

    module_prefix = "::".join(re.escape(part) for part in path_parts)
    escaped_symbol = re.escape(symbol)
    if rust_module is not None:
        test_name = f"{module_prefix}::{escaped_symbol}" if module_prefix else escaped_symbol
        module_test = re.compile(test_name)
    else:
        module_test = re.compile(rf"{module_prefix}(?:::[A-Za-z0-9_]+)*::{escaped_symbol}")
    for line in evidence.splitlines():
        row = re.fullmatch(r"\s*test (\S+) \.\.\. (?:ok|FAILED)\s*", line)
        nextest = re.fullmatch(
            r"\s*(?:PASS|FAIL)\s+(?:\[[^]]+\]\s+)?(?:\(\s*\d+/\d+\)\s+)?(\S+)\s+(\S+)\s*",
            line,
        )
        row_name = row[1] if row else nextest[2] if nextest else None
        if row_name is None:
            continue
        matches = module_test.fullmatch(row_name) is not None or (
            is_integration_test
            and rust_module is None
            and len(source_parts) == 2
            and nextest is not None
            and nextest[1].split("::")[-1] == PurePosixPath(path).stem
            and row_name == symbol
        )
        if matches and (core_command is None or _cargo_selects_test(core_command, None, row_name)):
            return True
    try:
        root = ElementTree.fromstring(
            evidence, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except (ElementTree.ParseError, DefusedXmlException):
        return False
    if root.tag not in {"testsuite", "testsuites"}:
        return False
    for suite in root.iter("testsuite"):
        for testcase in suite.findall("testcase"):
            if any(child.tag in {"skipped", "failure", "error"} for child in testcase):
                continue
            case_name = testcase.get("name")
            if not case_name:
                continue
            classname = testcase.get("classname")
            qualified_name = (
                f"{classname}::{case_name}" if classname and "::" not in case_name else case_name
            )
            if module_test.fullmatch(qualified_name) and (
                core_command is None or _cargo_selects_test(core_command, None, qualified_name)
            ):
                return True
    return False
