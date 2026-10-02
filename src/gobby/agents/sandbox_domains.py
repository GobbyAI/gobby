"""Outbound domain groups for managed agent sandboxes."""

import functools
import ipaddress
import json
import re
from importlib.resources import files

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")

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
    """Accept only entries SRT's domain rule allows, requiring real hostname labels.

    An entry is ``localhost``, a bracketed IPv6 literal, or two or more
    letter-digit-hyphen labels with an optional leading ``*.``. That refuses
    schemes, paths, ports, ``*``, ``*.com``, whitespace and empty labels.
    """
    if entry.startswith("[") and entry.endswith("]"):
        try:
            ipaddress.IPv6Address(entry[1:-1])
        except ValueError:
            return False
        return True
    if entry == "localhost":
        return True
    labels = entry.removeprefix("*.").split(".")
    return len(labels) >= 2 and all(_HOST_LABEL.fullmatch(label) for label in labels)


@functools.cache
def trusted_domains() -> tuple[str, ...]:
    """Return the vendored Trusted seed in file order; never fetches.

    Raises ``OSError`` when the seed is unreadable and ``ValueError`` when it is not
    ``{"categories": {name: [domain, ...]}}``.
    """
    seed = json.loads(
        files("gobby").joinpath("data/sandbox/trusted_domains.json").read_text(encoding="utf-8")
    )
    categories = seed.get("categories") if isinstance(seed, dict) else None
    if not isinstance(categories, dict) or not all(
        isinstance(domains, list) and all(isinstance(domain, str) for domain in domains)
        for domains in categories.values()
    ):
        raise ValueError("trusted_domains.json must map categories to lists of domains")
    return tuple(domain for domains in categories.values() for domain in domains)
