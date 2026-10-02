"""Tests for inline Python pipeline classification."""

import shlex
import warnings
from typing import Any

import pytest

from gobby.hooks._python_pipeline_classifier import (
    _classify_python_source_with_targets,
    _is_read_only_python_pipeline,
    _PythonExecutionClassification,
)
from gobby.hooks.normalization import normalize_tool_fields


def _classify_python_pipeline(script: str) -> bool:
    return _is_read_only_python_pipeline(["python3", "-c", script])


@pytest.mark.parametrize("callback", ["__import__", "eval", "exec", "compile", "getattr"])
def test_python_pipeline_normalization_rejects_forbidden_callback_names(
    callback: str,
) -> None:
    assert not _classify_python_pipeline(f"list(map({callback}, []))")


@pytest.mark.parametrize(
    "script",
    [
        'list(map(__import__, ["os"]))',
        'list(filter(exec, ["pass"]))',
        'import sys; sorted(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; min(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; max(["x"], key=lambda value: sys.modules["builtins"].eval)',
        'import sys; iter(sys.modules["builtins"].eval, "")',
        'import sys; next(sys.modules["builtins"].iter([]))',
        'sorted(["x"], key=lambda value: value.__class__)',
        "import sys; sys.stdin = []",
        (
            'import sys; list(map(sys.modules["builtins"].eval, '
            '["__import__(\\"os\\").system(\\"touch src/pwn\\")"]))'
        ),
    ],
)
def test_python_pipeline_normalization_rejects_higher_order_callback_escapes(
    script: str,
) -> None:
    assert not _classify_python_pipeline(script)


@pytest.mark.parametrize(
    "script",
    [
        "import json, sys; print(json.load(sys.stdin))",
        "import sys; lines = sys.stdin.readlines(); print(sorted(lines))",
        "import sys; x = sys.stdin.readlines(); print(len(list(filter(None, x))))",
        "import sys; print(list(map(str, sys.stdin.readlines())))",
        ("import sys; print(sorted(sys.stdin.readlines(), key=lambda line: line.lower()))"),
    ],
)
def test_python_pipeline_normalization_keeps_legitimate_read_only_scripts_safe(
    script: str,
) -> None:
    assert _classify_python_pipeline(script)


@pytest.mark.parametrize(
    "script",
    [
        'list(map(__import__, ["os"]))',
        'list(filter(exec, ["pass"]))',
        'import sys; sorted(["x"], key=lambda value: sys.modules["builtins"].eval)',
    ],
)
def test_python_pipeline_normalization_drops_callback_escape_read_only_classification(
    script: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "exec_command",
        "tool_input": {"command": f"gcode outline src/app.py | python3 -c '{script}'"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True
    assert "canonical_code_index_navigation" not in data


@pytest.mark.parametrize(
    "script",
    [
        "import sys; lines = sys.stdin.readlines(); print(sorted(lines))",
        "import sys; x = sys.stdin.readlines(); print(len(list(filter(None, x))))",
    ],
)
def test_python_pipeline_normalization_preserves_safe_read_only_classification(
    script: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "exec_command",
        "tool_input": {"command": f"gcode outline src/app.py | python3 -c '{script}'"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "read"
    assert data["canonical_code_index_navigation"] is True
    assert "canonical_repo_mutation" not in data


_MANIFEST_PATH_OVERLAP = """
import sys, re, collections, itertools
rx = re.compile(r'(?:src/gobby|crates|docs/|tests/)[A-Za-z0-9_./@-]*')
leaf = collections.defaultdict(collections.OrderedDict)
cur = {}
for line in sys.stdin:
    plan, _, body = line.rstrip('\\n').partition('\\t')
    m = re.match(r'- title: (.*)', body)
    if m:
        cur[plan] = m.group(1).strip()
        leaf[plan][cur[plan]] = ['?', set()]
        continue
    if plan not in cur:
        continue
    for p in rx.findall(body):
        p = p.rstrip('.').rstrip('/')
        if '/' in p:
            leaf[plan][cur[plan]][1].add(p)
allp = {plan: {p for t, (s, ps) in leaf[plan].items() for p in ps} for plan in leaf}
for a, b in itertools.combinations(allp, 2):
    inter = sorted(allp[a] & allp[b])
    print(f'### {a} x {b}: {len(inter)}')
    for p in inter:
        print('   ', p, ','.join(s for t, (s, ps) in leaf[a].items() if p in ps))
"""

_WORD_HISTOGRAM = """
from collections import Counter
import re
import sys


def tokens(text: str) -> list[str]:
    return [word.lower() for word in re.findall(r'[A-Za-z]+', text)]


counts: Counter[str] = Counter()
for line in sys.stdin:
    for word in tokens(line):
        counts[word] += 1
try:
    limit = int(sys.stdin.readline() or 5)
except ValueError:
    limit = 5
for word, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]:
    print(f'{word:<20}{count:>6}')
print(len(counts), 'distinct words', file=sys.stderr)
"""

_XLSX_WORKBOOK_DIAGNOSTIC = """
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

base = Path('/tmp/workbook-extract')
namespace = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
for path in sorted(base.glob('*/*.xlsx')):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read('xl/workbook.xml'))
        sheet = root.find('.//m:sheet', namespace)
        print(path.relative_to(base), sheet.get('name'), sheet.text)
"""

_AWS_MCP_DIAGNOSTIC = """
import asyncio
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig


async def main():
    config = MCPServerConfig(
        name="aws-openapi-smoke",
        project_id="00000000-0000-0000-0000-000000000001",
        transport="stdio",
        command="uvx",
        args=["awslabs.openapi-mcp-server@1.1.5"],
    )
    manager = MCPClientManager([config])
    try:
        await manager.list_tools("aws-openapi-smoke")
        session = await manager.get_client_session("aws-openapi-smoke")
        await session.list_prompts()
        await session.list_resources()
    finally:
        await manager.disconnect_all()


asyncio.run(main())
"""


@pytest.mark.parametrize(
    "script",
    [
        _MANIFEST_PATH_OVERLAP,
        _WORD_HISTOGRAM,
        "values = [1]; values[0] = 2; print(values)",
        "import math, statistics; print(math.pi, statistics.mean([1, 2]))",
        "import sys; print('progress', file=sys.stderr)",
        "from datetime import datetime; print(datetime.now())",
        "import json, sys; payload = json.load(sys.stdin); print(payload.get('name'))",
        (
            "from pathlib import Path; "
            "path = Path('~/.gobby/bootstrap.yaml').expanduser().resolve(); "
            "print(path.exists(), path.is_file(), path.is_dir(), path.stat(), path.read_text(), "
            "path.read_bytes(), Path.home(), list(Path.home().iterdir()))"
        ),
        _XLSX_WORKBOOK_DIAGNOSTIC,
    ],
)
def test_python_pipeline_accepts_pure_stdlib_analysis_scripts(script: str) -> None:
    assert _classify_python_pipeline(script)


@pytest.mark.parametrize(
    "script",
    [
        "import os; os.remove('x')",
        "from os import remove; remove('x')",
        "import pathlib; pathlib.Path('x').write_text('y')",
        "import subprocess; subprocess.run(['rm', 'x'])",
        "open('x', 'w').write('y')",
        "import csv; csv.io.open('x', 'w')",
        "import datetime; datetime.sys.modules['os'].remove('x')",
        "import datetime\nheld = datetime.sys\nheld.modules['os'].remove('x')",
        "import datetime\nf = lambda re: re.remove('x')\nf(datetime.sys)",
        "import re\nre.sub('a', lambda m: open('x', 'w').write('y'), 'a')",
        "def f():\n    open('x', 'w').write('1')\n\n\nf()",
        "def f(x=__import__('os').remove('y')):\n    pass",
        "def f(x: __import__('os').remove('y')):\n    pass",
        "from collections import _sys\n_sys.modules['os'].remove('x')",
        "import operator; operator.attrgetter('__class__')",
        "import re as r; r.compile('x')",
        "for sys in []:\n    pass",
        "with open('x') as re:\n    pass",
        "try:\n    pass\nexcept Exception as re:\n    pass",
        "from collections.abc import Mapping",
        "from re import *",
        "import sys; sys.stderr = None",
        "import json; json.dump({}, open('x', 'w'))",
        "import zipfile; zipfile.ZipFile('out.xlsx', 'w')",
        "import zipfile; zipfile.ZipFile('out.xlsx', mode='a')",
        "from pathlib import Path; Path('out.txt').write_text('payload')",
        "writer = open; writer('out.txt', 'w')",
        ("import xml.etree.ElementTree as ET\nET = open\nET('out.xml', 'w')"),
    ],
)
def test_python_pipeline_rejects_mutation_and_reflection_escapes(script: str) -> None:
    assert not _classify_python_pipeline(script)


def test_workbook_diagnostic_heredoc_is_read_only() -> None:
    command = f"python3 - <<'PYEOF'\n{_XLSX_WORKBOOK_DIAGNOSTIC}\nPYEOF"
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "execute"
    assert not data.get("canonical_repo_mutation")


def test_python_pipeline_normalization_keeps_uv_run_analysis_script_read_only() -> None:
    script = (
        "import re, sys, collections; "
        "counts = collections.Counter(re.findall(r'[a-z]+', sys.stdin.read())); "
        "print(counts.most_common(3))"
    )
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": f"awk '{{print}}' notes.md | uv run python -c \"{script}\""},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "execute"
    assert "canonical_repo_mutation" not in data


def test_python_pipeline_normalization_marks_aws_mcp_diagnostic_indeterminate() -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": f"uv run python -c {shlex.quote(_AWS_MCP_DIAGNOSTIC)}"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "execute"
    assert data["canonical_tool_confidence"] == "low"
    assert "canonical_repo_mutation" not in data


@pytest.mark.parametrize(
    "command",
    [
        'python3 -c "import websockets; print(websockets.__version__)"',
        'python3 -c "print(type(v).__name__)"',
        'python3 -c "print(pkg.__file__)"',
        'python3 -c "print(pkg.__doc__)"',
        'python3 -c "print(pkg.__module__)"',
    ],
)
def test_metadata_dunder_attribute_load_is_not_mutation(command: str) -> None:
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "execute"
    assert data["canonical_tool_confidence"] == "low"
    assert "canonical_repo_mutation" not in data


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import statistics as st; print(st.mean([1, 2]))'",
        "python3 -c 'import json as j; print(j.dumps({}))'",
        "python3 - <<'EOF'\nimport math, statistics as st\nprint(st.mean([math.pi]))\nEOF",
    ],
)
def test_a_fresh_import_alias_is_not_mutation(command: str) -> None:
    """``import statistics as st`` rebinds nothing the analysis trusts (#22410 found work)."""
    data: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "execute"
    assert data["canonical_tool_confidence"] == "low"
    assert "canonical_repo_mutation" not in data


@pytest.mark.parametrize(
    "script",
    [
        "import sys as math\nprint(math.argv)",
        "import builtins as re\nre.compile('x')",
        "import os as json\nprint(json.getcwd())",
        # A fresh alias must not hide a pure module's reach into io or sys.
        "import csv as c\nwriter = c.io.open\nwriter('x', 'w')",
        "import datetime as d\nheld = d.sys\nheld.exit()",
    ],
)
def test_an_import_alias_cannot_hide_an_escape(script: str) -> None:
    """An alias that shadows a reserved name, or reaches past a pure module, is still an escape."""
    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ()


@pytest.mark.parametrize(
    "attribute",
    [
        "__import__",
        "__builtins__",
        "__class__",
        "__subclasses__",
        "__dict__",
        "__globals__",
    ],
)
def test_escape_dunder_still_classifies_as_mutation(attribute: str) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": f'python3 -c "print(value.{attribute})"'},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_tool_confidence"] == "high"
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    "script",
    [
        "open('notes.md', 'w').write('changed')",
        "from pathlib import Path; Path('notes.md').write_text('changed')",
        "from pathlib import Path; Path('notes.md').replace('renamed.md')",
        "from pathlib import Path; path = Path('notes.md'); path.replace('renamed.md')",
        "import os; os.remove('notes.md')",
        "import subprocess; subprocess.run(['rm', 'notes.md'])",
        "import sys; sys.modules[\"builtins\"].eval(\"open('notes.md', 'w')\")",
    ],
)
def test_python_pipeline_normalization_keeps_proven_mutations_as_writes(
    script: str,
) -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": f"uv run python -c {shlex.quote(script)}"},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_tool_confidence"] == "high"
    assert data["canonical_repo_mutation"] is True


def test_python_pipeline_normalization_keeps_uv_run_mutation_script_write() -> None:
    data: dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": "uv run python -c \"import os; os.remove('notes.md')\""},
    }

    normalize_tool_fields(data)

    assert data["canonical_tool_kind"] == "write"
    assert data["canonical_repo_mutation"] is True


@pytest.mark.parametrize(
    ("script", "expected_targets"),
    [
        ("open('notes.md', 'w').write('changed')", ("notes.md",)),
        ("from pathlib import Path; Path('notes.md').write_text('changed')", ("notes.md",)),
        (
            "from pathlib import Path; path = Path('notes.md'); path.replace('renamed.md')",
            ("notes.md", "renamed.md"),
        ),
        ("import os; os.rename('a.md', 'b.md')", ("a.md", "b.md")),
        ("import shutil; shutil.copy('src.md', 'dst.md')", ("dst.md",)),
        ("import zipfile; zipfile.ZipFile('out.zip', 'w')", ("out.zip",)),
        ("from pathlib import Path; Path('a.md').open('w')", ("a.md",)),
        (
            "from pathlib import Path; Path('a.md').write_text('x'); Path('b.md').touch()",
            ("a.md", "b.md"),
        ),
    ],
)
def test_python_source_mutation_targets_are_collected(
    script: str, expected_targets: tuple[str, ...]
) -> None:
    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == expected_targets


@pytest.mark.parametrize(
    "script",
    [
        "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('x')",
        "from pathlib import Path; Path('a.md').write_text('x'); Path(input()).unlink()",
        "import os; name = 'a.md'; os.remove(name)",
        "from pathlib import Path; p = Path('a.md'); p = Path('b.md'); p.write_text('x')",
        "import subprocess; subprocess.run(['rm', 'notes.md'])",
        "import sys; sys.modules[\"builtins\"].eval(\"open('notes.md', 'w')\")",
        "import sys; from pathlib import Path; Path(sys.argv[1]).open('w')",
    ],
)
def test_python_source_mutation_without_literal_scope_has_no_targets(script: str) -> None:
    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ()


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(
            "p = Path('/tmp/safe')\nfor p in [Path('src/a.py')]:\n    p.write_text('x')",
            id="for-target",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\n[p.write_text('x') for p in [Path('src/a.py')]]",
            id="comprehension-target",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\nwith nullcontext(Path('src/a.py')) as p:\n"
            "    p.write_text('x')",
            id="with-as-target",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\np /= '/repo/src/a.py'\np.write_text('x')",
            id="augmented-assignment",
        ),
        pytest.param(
            "def touch(p):\n    p.write_text('x')\np = Path('/tmp/safe')\ntouch(Path('src/a.py'))",
            id="argument",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\np, other = Path('src/a.py'), 1\np.write_text('x')",
            id="tuple-unpack",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\nclass p:\n    write_text = Path('src/a.py').write_text\n"
            "p.write_text('x')",
            id="class-definition",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\nasync def p():\n    pass\np.write_text('x')",
            id="async-function-definition",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\ndef p():\n    pass\np.write_text('x')",
            id="function-definition",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\ndef touch[p](q: p) -> None:\n    p.write_text('x')",
            id="type-parameter",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\nglobals()['p'] = Path('src/a.py')\np.write_text('x')",
            id="globals-subscript",
        ),
        pytest.param(
            "p = Path('/tmp/safe')\ng = globals()\ng.update(p=Path('src/a.py'))\np.write_text('x')",
            id="globals-alias-update",
        ),
        pytest.param(
            "import sys\np = Path('/tmp/safe')\nsys._getframe(0).f_globals['p'] = Path('src/a.py')\n"
            "p.write_text('x')",
            id="frame-globals",
        ),
        pytest.param(
            "from builtins import globals as g\np = Path('/tmp/safe')\n"
            "g()['p'] = Path('src/a.py')\np.write_text('x')",
            id="aliased-globals-import",
        ),
    ],
)
def test_path_name_rebound_by_another_binding_has_no_targets(script: str) -> None:
    source = f"from contextlib import nullcontext\nfrom pathlib import Path\n{script}\n"

    classification, targets = _classify_python_source_with_targets(source)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ()


def test_path_name_with_repeated_identical_literal_keeps_its_target() -> None:
    source = "from pathlib import Path\np = Path('/tmp/a')\np = Path('/tmp/a')\np.write_text('x')\n"

    assert _classify_python_source_with_targets(source) == (
        _PythonExecutionClassification.MUTATION,
        ("/tmp/a",),
    )


def test_namespace_inspection_without_a_write_is_not_a_mutation() -> None:
    classification, targets = _classify_python_source_with_targets("print(sorted(globals()))\n")

    assert classification is not _PythonExecutionClassification.MUTATION
    assert targets == ()


@pytest.mark.parametrize(
    "state_change",
    [
        "p._raw_paths = ['src/a.py']",
        "q = p\nq._raw_paths = ['src/a.py']",
        "p._raw_paths[0] = 'src/a.py'",
        "parts = p._raw_paths\nparts.clear()\nparts.append('src/a.py')",
        "q = p\nq._raw_paths.clear()\nq._raw_paths.append('src/a.py')",
        "from redirector import redirect\nredirect(p)",
    ],
)
def test_mutable_path_receiver_state_has_unknown_write_scope(state_change: str) -> None:
    script = (
        f"from pathlib import Path\np = Path('/tmp/safe.txt')\n{state_change}\np.write_text('x')\n"
    )

    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ()


@pytest.mark.parametrize(
    "escape",
    [
        "redirect(p.write_text)",
        "method = p.write_text\nredirect(method)",
        "redirect(p.as_posix)",
        "method = p.as_posix\nredirect(method)",
        "redirect(p.absolute())",
        "redirect(p.expanduser())",
        "redirect(p.glob('*'))",
    ],
)
def test_bound_path_receiver_escape_has_unknown_write_scope(escape: str) -> None:
    script = (
        "from pathlib import Path\n"
        "from redirector import redirect\n"
        "p = Path('/tmp/safe.txt')\n"
        f"{escape}\n"
        "p.write_text('x')\n"
    )

    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ()


@pytest.mark.parametrize(
    "inspection",
    [
        "print(p.name, p.as_posix())",
        "print(p.suffix, p.parts, p.is_absolute())",
        "print(p.stat(), p.exists())",
    ],
)
def test_public_path_inspection_preserves_literal_write_scope(inspection: str) -> None:
    script = (
        f"from pathlib import Path\np = Path('/tmp/safe.txt')\n{inspection}\np.write_text('x')\n"
    )

    classification, targets = _classify_python_source_with_targets(script)

    assert classification is _PythonExecutionClassification.MUTATION
    assert targets == ("/tmp/safe.txt",)


def test_python_source_read_only_and_indeterminate_have_no_targets() -> None:
    assert _classify_python_source_with_targets("print(open('a.md').read())") == (
        _PythonExecutionClassification.READ_ONLY,
        (),
    )
    assert _classify_python_source_with_targets("import requests") == (
        _PythonExecutionClassification.INDETERMINATE,
        (),
    )


def test_invalid_escape_in_agent_source_classifies_without_a_syntax_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _classify_python_source_with_targets('x = "a\\$b"\nprint(x)\n')

    assert [str(warning.message) for warning in caught] == []
    assert result == (_PythonExecutionClassification.READ_ONLY, ())
