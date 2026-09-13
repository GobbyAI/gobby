"""Reference audit checks; inventory construction never invokes public operations."""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from inspect import signature
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit

import click
from markdown_it import MarkdownIt
from mcp.server.mcpserver import MCPServer

from gobby.cli import cli
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server
from gobby.mcp_proxy.stdio_tools import register_proxy_tools
from gobby.skills.capability_catalog import load_capability_catalog

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = Path("src/gobby/install/shared/skills/gobby")
Audit = dict[str, Any]


def load_audits(root: Path = ROOT) -> list[Audit]:
    return [
        json.loads(p.read_text()) for p in sorted((root / "docs/reference-audit").glob("*.json"))
    ]


def internal_tool_inventory() -> set[tuple[str, str]]:
    """Exercise configured registry branches using inert service boundaries."""
    config = DaemonConfig()
    dependencies: dict[str, Any] = {
        name: MagicMock(name=name) for name in signature(setup_internal_registries).parameters
    }
    database = MagicMock(name="inventory_database")
    database.fetchone.return_value = None
    database.fetchall.return_value = []
    dependencies.update(
        config_resolver=lambda: config,
        db=database,
        git_manager=None,
        project_id="00000000-0000-0000-0000-000000000001",
    )
    # Session registration reads an installed prompt for its tool description.
    # Replace that storage boundary, keeping all registry factories real.
    with patch(
        "gobby.prompts.loader.PromptLoader.load",
        return_value=MagicMock(content="Isolated inventory prompt"),
    ):
        manager = setup_internal_registries(**dependencies)
    return {
        (registry.name, tool["name"])
        for registry in manager.get_all_registries()
        for tool in registry.list_tools()
    }


def cli_inventory(command: click.Command = cli, prefix: str = "gobby") -> set[str]:
    """Include public leaves and independently invocable group callbacks."""
    if command.hidden:
        return set()
    if not isinstance(command, click.Group):
        return {prefix}
    result = {prefix} if command.invoke_without_command else set()
    for name, child in command.commands.items():
        result.update(cli_inventory(child, f"{prefix} {name}"))
    return result


async def proxy_tool_inventory() -> set[tuple[str, str]]:
    # Registration needs real method signatures but never calls the handler.
    daemon = create_mcp_server(object.__new__(GobbyDaemonTools))
    stdio = MCPServer("reference-inventory")
    register_proxy_tools(stdio, MagicMock(name="inert_daemon_proxy"))
    return {
        ("gobby", tool.name) for server in (daemon, stdio) for tool in await server.list_tools()
    }


def native_cli_inventory(root: Path = ROOT) -> set[str]:
    """Use checked-in native command declarations, never installed binaries."""
    contract = json.loads((root / "tests/contracts/gcode.contract.json").read_text())
    source = (root / "crates/gcode/src/contract.rs").read_text()
    declared = set(re.findall(r'CommandContract::new\(\s*"([^"]+)"', source))
    vendored = {command["name"] for command in contract["commands"]}
    if declared != vendored:
        raise ValueError(f"gcode source/contract command drift: {declared ^ vendored}")
    cli_source = (root / "crates/gcode/src/cli.rs").read_text()
    enums = {
        name: {
            re.sub(r"(?<!^)(?=[A-Z])", "-", variant).lower()
            for variant in re.findall(r"^    ([A-Z]\w+)\b", body, re.M)
        }
        for name, body in re.findall(r"enum (\w*Command) \{(.*?)^\}", cli_source, re.M | re.S)
    }
    groups = {
        name.removesuffix("Command").lower(): values
        for name, values in enums.items()
        if name != "Command"
    }
    clap_commands = (enums.get("Command", set()) - groups.keys()) | {
        f"{group} {command}" for group, children in groups.items() for command in children
    }
    if clap_commands != declared:
        raise ValueError(f"gcode Clap/contract command drift: {clap_commands ^ declared}")
    commands = {f"gcode {name}" for name in declared}
    schema_source = (root / "crates/gdaemon/src/main.rs").read_text()
    schema_enum = re.search(r"enum SchemaCommand \{(.*?)^\}", schema_source, re.M | re.S)
    if schema_enum is None:
        raise ValueError("gdaemon SchemaCommand declaration missing")
    for variant in re.findall(r"^    ([A-Z]\w+)", schema_enum[1], re.M):
        name = re.sub(r"(?<!^)(?=[A-Z])", "-", variant).lower()
        # These are private database implementation/test-maintenance entrypoints.
        if name not in {"apply", "sweep-test-schemas"}:
            commands.add(f"gdaemon schema {name}")
    hook_source = (root / "crates/ghook/src/args.rs").read_text()
    for override, field in re.findall(
        r'#\[arg\(long(?: = "([^"]+)")?\)\]\s+pub\(crate\) (\w+):', hook_source
    ):
        commands.add(f"ghook --{override or field.replace('_', '-')}")
    for variant in re.findall(r"^    ([A-Z]\w+)\s*\{", hook_source, re.M):
        commands.add("ghook " + re.sub(r"(?<!^)(?=[A-Z])", "-", variant).lower())
    client_source = (root / "crates/gclient/src/startup.rs").read_text()
    commands.update(f"gclient {flag}" for flag in re.findall(r'arg == "(--[\w-]+)"', client_source))
    return commands


def markdown_targets(path: Path) -> tuple[set[str], list[str]]:
    """Parse rendered headings and links, including reference-style links."""
    tokens = MarkdownIt().parse(path.read_text())
    headings: set[str] = set()
    counts: Counter[str] = Counter()
    links: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            label = "".join(
                child.content
                for child in tokens[index + 1].children or []
                if child.type in {"text", "code_inline"}
            )
            slug = re.sub(r"[^\w\- ]", "", label.lower()).replace(" ", "-")
            count = counts[slug]
            counts[slug] += 1
            headings.add(f"{slug}-{count}" if count else slug)
        for child in token.children or []:
            if child.type in {"link_open", "image"}:
                target = child.attrGet("href" if child.type == "link_open" else "src")
                if isinstance(target, str) and target:
                    links.append(target)
    return headings, links


def coverage_errors(
    audits: list[Audit], tools: set[tuple[str, str]], commands: set[str]
) -> list[str]:
    operations = [operation for audit in audits for operation in audit["operations"]]
    mapped_tools = {(op["server"], op["tool"]) for op in operations if op["surface"] == "mcp"}
    mapped_commands = {op["command"] for op in operations if op["surface"] == "cli"}
    for audit in audits:
        for entry in audit.get("native_cli", []):
            invocation = entry["command"]
            executable = invocation.split()[0]
            if executable in {"ghook", "gclient"}:
                mapped_commands.update(
                    f"{executable} {flag}" for flag in re.findall(r"--[\w-]+", invocation)
                )
            if invocation.startswith("ghook schema-identity"):
                mapped_commands.add("ghook schema-identity")
            if executable == "gdaemon":
                mapped_commands.add(" ".join(invocation.split()[:3]))
    return [
        *(
            f"Unmapped public tool: {server}:{tool}"
            for server, tool in sorted(tools - mapped_tools)
        ),
        *(f"Unmapped public CLI: {command}" for command in sorted(commands - mapped_commands)),
    ]


def local_link_errors(path: Path) -> list[str]:
    errors: list[str] = []
    for target in markdown_targets(path)[1]:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        if not destination.exists():
            errors.append(f"Broken link: {path} -> {target}")
        elif (
            parsed.fragment
            and destination.suffix == ".md"
            and unquote(parsed.fragment) not in markdown_targets(destination)[0]
        ):
            errors.append(f"Broken anchor: {path} -> {target}")
    return errors


def documentation_errors(audits: list[Audit], root: Path = ROOT) -> list[str]:
    """Check exact topic ownership, evidence, source symbols and guide links."""
    errors: list[str] = []
    catalog = load_capability_catalog(root / SKILL_ROOT)
    by_name = {audit["capability"]: audit for audit in audits}
    if len(by_name) != len(audits):
        errors.append("Duplicate capability audits")
    names = {capability.name for capability in catalog.capabilities}
    if set(by_name) != names:
        errors.append(f"Catalog/audit mismatch: {sorted(set(by_name) ^ names)}")
    audited_guides: dict[str, set[str]] = {}
    for guide_audit in audits:
        for guide in guide_audit["guides"]:
            audited_guides.setdefault(guide["path"], set()).update(guide["anchors"])
    for guide_path in audited_guides:
        if (root / guide_path).is_file():
            errors.extend(local_link_errors(root / guide_path))
    for capability in catalog.capabilities:
        audit = by_name.get(capability.name)
        if audit is None:
            continue
        if audit.get("version") != 1:
            errors.append(f"{capability.name}: unsupported audit version")
        if audit.get("status", "verified") != "verified":
            errors.append(f"{capability.name}: audit is not verified")
        evidence = {item["id"] for item in audit["evidence"]}
        for item in audit["evidence"]:
            if item.get("status", "passed") != "passed" or not (
                item.get("result") or item.get("description")
            ):
                errors.append(f"{capability.name}: incomplete evidence {item['id']}")
        if len(evidence) != len(audit["evidence"]):
            errors.append(f"{capability.name}: duplicate evidence identifiers")
        paths = {str(SKILL_ROOT / capability.overview)} | {
            str(SKILL_ROOT / topic.path) for topic in capability.topics
        }
        actual_paths = {
            path.relative_to(root).as_posix()
            for path in (root / SKILL_ROOT / "references" / capability.name).glob("*.md")
        }
        if actual_paths != paths:
            errors.append(
                f"{capability.name}: uncataloged or missing topics {actual_paths ^ paths}"
            )
        for guide in audit["guides"]:
            if guide.get("audit_status", "verified") != "verified":
                errors.append(f"{capability.name}: guide is not verified: {guide['path']}")
            path = root / guide["path"]
            if not path.is_file():
                errors.append(f"Missing guide: {guide['path']}")
                continue
            anchors, _ = markdown_targets(path)
            for anchor in set(guide["anchors"]) - anchors:
                errors.append(f"Missing audited anchor: {guide['path']}#{anchor}")
            proof = set(guide.get("evidence", []))
            if not proof or not proof <= evidence:
                errors.append(f"Missing guide evidence: {capability.name}:{guide['path']}")
        for raw_operation in [*audit["operations"], *audit.get("native_cli", [])]:
            implementation = raw_operation["implementation"]
            if isinstance(implementation, str):
                source_path, _, symbol = implementation.partition("::")
                implementation = {"path": source_path, "symbol": symbol}
            op = {
                **raw_operation,
                "surface": raw_operation.get("surface", "cli"),
                "implementation": implementation,
            }
            if op["reference"] not in paths:
                errors.append(f"Uncataloged operation reference: {op['reference']}")
            proof = set(op.get("verification", []))
            if not proof or not proof <= evidence:
                errors.append(f"Missing operation evidence: {op}")
            if op["surface"] == "cli" and op["command"].startswith("gobby "):
                command: click.Command | None = cli
                for word in op["command"].split()[1:]:
                    # Examples can contain options or positional sub-operations
                    # after the registered Click command (for example build stop).
                    if command is not None and word.startswith("-"):
                        options = {
                            option
                            for parameter in command.params
                            if isinstance(parameter, click.Option)
                            for option in (*parameter.opts, *parameter.secondary_opts)
                        }
                        if word not in options:
                            errors.append(f"Unregistered CLI option: {op['command']}")
                        break
                    if not isinstance(command, click.Group):
                        break
                    command = (
                        command.commands.get(word) if isinstance(command, click.Group) else None
                    )
                    if command is None:
                        errors.append(f"Unregistered CLI mapping: {op['command']}")
                        break
            source = root / op["implementation"]["path"]
            if not source.is_file():
                errors.append(f"Missing implementation: {source}")
                continue
            if source.suffix == ".py":
                tree = ast.parse(source.read_text())
                parts = op["implementation"]["symbol"].split(".")
                # Early audits use unique unqualified nested function names.
                # Qualified names are resolved structurally, component by component.
                nodes = list(ast.walk(tree)) if len(parts) == 1 else list(tree.body)
                for part in parts:
                    matches = [
                        item
                        for item in nodes
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and item.name == part
                    ]
                    if len(matches) != 1:
                        errors.append(
                            f"Missing or ambiguous implementation symbol: {source}:{part}"
                        )
                        break
                    nodes = list(matches[0].body)
        for reference in sorted(paths):
            path = root / reference
            for target in markdown_targets(path)[1]:
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                destination = (
                    (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                )
                if not destination.is_file():
                    errors.append(f"Broken link: {reference} -> {target}")
                    continue
                anchor = unquote(parsed.fragment)
                if anchor and anchor not in markdown_targets(destination)[0]:
                    errors.append(f"Broken anchor: {reference} -> {target}")
                relative = destination.relative_to(root).as_posix()
                if relative.startswith("docs/guides/"):
                    guide_anchors = audited_guides.get(relative)
                    if guide_anchors is None or (anchor and anchor not in guide_anchors):
                        errors.append(f"Unaudited guide link: {reference} -> {target}")
    return errors
