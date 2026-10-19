"""Fail-closed reuse decisions over candidate source blobs."""

from pathlib import Path

import pytest

from gobby.tasks.validation_reuse_classifier import (
    REVIEWED_DISPOSITIONS,
    Disposition,
    classify_module,
    disposition_pin,
)


def _classify(source: str, conftest: str = "", **extra: str) -> tuple[str, ...]:
    sources = {"tests/test_unit.py": source, "tests/conftest.py": conftest, **extra}
    return classify_module("tests/test_unit.py", sources, dispositions=()).reasons


@pytest.fixture
def tree_sources() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {
        str(path.relative_to(root)): path.read_text()
        for folder in ("src/gobby", "tests")
        for path in (root / folder).rglob("*.py")
    }


def test_real_root_chain_leaves_plain_module_eligible(tree_sources: dict[str, str]) -> None:
    plain = classify_module("tests/tasks/test_criterion_commands.py", tree_sources)
    assert plain.eligible, plain.reasons
    refused = classify_module(
        "tests/agents/test_plan_adversary_no_edits_on_reject.py", tree_sources
    )
    assert not refused.eligible
    assert any("agent->repo_root" in reason for reason in refused.reasons)


def test_disposition_pins_are_current_and_fail_closed(tree_sources: dict[str, str]) -> None:
    assert REVIEWED_DISPOSITIONS
    for entry in REVIEWED_DISPOSITIONS:
        assert disposition_pin(entry, tree_sources) == entry.pin
    for path, before, after in (
        ("tests/conftest.py", "operator_home = Path.home()", "operator_home = Path.cwd()"),
        ("tests/fixtures/gdaemon_binary.py", "import", "# changed dependency\nimport"),
    ):
        edited = dict(tree_sources)
        assert before in edited[path]
        edited[path] = edited[path].replace(before, after, 1)
        result = classify_module("tests/tasks/test_criterion_commands.py", edited)
        assert not result.eligible
        assert any("infrastructure_pin_stale:" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "site",
    [
        "subprocess.run(['x'])",
        "pty.spawn('x')",
        "runpy.run_module('x')",
        "os.system('x')",
        "os.execv('x', [])",
        "os.spawnv(0, 'x', [])",
        "os.popen('x')",
        "asyncio.create_subprocess_exec('x')",
        "__file__",
        "Path.cwd()",
        "os.getcwd()",
        "Path.home()",
        "Path('~').expanduser()",
        "os.path.expanduser('~')",
        "importlib.resources.files('x')",
        "importlib.resources.as_file(resource)",
        "'src/a'",
        "'tests/a'",
        "'crates/a'",
        "'web/a'",
        "'.gobby/a'",
    ],
)
def test_sites_in_executed_units_refuse(site: str) -> None:
    imports = "import subprocess, pty, runpy, os, asyncio, importlib.resources\nfrom pathlib import Path\n"
    assert _classify(imports + f"def test_it():\n    {site}\n")
    for trigger in (
        "@pytest.fixture(autouse=True)\ndef danger():",
        "def pytest_sessionstart(session):",
        "@pytest.fixture\ndef danger():",
    ):
        conftest = imports + f"import pytest\n{trigger}\n    helper()\ndef helper():\n    {site}\n"
        assert _classify("def test_it(danger): pass", conftest)
    conftest = imports + f"import pytest\n@pytest.fixture\ndef danger():\n    {site}\n"
    assert not _classify("def test_it(): pass", conftest)
    for source in (
        "import pytest\n@pytest.mark.usefixtures('danger')\ndef test_it(): pass",
        "import pytest\npytestmark = pytest.mark.usefixtures('danger')\ndef test_it(): pass",
        "def test_it(outer): pass",
    ):
        assert _classify(source, conftest + "\n@pytest.fixture\ndef outer(danger): pass")
    assert _classify(
        "from tests.helper import work\ndef test_it(): work()",
        "",
        **{"tests/helper.py": imports + site},
    )


@pytest.mark.parametrize(
    "source, reason",
    [
        ("def test_it(request): request.getfixturevalue(variable)", "dynamic_fixture"),
        ("def test_it(unknown): pass", "unresolved_fixture:unknown"),
        ("def test_it(cache): pass", "unbounded_fixture:cache"),
        (
            "import pytest\n@pytest.mark.parametrize('value', [1], indirect=variable)\ndef test_it(value): pass",
            "dynamic_indirect",
        ),
        ("import importlib\ndef test_it(): importlib.import_module(variable)", "dynamic_import"),
        ("def test_it(): __import__(variable)", "dynamic_import"),
        ("import pkgutil\ndef test_it(): pkgutil.iter_modules(variable)", "dynamic_import"),
        (
            "import importlib.metadata\ndef test_it(): importlib.metadata.entry_points(group=variable)",
            "dynamic_import",
        ),
    ],
)
def test_unresolvable_and_dynamic_requests_refuse(source: str, reason: str) -> None:
    assert any(reason in refusal for refusal in _classify(source))


def test_parametrization_fixture_overrides_and_aliases() -> None:
    direct = "import pytest\n@pytest.mark.parametrize('value,other', [(1,2)])\ndef test_it(value,other): pass"
    assert not _classify(direct)
    for indirect in ("True", "['value']"):
        source = f"import pytest\n@pytest.mark.parametrize('value', [1], indirect={indirect})\ndef test_it(value): pass"
        assert any("unresolved_fixture:value" in reason for reason in _classify(source))
    assert _classify("from subprocess import run as launch\ndef test_it(): launch(['x'])")
    assert not _classify("import subprocess\ndef test_it(): pass")
    outer = "import pytest\n@pytest.fixture\ndef outer(): return __file__"
    assert _classify(
        "import pytest\n@pytest.fixture\ndef outer(outer): pass\ndef test_it(outer): pass", outer
    )
    assert not _classify(
        "import pytest\n@pytest.fixture\ndef outer(): return 1\ndef test_it(outer): pass", outer
    )


def test_plugins_helpers_and_missing_sources_fail_closed() -> None:
    assert _classify(
        "pytest_plugins = ['tests.plugin']\ndef test_it(): pass",
        "",
        **{"tests/plugin.py": "def pytest_configure(config): return __file__"},
    )
    assert _classify(
        "def test_it(): pass",
        "import pytest\n@pytest.fixture(autouse=True)\ndef fixture():\n    import tests.helper",
        **{"tests/helper.py": "value = __file__"},
    )
    result = classify_module("tests/missing.py", {}, dispositions=())
    assert not result.eligible
    assert any("missing_source" in reason for reason in result.reasons)
    assert _classify("def test_it(: pass")


def test_literal_dynamic_imports_and_recursive_plugins_scan_their_helpers() -> None:
    assert _classify(
        "import importlib\ndef test_it(): importlib.import_module('tests.helper')",
        "",
        **{"tests/helper.py": "outside = __file__"},
    )
    assert _classify(
        "pytest_plugins = ['tests.plugin']\ndef test_it(): pass",
        "",
        **{
            "tests/plugin.py": "pytest_plugins = ['tests.nested']",
            "tests/nested.py": "def pytest_sessionstart(session): return __file__",
        },
    )


def test_disposition_covers_only_exact_unit_and_lazy_dependency() -> None:
    sources = {
        "tests/test_unit.py": "def test_it(): pass",
        "tests/conftest.py": "import pytest\n@pytest.fixture(autouse=True)\ndef guard():\n    from tests.helper import safe\n    return __file__\n@pytest.fixture\ndef other(): return __file__",
        "tests/helper.py": "safe = __file__",
    }
    entry = Disposition("tests/conftest.py", "guard", "isolation", "")
    pinned = Disposition(entry.path, entry.symbol, entry.kind, disposition_pin(entry, sources))
    assert classify_module("tests/test_unit.py", sources, dispositions=(pinned,)).eligible
    sources["tests/test_unit.py"] = "def test_it(other): pass"
    assert not classify_module("tests/test_unit.py", sources, dispositions=(pinned,)).eligible
    sources["tests/helper.py"] += "\nchanged = True"
    reasons = classify_module("tests/test_unit.py", sources, dispositions=(pinned,)).reasons
    assert any("infrastructure_pin_stale" in reason for reason in reasons)


@pytest.mark.parametrize(
    "module_import, dangerous_call, unrelated_import",
    [
        ("import subprocess", "subprocess.run(['x'])", "import os as subprocess"),
        (
            "from subprocess import run as launch",
            "launch(['x'])",
            "from os import getpid as launch",
        ),
    ],
)
def test_unrelated_local_import_does_not_hide_module_alias(
    module_import: str, dangerous_call: str, unrelated_import: str
) -> None:
    source = (
        f"{module_import}\ndef test_it():\n    {dangerous_call}\n"
        f"def helper():\n    {unrelated_import}\n"
    )
    reasons = _classify(source)
    assert any("unbounded_input:" in reason for reason in reasons), reasons
    safe_source = f"{module_import}\ndef test_it(): pass\ndef helper():\n    {unrelated_import}\n"
    assert _classify(safe_source) == ()


@pytest.mark.parametrize(
    "helper_body, expected",
    [
        ("request.getfixturevalue('danger')", "unbounded_input:"),
        ("request.getfixturevalue(variable)", "dynamic_fixture"),
        ("return 1", ""),
    ],
)
def test_correction_helper_fixture_requests(helper_body: str, expected: str) -> None:
    reasons = _classify(
        "from tests.helper import work\ndef test_it(request): work(request)",
        "import pytest\n@pytest.fixture\ndef danger(): return __file__",
        **{"tests/helper.py": f"def work(request):\n    {helper_body}\n"},
    )
    if expected:
        assert any(expected in reason for reason in reasons), reasons
    else:
        assert reasons == ()


@pytest.mark.parametrize("options", ["{'autouse': True}", "options"])
def test_correction_fixture_keyword_unpacking_refuses(options: str) -> None:
    reasons = _classify(
        "def test_it(): pass",
        f"import pytest\n@pytest.fixture(**{options})\ndef danger(): return __file__",
    )
    assert reasons, "unresolved fixture options must fail closed"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('subprocess').run(['x'])",
        "__import__('pty').spawn('x')",
        "__import__('runpy').run_module('x')",
        "__import__('os').system('x')",
        "importlib.import_module('pathlib').Path.home()",
        "importlib.import_module('importlib.resources').files('x')",
    ],
)
def test_correction_literal_import_forbidden_namespace(expression: str) -> None:
    reasons = _classify(f"import importlib\ndef test_it():\n    {expression}\n")
    assert any("unbounded_input:" in reason for reason in reasons), reasons


def test_correction_literal_repository_import_can_be_bounded() -> None:
    assert (
        _classify(
            "import importlib\ndef test_it(): importlib.import_module('tests.helper')",
            **{"tests/helper.py": "def work(): return 1"},
        )
        == ()
    )
