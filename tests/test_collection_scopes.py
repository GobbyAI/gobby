"""Conftest fixtures must survive interleaved explicit-file collection."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("selection", ["explicit", "directory"])
@pytest.mark.parametrize("package", ["child", "child/nested"])
def test_autouse_fixtures_follow_every_directory_collector(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
    package: str,
) -> None:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    pytester.makeini("[pytest]\n")
    pytester.makeconftest(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def root_guard(monkeypatch):\n"
        "    monkeypatch.setenv('COLLECTION_ROOT_GUARD', 'applied')\n"
    )
    for parent in Path(package).parents:
        if parent != Path("."):
            (pytester.path / parent).mkdir(parents=True, exist_ok=True)
            (pytester.path / parent / "__init__.py").touch()
    directory = pytester.path / package
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "__init__.py").touch()
    (directory / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def child_guard(monkeypatch):\n"
        "    monkeypatch.setenv('COLLECTION_CHILD_GUARD', 'applied')\n"
    )
    dependent_test = (
        "import os\n"
        "def test_guard():\n"
        "    root = os.getenv('COLLECTION_ROOT_GUARD')\n"
        "    child = os.getenv('COLLECTION_CHILD_GUARD')\n"
        "    assert root == 'applied'\n"
        "    assert child == 'applied'\n"
    )
    for name in ("test_before.py", "test_after.py"):
        (directory / name).write_text(dependent_test)
    pytester.makepyfile(
        test_middle=(
            "import os\n"
            "def test_sibling_has_root_guard_only():\n"
            "    root = os.getenv('COLLECTION_ROOT_GUARD')\n"
            "    child = os.getenv('COLLECTION_CHILD_GUARD')\n"
            "    assert root == 'applied'\n"
            "    assert child is None\n"
        )
    )
    args = (
        [f"{package}/test_before.py", "test_middle.py", f"{package}/test_after.py"]
        if selection == "explicit"
        else [str(pytester.path)]
    )
    result = pytester.runpytest_subprocess(*args, "-q", "-p", "no:cacheprovider", timeout=30)
    result.assert_outcomes(passed=3)
