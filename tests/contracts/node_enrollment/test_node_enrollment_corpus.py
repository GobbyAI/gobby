"""Contract for the node enrollment and node install spec corpus.

The corpus is written from the acceptance lines of ``.gobby/plans/node-install-enrollment.md``
and is not replayed: there is no Python implementation of these verbs to record. Its
document (``corpus.json`` beside this file)::

    {
      "contract_refs": ["docs/contracts/node-commands.md"],
      "command_frames": [],
      "cases": [
        {
          "id": "2.3.1",
          "kind": "enrollment",
          "command": "gobby node enroll",
          "plan_line": 755,
          "anchor_text": "<the plan line, verbatim>",
          "http": [{"method": "POST", "path": "/api/auth/keys/bootstrap",
                    "route_source_line": 718}]
        }
      ]
    }

``http`` is present only on a case whose anchor states an outcome of the hub's answer to
a route; ``route_source_line`` is the plan line that names that route. Enrollment is
HTTP, not a command frame, so ``command_frames`` stays empty and no enrollment command or
route reuses the hub-node command contract.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_PATH = Path(__file__).resolve().parent / "corpus.json"
NODE_PLAN = REPO_ROOT / ".gobby" / "plans" / "node-install-enrollment.md"
CONTRACT_DOC = REPO_ROOT / "docs" / "contracts" / "node-commands.md"
HUB_CORPUS = (
    REPO_ROOT / "crates" / "gdaemon" / "tests" / "fixtures" / "node_commands" / "corpus.json"
)
ROUTE_MODULES = (
    REPO_ROOT / "src" / "gobby" / "servers" / "routes" / "api_keys.py",
    REPO_ROOT / "src" / "gobby" / "servers" / "routes" / "auth.py",
)
CONTRACT_REF = "docs/contracts/node-commands.md"
ANCHOR = re.compile(r"^- (\d+\.\d+\.\d+) - ")

# Each plan section the corpus covers, with the entry kind and the verb its anchors run.
SECTIONS: dict[str, tuple[str, str]] = {
    "2.3": ("enrollment", "gobby node enroll"),
    "2.4": ("enrollment", "gdaemon serve"),
    "2.5": ("enrollment", "gdaemon node enroll"),
    "3.1": ("node_install", "gobby node install"),
    "3.2": ("node_install", "gobby node install"),
    "3.3": ("node_install", "gobby node install"),
    "3.4": ("node_install", "gobby node install"),
    "3.5": ("node_install", "gobby node install"),
    "3.6": ("node_install", "gobby node install"),
    "3.7": ("node_install", "gobby node install"),
    "3.8": ("node_install", "gobby node install"),
    "3.9": ("node_install", "gobby node install"),
}
# The highest anchor of each section that the corpus covers.
COVERED_THROUGH: dict[str, int] = {
    "2.3": 7,
    "2.4": 3,
    "2.5": 2,
    "3.1": 5,
    "3.2": 3,
    "3.3": 6,
    "3.4": 4,
    "3.5": 5,
    "3.6": 2,
    "3.7": 3,
    "3.8": 4,
    "3.9": 5,
}
# Anchors left out: version bumps move to the cutover that promotes a binary, and the
# 2.5 tail is CLI registration, an audit, middleware guidance and a parity ledger fact.
EXCLUDED_IDS: frozenset[str] = frozenset(
    {
        "2.3.8",
        "2.4.4",
        "2.4.5",
        "2.4.6",
        "2.5.3",
        "2.5.4",
        "2.5.5",
        "2.5.6",
        "3.1.6",
        "3.2.4",
        "3.3.7",
        "3.4.5",
        "3.5.6",
        "3.6.3",
        "3.7.4",
        "3.8.5",
        "3.9.6",
    }
)
COVERED_IDS: tuple[str, ...] = tuple(
    f"{section}.{number}"
    for section, last in COVERED_THROUGH.items()
    for number in range(1, last + 1)
)
_BOOTSTRAP = ("POST", "/api/auth/keys/bootstrap")
_LOGIN = ("POST", "/api/auth/login")
_REVOKE = ("DELETE", "/api/auth/keys/{key_id}")
# The anchors whose outcome rests on the hub's answer to a route, and those routes.
ROUTE_ANCHORS: dict[str, frozenset[tuple[str, str]]] = {
    "2.3.1": frozenset({_BOOTSTRAP}),
    "2.3.3": frozenset({_BOOTSTRAP}),
    "2.3.5": frozenset({_BOOTSTRAP}),
    "2.3.6": frozenset({_BOOTSTRAP, _LOGIN, _REVOKE}),
}
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _corpus() -> dict[str, Any]:
    assert CORPUS_PATH.is_file(), f"the node spec corpus does not exist at {CORPUS_PATH}"
    loaded = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{CORPUS_PATH} is not a JSON object"
    return loaded


def _cases() -> list[dict[str, Any]]:
    cases = _corpus().get("cases")
    assert isinstance(cases, list), f"{CORPUS_PATH} has no cases list"
    assert cases, f"{CORPUS_PATH} has no cases"
    for case in cases:
        assert isinstance(case, dict), f"{CORPUS_PATH} holds a case that is not an object"
    return cases


def _plan_lines() -> list[str]:
    assert NODE_PLAN.is_file(), f"the node plan does not exist at {NODE_PLAN}"
    return NODE_PLAN.read_text(encoding="utf-8").splitlines()


def _plan_anchors() -> dict[str, int]:
    """The 1-based line of each acceptance anchor in the covered sections."""
    anchors: dict[str, int] = {}
    for number, line in enumerate(_plan_lines(), start=1):
        match = ANCHOR.match(line)
        if match is None:
            continue
        anchor = match.group(1)
        if anchor.rsplit(".", 1)[0] not in SECTIONS:
            continue
        assert anchor not in anchors, f"{NODE_PLAN} names anchor {anchor} twice"
        anchors[anchor] = number
    return anchors


def _route_key(method: str, path: str) -> tuple[str, str]:
    return method.upper(), re.sub(r"\{[^}]+\}", "{}", path)


def _declared_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for module in ROUTE_MODULES:
        assert module.is_file(), f"the route module does not exist at {module}"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        prefixes = [
            keyword.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "APIRouter"
            for keyword in node.keywords
            if keyword.arg == "prefix"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ]
        assert len(prefixes) == 1, f"{module} declares routers with prefixes {prefixes}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "router"
                    and decorator.func.attr in HTTP_METHODS
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ):
                    routes.add((decorator.func.attr.upper(), prefixes[0] + decorator.args[0].value))
    assert routes, "the route modules declare no routes"
    return routes


def _case_routes(case: dict[str, Any]) -> set[tuple[str, str]]:
    http = case.get("http", [])
    assert isinstance(http, list), f"case {case.get('id')} http is not a list"
    return {(str(item.get("method")), str(item.get("path"))) for item in http}


def test_enrollment_corpus_uses_node_verbs() -> None:
    cases = _cases()
    for case in cases:
        command = case.get("command")
        assert isinstance(command, str), f"case {case.get('id')} has no command"
        assert command.startswith(("gobby node ", "gdaemon ")), (
            f"case {case.get('id')} runs {command}, not a node verb"
        )
        assert "gobby auth login" not in command, f"case {case.get('id')} runs the Python login"
    for case in cases:
        section = str(case.get("id")).rsplit(".", 1)[0]
        assert section in SECTIONS, f"case {case.get('id')} is outside the covered sections"
        kind, command = SECTIONS[section]
        assert case.get("kind") == kind, f"case {case['id']} has kind {case.get('kind')}"
        assert case.get("command") == command, f"case {case['id']} runs {case.get('command')}"


def test_enrollment_cases_cover_plan_acceptance() -> None:
    cases = _cases()
    anchors = _plan_anchors()
    lines = _plan_lines()
    assert set(anchors) == set(COVERED_IDS) | EXCLUDED_IDS, (
        f"the plan's anchors {sorted(set(anchors) ^ (set(COVERED_IDS) | EXCLUDED_IDS))} "
        "are not partitioned into covered and excluded"
    )
    ids = [str(case.get("id")) for case in cases]
    assert sorted(ids) == sorted(COVERED_IDS), (
        f"the corpus covers {len(ids)} anchors, not the {len(COVERED_IDS)} it declares"
    )
    assert not EXCLUDED_IDS & set(ids)
    for case in cases:
        anchor = case["id"]
        assert case.get("plan_line") == anchors[anchor], (
            f"case {anchor} cites line {case.get('plan_line')}, not {anchors[anchor]}"
        )
        assert case.get("anchor_text") == lines[anchors[anchor] - 1], (
            f"case {anchor} does not copy its plan line verbatim"
        )


def test_enrollment_routes_match_declared_routes() -> None:
    cases = _cases()
    with_http = [case for case in cases if case.get("http")]
    assert len(with_http) >= 3, f"only {len(with_http)} cases carry http"
    declared = {_route_key(method, path) for method, path in _declared_routes()}
    lines = _plan_lines()
    cited: set[tuple[str, str]] = set()
    for case in cases:
        routes = _case_routes(case)
        assert routes == set(ROUTE_ANCHORS.get(case["id"], frozenset())), (
            f"case {case['id']} carries routes {sorted(routes)}"
        )
        for item in case.get("http", []):
            key = _route_key(item["method"], item["path"])
            assert key in declared, f"case {case['id']} names the undeclared route {key}"
            source = item.get("route_source_line")
            assert isinstance(source, int), f"case {case['id']} route {key} has no source line"
            plan_text = re.sub(r"\{[^}]+\}", "{}", lines[source - 1])
            assert f"{key[0]} {key[1]}" in plan_text, (
                f"plan line {source} does not name {key[0]} {key[1]}"
            )
            cited.add((item["method"], item["path"]))
    assert cited == {_BOOTSTRAP, _LOGIN, _REVOKE}, f"the corpus cites the routes {sorted(cited)}"


def test_enrollment_corpus_declares_no_command_frames() -> None:
    corpus = _corpus()
    assert corpus.get("command_frames") == [], "the enrollment corpus declares command frames"
    refs = corpus.get("contract_refs")
    assert isinstance(refs, list), f"{CORPUS_PATH} has no contract_refs list"
    assert CONTRACT_REF in refs, f"{CORPUS_PATH} does not cite {CONTRACT_REF}"
    for ref in refs:
        assert (REPO_ROOT / ref).is_file(), f"the contract reference {ref} does not exist"


def test_enrollment_does_not_reuse_the_command_contract() -> None:
    assert HUB_CORPUS.is_file(), f"the hub-node command corpus does not exist at {HUB_CORPUS}"
    assert CONTRACT_DOC.is_file(), f"the node command contract does not exist at {CONTRACT_DOC}"
    hub = json.loads(HUB_CORPUS.read_text(encoding="utf-8"))
    messages = hub.get("messages")
    assert isinstance(messages, list), f"{HUB_CORPUS} has no messages list"
    assert messages, f"{HUB_CORPUS} has no messages"
    frame_names: set[str] = set()
    for message in messages:
        body = json.loads(message["json"])
        for key in ("type", "command"):
            value = body.get(key) if isinstance(body, dict) else None
            if isinstance(value, str):
                frame_names.add(value)
    assert frame_names, f"{HUB_CORPUS} names no message type or command"
    contract = CONTRACT_DOC.read_text(encoding="utf-8")
    cases = _cases()
    corpus = _corpus()
    assert corpus.get("command_frames") == [], "the enrollment corpus declares command frames"
    for case in cases:
        assert case.get("command") not in frame_names, (
            f"case {case.get('id')} reuses the command frame {case.get('command')}"
        )
        for method, path in _case_routes(case):
            assert f"{method} {path}" not in contract, (
                f"case {case.get('id')} reuses the contract route {method} {path}"
            )
