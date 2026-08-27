"""Tests for inline Python pipeline classification."""

import shlex
from typing import Any

import pytest

from gobby.hooks._python_pipeline_classifier import _is_read_only_python_pipeline
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
