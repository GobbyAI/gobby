"""Shared terminal-output redaction tests."""

from pathlib import Path

import pytest

from gobby.utils.terminal_output import redact_terminal_output

pytestmark = pytest.mark.unit


def test_redact_terminal_output_scrubs_secrets() -> None:
    text = (
        "key sk-ABCDEFGHIJKLMNOPQRSTUV and token ghp_ABCDEFGHIJKLMNOPQR "
        "plus github_pat_1234567890_abcdefghijklmnopqrstuv"
    )
    redacted = redact_terminal_output(text)
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in redacted
    assert "sk-<redacted>" in redacted
    assert "ghp_ABCDEFGHIJKLMNOPQR" not in redacted
    assert "github_pat_1234567890_abcdefghijklmnopqrstuv" not in redacted


def test_redact_terminal_output_scrubs_url_credentials() -> None:
    redacted = redact_terminal_output(
        "connect failed: postgresql://gobby:hunter2secret@127.0.0.1:5432/gobby"
    )
    assert "hunter2secret" not in redacted
    assert "postgresql://gobby:<redacted>@127.0.0.1:5432/gobby" in redacted


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        ('{"password":"hunter2secret"}', "hunter2secret", '{"password":<redacted>}'),
        ("{'api_key': 'k3y 9'}", "k3y 9", "{'api_key': <redacted>}"),
        ("password=secret123 next", "secret123", "password=<redacted> next"),
        ("token: ab1", "ab1", "token: <redacted>"),
        ("client_secret=s3cr retry", "s3cr", "client_secret=<redacted> retry"),
        ("password=ab;c,d} retry", "c,d", "password=<redacted> retry"),
        ('{"password":"ab\\"secretTAIL"}', "secretTAIL", '{"password":<redacted>}'),
        ("{'token': 'ab\\'secretTAIL'}", "secretTAIL", "{'token': <redacted>}"),
        ('{"password":"ab\\\\"} next', "ab", '{"password":<redacted>} next'),
        ('body={"password":"hunter2 secretTAIL', "secretTAIL", 'body={"password":<redacted>'),
    ],
)
def test_redact_terminal_output_scrubs_quoted_keys_and_short_values(
    text: str, secret: str, expected: str
) -> None:
    redacted = redact_terminal_output(text)
    assert secret not in redacted
    assert redacted == expected


def test_redact_terminal_output_rewrites_home_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/secrethome"))
    redacted = redact_terminal_output("see /Users/secrethome/.gobby/x")
    assert "/Users/secrethome" not in redacted
    assert "~/.gobby/x" in redacted
