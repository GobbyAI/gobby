"""Path policy for test writing and implementation evidence in a TDD cycle."""

from pathlib import PurePosixPath

_DOCUMENTATION_ROOTS = frozenset({"docs", ".gobby"})
_INSTRUCTION_FILES = frozenset({"agents.md", "claude.md", "readme.md", "changelog.md"})
_MODULE_SUFFIXES = frozenset({".py", ".pyi", ".rs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})


def _test_module_name(name: str) -> bool:
    return name.startswith("test_") or "_test." in name or ".test." in name or ".spec." in name


def is_test_convention_path(path: str) -> bool:
    """A test module in any language or any file under a test directory."""
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    if (name == "tests.rs" or name.endswith("_tests.rs")) and any(
        part.casefold() == "src" for part in pure.parts[:-1]
    ):
        return True
    return any(
        part.casefold() in {"test", "tests", "__tests__"} for part in pure.parts[:-1]
    ) or _test_module_name(name)


def is_production_edit_path(path: str) -> bool:
    """Ordinary implementation paths, also used by production stub evidence checks."""
    if is_test_convention_path(path):
        return False
    pure = PurePosixPath(path)
    if pure.parts and pure.parts[0].casefold() in _DOCUMENTATION_ROOTS:
        return False
    return pure.name.casefold() not in _INSTRUCTION_FILES


def is_implementation_edit_path(
    path: str,
    *,
    named_test_paths: frozenset[str],
    test_infrastructure_paths: frozenset[str],
) -> bool:
    """Exclude tests always; allow linked fixture modules only through an explicit opt-in."""
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    if path in named_test_paths or name == "conftest.py":
        return False
    if is_test_convention_path(path):
        if (
            path not in test_infrastructure_paths
            or not path.startswith("tests/")
            or pure.suffix.casefold() not in _MODULE_SUFFIXES
            or _test_module_name(name)
            or name == "tests.rs"
            or name.endswith("_tests.rs")
        ):
            return False
    if pure.parts and pure.parts[0].casefold() in _DOCUMENTATION_ROOTS:
        return False
    return name not in _INSTRUCTION_FILES
