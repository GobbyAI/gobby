"""Mechanical transforms for Impeccable release references."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from pathlib import Path

REFERENCE_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n+", re.DOTALL)
CATALOGUE_ROW_RE = re.compile(r"^\| `(?P<path>references/[^`]+)` \| (?P<class>[^|]+) \|$")
ADDITIONAL_CONTEXT_RE = re.compile(
    r"\A> \*\*Additional context needed\*\*:\s*(?P<context>[^\n]+)\n?"
)
MARKDOWN_REFERENCE_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<path>(?:reference/)?(?:degraded/)?[^)]+\.md)\)"
)
COMMAND_REFERENCE_RE = re.compile(r"/impeccable\s+(?P<name>[a-z][a-z0-9-]*)")
BACKTICK_COMMAND_REFERENCE_RE = re.compile(r"`/impeccable\s+(?P<name>[a-z][a-z0-9-]*)`")
PLACEHOLDER_RE = re.compile(r"\{\{[a-z_]+\}\}")

PREAMBLE = (
    "> You are continuing a session under the `impeccable` skill; "
    "the design-context protocol and anti-pattern rules already apply."
)
SCRIPTS_RESOLVER = (
    'Resolve `<scripts_dir>` by calling `materialize_skill_scripts(name="impeccable")` '
    "on `gobby-skills`; it returns the absolute path of the skill's materialized "
    "`scripts/` directory. Export the returned `environment.PUPPETEER_CACHE_DIR` "
    "before any browser-engine invocation. If the tool or Node is unavailable, "
    "skip detector runs and scan manually."
)
AVAILABLE_COMMANDS = (
    "`adapt`, `animate`, `audit`, `bolder`, `clarify`, `colorize`, `critique`, "
    "`delight`, `distill`, `harden`, `layout`, `optimize`, `overdrive`, `polish`, "
    "`quieter`, `shape`, `typeset`"
)

# These files carry Gobby prose beyond the standard mechanical adaptation even
# though their catalogue classification predates the explicit curated label.
CURATED_REFERENCE_PATHS = frozenset(
    {
        "references/adapt.native.md",
        "references/audit.native.md",
        "references/doctor.md",
        "references/visualize.md",
    }
)


class UpgradeRejected(RuntimeError):
    """A preflight authority could not produce a coherent vendored release."""


def parse_catalogue(notice: str) -> dict[str, str]:
    catalogue: dict[str, str] = {}
    for line in notice.splitlines():
        match = CATALOGUE_ROW_RE.fullmatch(line)
        if match is not None:
            catalogue[match.group("path")] = match.group("class").strip()
    if not catalogue:
        raise UpgradeRejected("NOTICE.md has no reference adaptation catalogue")
    return catalogue


def _reference_loader(path: str) -> str:
    target = path.removeprefix("reference/")
    if target.startswith("references/"):
        target = target.removeprefix("references/")
    label = Path(target).stem
    return (
        f'`{label}` by calling `get_skill_file(name="impeccable", '
        f'path="references/{target}")` on `gobby-skills`'
    )


def _transform_prose(chunk: str) -> str:
    chunk = chunk.replace("{{scripts_path}}", "<scripts_dir>")
    chunk = chunk.replace(".claude/skills/impeccable/scripts", "<scripts_dir>")
    chunk = chunk.replace("DESIGN.md", ".impeccable.md")
    chunk = chunk.replace("{{config_file}}", ".impeccable.md")
    chunk = chunk.replace("{{ask_instruction}}", "Ask the user")
    chunk = chunk.replace(
        "call the AskUserQuestion tool to clarify.",
        "ask the user directly to clarify what you cannot infer",
    )
    chunk = chunk.replace("STOP and ask the user directly", "ask the user directly")
    chunk = chunk.replace("{{available_commands}}", AVAILABLE_COMMANDS)

    seen: set[str] = set()

    def markdown_reference(match: re.Match[str]) -> str:
        path = match.group("path").removeprefix("reference/")
        label = Path(path).stem
        if path in seen:
            return f"`{label}`"
        seen.add(path)
        return _reference_loader(path)

    chunk = MARKDOWN_REFERENCE_RE.sub(markdown_reference, chunk)

    def command_reference(match: re.Match[str]) -> str:
        path = f"{match.group('name')}.md"
        if path in seen:
            return f"`{match.group('name')}`"
        seen.add(path)
        return (
            f'call `get_skill_file(name="impeccable", path="references/{path}")` '
            "on `gobby-skills` and follow it"
        )

    chunk = re.sub(
        r"hand off to " + BACKTICK_COMMAND_REFERENCE_RE.pattern,
        command_reference,
        chunk,
    )
    chunk = BACKTICK_COMMAND_REFERENCE_RE.sub(command_reference, chunk)
    chunk = COMMAND_REFERENCE_RE.sub(command_reference, chunk)
    chunk = re.sub(r"\{\{command_prefix\}\}([a-z][a-z0-9-]*)", r"\1", chunk)
    return chunk


def _add_resolver(text: str) -> str:
    if "<scripts_dir>" not in text or SCRIPTS_RESOLVER in text:
        return text
    paragraphs = text.split("\n\n")
    for index, paragraph in enumerate(paragraphs):
        if "<scripts_dir>" in paragraph:
            paragraphs.insert(index + 1, SCRIPTS_RESOLVER)
            break
    return "\n\n".join(paragraphs)


def mechanical_reference_transform(source: str) -> str:
    """Apply harness-neutral transforms to one generated release reference."""
    text = REFERENCE_FRONTMATTER_RE.sub("", source, count=1)
    context_match = ADDITIONAL_CONTEXT_RE.match(text)
    if context_match is not None:
        context = context_match.group("context").strip()
        text = f"{PREAMBLE} Additional context needed: {context}\n" + text[context_match.end() :]
    elif not text.startswith(PREAMBLE):
        text = f"{PREAMBLE}\n\n{text.lstrip()}"

    chunks = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    text = "".join(
        chunk if index % 2 else _transform_prose(chunk) for index, chunk in enumerate(chunks)
    )
    text = _add_resolver(text)
    leftovers = sorted(set(PLACEHOLDER_RE.findall(text)))
    if leftovers:
        raise UpgradeRejected(f"reference has unsupported placeholders: {', '.join(leftovers)}")
    return text.rstrip() + "\n"


def reference_plan(
    staged_root: Path,
    destination_root: Path,
    catalogue: Mapping[str, str],
) -> tuple[dict[Path, bytes], list[str]]:
    staged_files = {
        f"references/{path.relative_to(staged_root).as_posix()}": path
        for path in staged_root.rglob("*")
        if path.is_file()
    }
    unknown = sorted(set(staged_files) - set(catalogue))
    if unknown:
        raise UpgradeRejected(
            "released references are missing NOTICE.md classifications: " + ", ".join(unknown)
        )

    writes: dict[Path, bytes] = {}
    judgments: list[str] = []
    for relative, classification in sorted(catalogue.items()):
        destination = destination_root / relative.removeprefix("references/")
        source_path = staged_files.get(relative)
        if "Gobby-retained" in classification:
            if not destination.is_file():
                raise UpgradeRejected(f"retained reference is absent: {relative}")
            continue
        if source_path is None:
            raise UpgradeRejected(f"catalogued released reference is absent: {relative}")
        source = source_path.read_text(encoding="utf-8")
        if classification == "Vendored as-is":
            writes[destination] = source.encode()
            continue

        candidate = mechanical_reference_transform(source)
        mechanically_managed = (
            classification in {"Standard adaptation", "Near-verbatim native reference"}
            and relative not in CURATED_REFERENCE_PATHS
        )
        if mechanically_managed:
            writes[destination] = candidate.encode()
            continue
        if not destination.is_file():
            raise UpgradeRejected(f"curated reference is absent: {relative}")
        current = destination.read_text(encoding="utf-8")
        if current != candidate:
            diff = "\n".join(
                difflib.unified_diff(
                    candidate.splitlines(),
                    current.splitlines(),
                    fromfile=f"released/{relative}",
                    tofile=f"gobby/{relative}",
                    lineterm="",
                )
            )
            judgments.append(f"{relative} ({classification})\n{diff}")
    return writes, judgments
