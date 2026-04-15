"""Refresh impeccable steering-command references from upstream.

Fetches each of the 17 upstream steering-command ``SKILL.md`` files from
``github.com/pbakaus/impeccable`` and rewrites them into Gobby reference
files at ``../references/<cmd>.md``.

Run from anywhere::

    uv run python src/gobby/install/shared/skills/impeccable/.upgrade/transform.py

What it does:

1. Strips the upstream YAML frontmatter (``name``, ``description``,
   ``argument-hint``, ``user-invocable``, ``license``). Gobby reference files
   are classified by directory, not frontmatter.
2. Collapses the upstream ``## MANDATORY PREPARATION`` boilerplate into a
   single blockquote note, preserving any ``Additionally gather: ...`` clause.
   Handles three upstream variants: standard block, ``### Step 1: Preparation``
   subsection (``critique``), and no preparation section at all
   (``harden``, ``optimize``).
3. Substitutes upstream templating placeholders:

   - ``{{command_prefix}}impeccable [teach|craft|extract]`` →
     *"the impeccable skill's `<mode>` mode"*
   - ``{{command_prefix}}impeccable`` (bare) → *"the `impeccable` skill"*
   - ``{{command_prefix}}<cmd>`` inside backticks → bare ``<cmd>`` to avoid
     breaking markdown rendering.
   - ``{{command_prefix}}<cmd>`` in prose → *"the `<cmd>` steering command
     (load via `get_skill_file(...)` on `gobby-skills`)"*
   - ``{{command_prefix}}command-name`` (upstream template literal meaning
     "fill in the actual command") → ``` `<cmd>` ```.
   - ``{{available_commands}}`` → literal backtick-quoted list of the 17
     commands.
   - ``{{model}}``, ``{{config_file}}``, ``{{ask_instruction}}`` replaced
     with concrete language appropriate for a Gobby runtime.

4. Warns on any residual ``{{placeholder}}`` tokens so you can fix the
   transform rules rather than silently shipping broken content.

This tool does NOT regenerate the 9 design references (typography,
color-and-contrast, spatial-design, etc.) — those are copied more-or-less
verbatim during the initial vendor and rarely drift. It also does NOT
touch the main ``SKILL.md`` dispatch block, which is hand-maintained.

When upstream ships updated command bodies, run this script, inspect the
diff, and commit.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_BASE = (
    "https://raw.githubusercontent.com/pbakaus/impeccable/main/source/skills"
)

DST_DIR = Path(__file__).resolve().parent.parent / "references"

COMMANDS = [
    "adapt", "animate", "audit", "bolder", "clarify", "colorize",
    "critique", "delight", "distill", "harden", "layout", "optimize",
    "overdrive", "polish", "quieter", "shape", "typeset",
]

COMMAND_SET = set(COMMANDS)

FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n+", re.DOTALL)

PREP_RE = re.compile(
    r"## MANDATORY PREPARATION\s*\n\s*(?P<body>.*?)\n\s*---\s*\n",
    re.DOTALL,
)

STEP1_PREP_RE = re.compile(
    r"(### Step 1: Preparation\s*\n\s*)(?P<body>.*?)(?=\n###\s)",
    re.DOTALL,
)

ADDITIONAL_GATHER_RE = re.compile(
    r"Additionally gather:\s*(?P<extra>[^\n]+?)\.?\s*$",
    re.MULTILINE,
)

PREAMBLE_NOTE = (
    "> You are continuing a session under the `impeccable` skill; "
    "the design-context protocol and anti-pattern rules already apply."
)

AVAILABLE_COMMANDS_LIST = ", ".join(f"`{c}`" for c in COMMANDS)


def fetch(cmd: str) -> str:
    url = f"{UPSTREAM_BASE}/{cmd}/SKILL.md"
    req = urllib.request.Request(url, headers={"User-Agent": "gobby-impeccable-upgrade"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def substitute_command_refs(text: str) -> str:
    specific = [
        (
            re.compile(r"\{\{command_prefix\}\}impeccable teach"),
            "the impeccable skill's `teach` mode",
        ),
        (
            re.compile(r"\{\{command_prefix\}\}impeccable craft"),
            "the impeccable skill's `craft` mode",
        ),
        (
            re.compile(r"\{\{command_prefix\}\}impeccable extract"),
            "the impeccable skill's `extract` mode",
        ),
        (
            re.compile(r"\{\{command_prefix\}\}impeccable"),
            "the `impeccable` skill",
        ),
    ]
    for pat, repl in specific:
        text = pat.sub(repl, text)

    def inline_cmd_sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "command-name":
            return "`<cmd>`"
        return f"`{name}`"

    text = re.sub(
        r"`\{\{command_prefix\}\}([a-z][a-z0-9-]*)`",
        inline_cmd_sub,
        text,
    )

    def prose_cmd_sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "command-name":
            return "`<cmd>`"
        if name in COMMAND_SET:
            return (
                f"the `{name}` steering command "
                f'(load via `get_skill_file(name="impeccable", '
                f'path="references/{name}.md")` on `gobby-skills`)'
            )
        return f"`{name}` (unknown upstream command)"

    text = re.sub(
        r"\{\{command_prefix\}\}([a-z][a-z0-9-]*)",
        prose_cmd_sub,
        text,
    )

    text = text.replace("{{model}}", "you")
    text = text.replace(
        "{{config_file}}",
        "the project instructions file (e.g. `CLAUDE.md`, `GEMINI.md`, or `AGENTS.md`)",
    )
    text = text.replace("{{ask_instruction}}", "Ask the user")
    text = text.replace("{{available_commands}}", AVAILABLE_COMMANDS_LIST)
    return text


def _extract_extra_clause(body: str) -> str:
    match = ADDITIONAL_GATHER_RE.search(body)
    if not match:
        return ""
    extra = match.group("extra").strip().rstrip(".")
    return f" Additionally gather: {extra}."


def collapse_mandatory_prep(text: str) -> str:
    match = PREP_RE.search(text)
    if match is not None:
        extra = _extract_extra_clause(match.group("body"))
        replacement = f"{PREAMBLE_NOTE}{extra}\n\n---\n"
        return text[: match.start()] + replacement + text[match.end() :]

    step_match = STEP1_PREP_RE.search(text)
    if step_match is not None:
        extra = _extract_extra_clause(step_match.group("body"))
        replacement = f"{step_match.group(1)}{PREAMBLE_NOTE}{extra}\n\n"
        return text[: step_match.start()] + replacement + text[step_match.end() :]

    first_heading = re.search(r"^#{1,6}\s", text, re.MULTILINE)
    if first_heading is None:
        return f"{PREAMBLE_NOTE}\n\n{text}"
    insert_at = first_heading.start()
    return f"{text[:insert_at]}{PREAMBLE_NOTE}\n\n{text[insert_at:]}"


def transform(text: str) -> str:
    text = FRONTMATTER_RE.sub("", text, count=1)
    text = collapse_mandatory_prep(text)
    text = substitute_command_refs(text)
    return text.rstrip() + "\n"


def main() -> int:
    DST_DIR.mkdir(parents=True, exist_ok=True)
    had_warnings = False

    for cmd in COMMANDS:
        try:
            raw = fetch(cmd)
        except urllib.error.URLError as e:
            print(f"[ERROR] {cmd}: fetch failed: {e}", file=sys.stderr)
            return 2

        transformed = transform(raw)

        leftover = re.findall(r"\{\{[a-z_]+\}\}", transformed)
        if leftover:
            print(
                f"[WARN] {cmd}: leftover placeholders: {sorted(set(leftover))}",
                file=sys.stderr,
            )
            had_warnings = True

        dst = DST_DIR / f"{cmd}.md"
        dst.write_text(transformed)
        print(f"wrote {dst.relative_to(Path.cwd())} ({len(transformed)}B)")

    if had_warnings:
        print(
            "\n[!] One or more files had leftover placeholders — "
            "update substitute_command_refs() before committing.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
