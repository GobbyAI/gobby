"""Outbound domain groups for managed agent sandboxes."""

import functools
import ipaddress
import json
from importlib.resources import files

GIT_DOMAINS = (
    "github.com",
    "*.github.com",
    "gitlab.com",
    "*.gitlab.com",
    "bitbucket.org",
    "*.bitbucket.org",
)

PACKAGE_REGISTRY_DOMAINS = (
    "registry.npmjs.org",
    "*.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "static.rust-lang.org",
    "proxy.golang.org",
    "sum.golang.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "rubygems.org",
    "api.nuget.org",
    "repo.packagist.org",
    "pub.dev",
    "repo.hex.pm",
    "luarocks.org",
    "deps.files.ghostty.org",
)


def is_srt_domain_pattern(entry: str) -> bool:
    """Mirror SRT's domain rule: host, ``*.`` plus two labels, localhost, or ``[IPv6]``."""
    if entry.startswith("[") and entry.endswith("]"):
        try:
            ipaddress.IPv6Address(entry[1:-1])
        except ValueError:
            return False
        return True
    if "/" in entry or ":" in entry:
        return False
    if entry == "localhost":
        return True
    if entry.startswith("*."):
        parts = entry[2:].split(".")
        return len(parts) >= 2 and all(parts)
    if "*" in entry:
        return False
    return "." in entry and not entry.startswith(".") and not entry.endswith(".")


@functools.cache
def trusted_domains() -> tuple[str, ...]:
    """Return the vendored Trusted seed in file order; never fetches, raises if missing."""
    seed = json.loads(
        files("gobby").joinpath("data/sandbox/trusted_domains.json").read_text(encoding="utf-8")
    )
    return tuple(domain for domains in seed["categories"].values() for domain in domains)
