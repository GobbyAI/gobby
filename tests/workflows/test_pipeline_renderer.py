"""Tests for pipeline step rendering."""

from gobby.workflows.pipeline.renderer import _filter_env


def test_filter_env_excludes_sensitive_names_and_segments() -> None:
    env = {
        "PASSWORD": "exact-password",
        "token": "lowercase-token",
        "SECRET_STUFF": "prefixed-secret",
        "MY_SECRET_VALUE": "infix-secret",
        "mixed_ToKeN_value": "mixed-case-token",
        "GH_PAT": "token-alias",
        "AWS_SECRET_ACCESS_KEY": "known-sensitive-name",
        "database_url": "case-insensitive-sensitive-name",
        "PATH": "/usr/bin",
        "TOKENIZER_MODEL": "safe-tokenizer",
        "MONKEY": "safe-key-substring",
        "PUBLIC_URL": "https://example.test",
    }

    assert _filter_env(env) == {
        "PATH": "/usr/bin",
        "TOKENIZER_MODEL": "safe-tokenizer",
        "MONKEY": "safe-key-substring",
        "PUBLIC_URL": "https://example.test",
    }


def test_filter_env_explicit_allowlist_can_include_sensitive_names() -> None:
    env = {
        "PASSWORD": "allowed-secret",
        "PATH": "/usr/bin",
        "HOME": "/home/test",
    }

    assert _filter_env(env, frozenset({"PASSWORD", "PATH"})) == {
        "PASSWORD": "allowed-secret",
        "PATH": "/usr/bin",
    }
