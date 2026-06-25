"""Golden-render lock for the shared ``handoff/session_end`` prompt.

This prompt is the single source of truth for the session handoff summary on both
sides of the platform:

* the daemon renders it via ``gobby.llm.prompt_rendering.render_summary_prompt``
  (Jinja2), reading the body that ``sync_bundled_prompts`` seeded into the hub; and
* standalone gwiki renders the *same installed file bytes* via Rust ``minijinja``
  (Workstream D).

For the two engines to stay byte-identical the template is constrained to the pure
``{{ variable }}`` substitution subset that Jinja2 and minijinja render the same
way, and the rendered output is pinned to a committed golden fixture.

The matching gwiki test (``crates/gwiki``) renders the same file body with the same
``GOLDEN_CONTEXT`` through minijinja and asserts equality with the same golden
bytes. If you change the prompt body you MUST regenerate
``fixtures/handoff_session_end_golden.md`` *and* re-verify the gwiki golden test, or
daemon and gwiki session summaries will diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gobby.llm.prompt_rendering import render_summary_prompt
from gobby.prompts.models import parse_frontmatter
from gobby.prompts.sync import get_bundled_prompts_path

pytestmark = pytest.mark.unit

PROMPT_PATH = get_bundled_prompts_path() / "handoff" / "session_end.md"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "handoff_session_end_golden.md"

# Variable contract. The union below is exactly the set of ``{{ variables }}`` the
# body interpolates; standalone gwiki must supply every one of them.
REQUIRED_VARIABLES = [
    "transcript_summary",
    "last_messages",
    "git_status",
    "file_changes",
]
OPTIONAL_VARIABLES = [
    "structured_context",
    "git_diff_summary",
    "claimed_tasks",
    "session_memories",
    "first_digest_turn",
    "recent_digest_turns",
]

# Deterministic, already-stringified context. Passing plain strings makes
# ``_format_summary_context`` an identity, so the Jinja2 render here and the
# minijinja render in gwiki produce identical bytes from identical inputs.
GOLDEN_CONTEXT = {name: f"<<{name}>>" for name in REQUIRED_VARIABLES + OPTIONAL_VARIABLES}


def _frontmatter_and_body() -> tuple[dict[str, object], str]:
    """Return (frontmatter, body) using the same extraction as ``sync_bundled_prompts``.

    ``sync_bundled_prompts`` seeds ``content = body.strip()`` into the hub, so the
    daemon renders ``body.strip()``. Mirror that here so the golden lock pins the
    exact bytes the daemon (and gwiki) render.
    """
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw)
    return frontmatter, body.strip()


def test_prompt_file_exists() -> None:
    assert PROMPT_PATH.exists(), f"Expected shared prompt at {PROMPT_PATH}"


def test_frontmatter_variable_contract() -> None:
    """required/optional variable lists are locked; gwiki depends on this exact set."""
    frontmatter, _ = _frontmatter_and_body()
    assert frontmatter.get("required_variables") == REQUIRED_VARIABLES
    assert frontmatter.get("optional_variables") == OPTIONAL_VARIABLES


def test_body_uses_only_shared_substitution_subset() -> None:
    """Body must stay within the pure ``{{ var }}`` subset Jinja2 and minijinja share.

    No statement blocks (``{% %}``), template comments (``{# #}``), or filters
    (``|``): those risk cross-engine drift. Broadening the subset requires verifying
    minijinja parity and regenerating the golden fixture.
    """
    _, body = _frontmatter_and_body()
    assert "{%" not in body, "statement blocks are outside the shared subset"
    assert "{#" not in body, "template comments are outside the shared subset"

    tags = re.findall(r"\{\{(.*?)\}\}", body, flags=re.DOTALL)
    assert tags, "expected {{ variable }} substitutions in the body"

    declared = set(REQUIRED_VARIABLES) | set(OPTIONAL_VARIABLES)
    used: set[str] = set()
    for tag in tags:
        token = tag.strip()
        assert "|" not in token, f"filters are outside the shared subset: {{{{{tag}}}}}"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token), (
            f"only bare variable names are allowed, got {{{{{tag}}}}}"
        )
        used.add(token)
    assert used == declared, (
        f"body variables {sorted(used)} must match declared contract {sorted(declared)}"
    )


def test_golden_render_is_byte_locked() -> None:
    """``render_summary_prompt(body, GOLDEN_CONTEXT)`` is pinned to the golden fixture.

    This is the cross-repo contract: gwiki's minijinja render of the same body and
    context must equal these exact bytes. Regenerate the fixture only when the prompt
    intentionally changes (and re-verify the gwiki side).
    """
    _, body = _frontmatter_and_body()
    rendered = render_summary_prompt(body, GOLDEN_CONTEXT)
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    assert rendered == expected
