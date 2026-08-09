"""Contract tests for universal cross-session messaging guidance."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTION_FILES = (
    PROJECT_ROOT / "AGENTS.md",
    PROJECT_ROOT / "CLAUDE.md",
    PROJECT_ROOT / "QWEN.md",
    PROJECT_ROOT / "GUIDING_PRINCIPLES.md",
)
P2P_PRINCIPLE = (
    "18. **ALWAYS use `gobby-agents:send_message` for direct cross-session agent "
    "communication.** Reserve `gobby-sessions:send_keys` for terminal control."
)
RULE_8_ANCHOR = "command, diagnostics, and affected paths via `gobby-agents:send_message`"


def test_universal_instruction_files_require_p2p_messaging() -> None:
    for path in INSTRUCTION_FILES:
        content = path.read_text(encoding="utf-8")
        normalized_content = " ".join(content.split())

        assert P2P_PRINCIPLE in content, path.name
        assert RULE_8_ANCHOR in normalized_content, path.name
