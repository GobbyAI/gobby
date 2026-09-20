"""Droid native skill expansion must reach immediate help before bootstrap."""

from pathlib import Path

import pytest

from gobby.adapters.droid import DroidAdapter
from gobby.hooks.events import HookEventType

pytestmark = pytest.mark.unit


@pytest.fixture
def router_prompt() -> str:
    router = (
        Path(__file__).parents[2] / "src/gobby/install/shared/skills/gobby/SKILL.md"
    ).read_text()
    body = router[router.index("# Gobby Router\n") :].rstrip()
    return (
        "<system-notification>\n"
        "Skills provide specialized capabilities and domain knowledge. "
        "The user has selected the following skill for immediate execution. "
        "Begin following the skill's instructions now.\n"
        '<skill filePath="/home/user/.agents/skills/gobby/SKILL.md">\n'
        "<name>gobby</name>\n"
        "<description>Router contract for provider-aware Gobby help. (personal)</description>\n"
        f"{body}\n</skill>\n</system-notification>"
    )


@pytest.mark.parametrize("field", ["prompt", "user_prompt"])
@pytest.mark.parametrize("args", ["", "\n\nhelp\n"])
def test_native_router_help(router_prompt: str, field: str, args: str) -> None:
    event = DroidAdapter().translate_to_hook_event(
        {"hook_event_name": "UserPromptSubmit", field: router_prompt + args}
    )
    assert event.event_type is HookEventType.BEFORE_AGENT
    assert event.data["prompt"] == ("/gobby help" if args else "/gobby")


@pytest.mark.parametrize(
    "args", ["\n\ntasks", "\n\nhelp me build", "\n\nskill python", "\n\ntasks references"]
)
def test_explicit_work_stays_intact(router_prompt: str, args: str) -> None:
    prompt = router_prompt + args
    event = DroidAdapter().translate_to_hook_event(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    )
    assert event.data["prompt"] == prompt


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/skills/gobby/", "/skills/other/"),
        ("<name>gobby</name>", "<name>other</name>"),
        ("# Gobby Router\n", "# Custom Router\n"),
        ("<!-- gobby-router:end -->", ""),
        ("</system-notification>", ""),
    ],
)
def test_other_or_incomplete_expansions_stay_intact(router_prompt: str, old: str, new: str) -> None:
    prompt = router_prompt.replace(old, new)
    event = DroidAdapter().translate_to_hook_event(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    )
    assert event.data["prompt"] == prompt


def test_router_in_other_event_is_not_a_help_request(router_prompt: str) -> None:
    event = DroidAdapter().translate_to_hook_event(
        {"hook_event_name": "PostToolUse", "prompt": router_prompt}
    )
    assert event.data["prompt"] == router_prompt
