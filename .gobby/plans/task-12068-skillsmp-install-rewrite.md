# SkillsMP install-from-hub rewrite (get_skill_details + download_skill)

## Overview

`SkillsMPProvider.get_skill_details()` and `SkillsMPProvider.download_skill()` in
`src/gobby/skills/hubs/skillsmp.py` call endpoints that don't exist on
skillsmp.com (`GET /skills/{slug}` and `GET /skills/{slug}/download` — both
404). The real API, confirmed against `https://skillsmp.com/openapi.json`,
exposes only `GET /api/v1/skills/search`, `GET /api/v1/skills/ai-search`, and
`GET /api/health`. Search results carry a `githubUrl` field — SkillsMP is an
index into GitHub-hosted skills, not a ZIP CDN. This plan rewrites the two
broken methods to thread `githubUrl` through the data model, derive details
from `/skills/search`, and fetch skill contents from GitHub via the Contents
API. Outcome: `install_skill(hub_name="skillsmp", slug=...)` succeeds
end-to-end via the existing `/hubs/install` route and `install_skill` MCP tool.

## Constraints

- Keep the `HubProvider.get_skill_details` / `download_skill` abstract
  signatures unchanged. The rewrite is internal to `SkillsMPProvider`; no route,
  storage, migration, or MCP-tool-surface changes.
- Preserve the defensive envelope helper `_unwrap_skills` and the
  None-on-error semantics of `get_skill_details` (the route needs to
  distinguish "not found" from "exploded").
- Do not send the SkillsMP auth token to GitHub. Separate hosts, separate
  credential models.
- All new HTTP calls must be testable with `respx` / mocked httpx — no
  `subprocess git clone` in the SkillsMP download path (the repo's
  `GitHubCollectionProvider` uses `clone_skill_repo` which is fine for it, but
  SkillsMP installs need to be HTTP-mockable).
- Path safety: reject any Contents API entry whose name/path is absolute or
  contains `..`; cap per-file size at 10 MB; cap recursion depth at 3.
- `githubUrl` is not guaranteed on every record. **Do not** fall back to
  `skillUrl` — `skillUrl` is the SkillsMP detail-page URL
  (`https://skillsmp.com/skills/{id}`), not a fetchable source. Falling back
  to it would only manufacture a downstream "non-GitHub host" failure with a
  worse error message than the explicit "no source URL" guard.
- 19 existing tests in `tests/skills/hubs/test_skillsmp.py` must still pass
  after the rewrite (minus `test_download_no_url_returns_error` which targets
  the deleted endpoint-shaped path and is replaced with new-flow equivalents).

## Phase 1: Data Model Thread-Through

**Goal**: Add a generic `source_url` field to `HubSkillInfo` so hubs that can
resolve a skill to a fetchable URL can surface it without per-provider
signature extensions.

### 1.1 Add source_url to HubSkillInfo [category: code]

Target: `src/gobby/skills/hubs/base.py`

Extend the `HubSkillInfo` dataclass with a new optional field `source_url`,
default `None`. `HubSkillDetails` inherits the field via dataclass inheritance
so no explicit addition there. Extend `HubSkillInfo.to_dict()` to include the
key.

Current class (base.py:49–79):

```python
@dataclass
class HubSkillInfo:
    slug: str
    display_name: str
    description: str
    hub_name: str
    version: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "hub_name": self.hub_name,
            "version": self.version,
            "score": self.score,
        }
```

Target shape after the edit:

```python
@dataclass
class HubSkillInfo:
    slug: str
    display_name: str
    description: str
    hub_name: str
    version: str | None = None
    score: float | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "description": self.description,
            "hub_name": self.hub_name,
            "version": self.version,
            "score": self.score,
            "source_url": self.source_url,
        }
```

The field is additive with a safe default — existing callers (ClawdHub,
GitHubCollection, ClaudePlugins, SkillsMP search/list) are not required to
populate it and will leave it as `None`. The route layer and MCP tool
`to_dict()` consumers are not broken by the new key.

Update the docstring's Attributes block in `HubSkillInfo` to mention
`source_url: Hub-provided URL to fetch the skill from (when known)`.

`HubSkillDetails` needs no source edit because the dataclass inherits
`source_url`; its `to_dict()` currently calls `super().to_dict()` and merges
`latest_version` + `versions` — that already picks up the new key via super.

Verify the field is picked up by `HubSkillInfo(...).to_dict()` round-tripping
through any existing callers (list_hubs MCP tool, /hubs/search route); no
behavioral change expected because the key is new.

Validation criteria: `HubSkillInfo(slug="x", display_name="x",
description="x", hub_name="h", source_url="https://x.test/y")` constructs
without error; `to_dict()["source_url"] == "https://x.test/y"`; default when
unspecified is `None`.

## Phase 2: SkillsMPProvider Rewrite

**Goal**: Make `install_skill(hub_name="skillsmp", slug=...)` succeed
end-to-end. Route `get_skill_details` through `/skills/search`, replace the ZIP
download path with a GitHub Contents API fetch, thread `githubUrl` through
`HubSkillDetails.source_url`.

### 2.1 Rewrite SkillsMPProvider.get_skill_details() and _skill_to_info [category: code] (depends: 1.1)

Target: `src/gobby/skills/hubs/skillsmp.py`

Replace the current `get_skill_details()` (L177-200) that calls the
non-existent `GET /skills/{slug}` with a search-based derivation. Also extend
`_skill_to_info()` (L108-126) so search results carry `source_url` (and by
extension, `list_skills()` and `search()` benefit too).

New `_skill_to_info()`:

```python
def _skill_to_info(self, skill: dict[str, Any]) -> HubSkillInfo:
    """Map a SkillsMP skill record to HubSkillInfo.

    SkillsMP uses ``stars`` as the popularity signal — surfaced as ``score``
    so the MCP layer ranks consistently with other hubs. ``githubUrl``
    populates ``source_url`` so callers can resolve the skill to a GitHub
    location without a second round-trip. ``skillUrl`` is **not** used as a
    fallback because it points at the SkillsMP detail page, not a fetchable
    source. ``version`` is not provided by the list/search endpoints today.
    """
    stars = skill.get("stars")
    score = (
        float(stars)
        if isinstance(stars, int | float) and not isinstance(stars, bool)
        else None
    )
    source_url = skill.get("githubUrl") or None
    return HubSkillInfo(
        slug=skill.get("id", skill.get("name", "")),
        display_name=skill.get("name", skill.get("id", "")),
        description=skill.get("description", ""),
        hub_name=self.hub_name,
        version=skill.get("version"),
        score=score if score is not None else skill.get("score"),
        source_url=source_url,
    )
```

New `_skill_to_details()` (new helper) plus rewritten `get_skill_details()`:

```python
def _skill_to_details(self, skill: dict[str, Any]) -> HubSkillDetails:
    """Map a SkillsMP skill record to HubSkillDetails.

    The real API's ``Skill`` schema does not carry an explicit version list.
    Populate ``version`` / ``latest_version`` from the record when present;
    leave ``versions`` empty when absent.
    """
    stars = skill.get("stars")
    score = (
        float(stars)
        if isinstance(stars, int | float) and not isinstance(stars, bool)
        else None
    )
    source_url = skill.get("githubUrl") or None
    version = skill.get("version")
    versions_raw = skill.get("versions")
    versions = list(versions_raw) if isinstance(versions_raw, list) else (
        [version] if version else []
    )
    return HubSkillDetails(
        slug=skill.get("id", skill.get("name", "")),
        display_name=skill.get("name", skill.get("id", "")),
        description=skill.get("description", ""),
        hub_name=self.hub_name,
        version=version,
        score=score if score is not None else skill.get("score"),
        source_url=source_url,
        latest_version=skill.get("latest_version", version),
        versions=versions,
    )

async def get_skill_details(
    self,
    slug: str,
) -> HubSkillDetails | None:
    """Derive full skill details from /skills/search.

    SkillsMP has no per-skill endpoint (GET /skills/{slug} returns 404).
    Query search with the slug, filter for an exact ``id`` match, and map
    the record. Returns None when no exact match exists or when the upstream
    request fails (parity with prior behavior).
    """
    if not self.auth_token:
        raise RuntimeError(
            "SkillsMP API key not configured. "
            "Run 'gobby install' or 'gobby secrets set SKILLSMP_API_KEY'."
        )
    try:
        result = await self._make_request(
            method="GET",
            endpoint="/skills/search",
            params={"q": slug, "limit": 10},
        )
    except RuntimeError:
        return None

    for skill in self._unwrap_skills(result):
        if skill.get("id") == slug or skill.get("slug") == slug:
            return self._skill_to_details(skill)
    return None
```

Behavioral contract:

- Empty search response → `None`.
- Non-empty search response without an exact slug match → `None` (do not
  fuzzy-match; install-from-hub with the wrong slug should fail cleanly).
- Exact match record missing `githubUrl` → `source_url = None` (download will
  fail clearly at its own guard; `get_skill_details` itself still returns the
  populated details). `skillUrl` is **not** a fallback — it points at the
  SkillsMP detail page (e.g. `https://skillsmp.com/skills/foo`), not a
  fetchable source. Surfacing it as `source_url` would only generate a bogus
  "non-GitHub host" failure downstream with a worse error message.
- `RuntimeError` from `_make_request` (404, 500, network) → `None`.
- Missing auth → raises `RuntimeError` with the canonical message (parity
  with `search()` and `list_skills()`).

Imports unchanged for this task.

Validation criteria: `get_skill_details("known-slug")` returns a populated
`HubSkillDetails` when mocked `_make_request` returns a search envelope
containing a record with `id == "known-slug"` and `githubUrl`; returns `None`
when the mocked envelope contains no exact match; raises `RuntimeError`
matching `"API key not configured"` when `auth_token is None`. Existing
`_skill_to_info`-driven tests (`search`, `list_skills`) continue to pass
because the new `source_url` field is additive.

Test scenarios (auto-wrapped by TDD):

- `get_skill_details_returns_details_from_search` — mock envelope with one
  matching record; assert `HubSkillDetails` populated including `source_url`
  from `githubUrl`.
- `get_skill_details_returns_none_when_no_exact_match` — envelope with skills
  but no matching `id`.
- `get_skill_details_drops_skillUrl_in_source_url` — record missing
  `githubUrl` but carrying `skillUrl`; assert `details.source_url is None`
  (no fallback).
- `get_skill_details_returns_none_on_runtime_error` — `_make_request` raises.
- `get_skill_details_raises_without_api_key` — parity guard.
- `get_skill_details_returns_none_when_search_empty` — envelope with empty
  skills list.
- `_skill_to_info_populates_source_url` — via the existing `search()` test
  fixtures, assert `HubSkillInfo.source_url` is set when `githubUrl` is in
  the record.

### 2.2 Rewrite SkillsMPProvider.download_skill() with GitHub Contents API [category: code] (depends: 2.1)

Target: `src/gobby/skills/hubs/skillsmp.py`

Replace the current `download_skill()` (L202-231) that calls the non-existent
`GET /skills/{slug}/download` with a flow that resolves the skill to a GitHub
location (via `get_skill_details`) and fetches the directory contents via the
GitHub Contents API.

New imports required at module top:

```python
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo
```

(Remove `zipfile` and `BytesIO` imports — they were only used by the deleted
`_download_and_extract`. See task 2.3. Add `dataclasses.dataclass` because the
new `_GithubDirSource` / `_GithubFileSource` types use the decorator and the
existing module does not currently import it; without this import the module
fails to load before mypy/tests can run.)

New module-level constants — each value is justified, not arbitrary:

```python
_GITHUB_API_HOST = "api.github.com"
_GITHUB_RAW_HOST = "raw.githubusercontent.com"

# 30s for GitHub Contents API: typical p99 < 5s; 30s leaves headroom for slow
# networks without making install hangs feel indefinite.
_GITHUB_API_TIMEOUT = 30.0

# 60s for raw-content GETs: max-cap file is 10 MB; even at 200 KB/s that
# completes in ~50s. Beyond 60s the download is considered hung.
_GITHUB_RAW_TIMEOUT = 60.0

# 10 MB per-file ceiling: SKILL.md and reference docs are KB-scale; the
# largest legitimate skill payload observed in the SkillsMP index is ~5 MB
# (specialized skills bundling small reference data). 10 MB is a 2x safety
# margin. Larger files indicate either unintended bundling or a malicious
# payload — fail closed.
_MAX_FILE_BYTES = 10 * 1024 * 1024

# Recursion depth cap of 3: skill structure is `skill-root/{SKILL.md,
# scripts/X.py, references/Y.md}` — depth 0 is the skill root, depth 1 is
# `scripts/` or `references/`, depth 2 is files inside those. Depth 3 is a
# margin for occasional `references/topic-name/file.md` shapes. Deeper trees
# indicate either accidental nested-skill collection or a fork-bomb payload
# — fail closed.
_MAX_TREE_DEPTH = 3

# GitHub identifier grammars. Owner/repo come from URL parsing and must match
# GitHub's public rules; ref/path-segment grammars are deliberately tighter
# than GitHub's full ref grammar to avoid disambiguation traps in /blob//tree/
# URLs (see _parse_github_url).
#
# Owner: 1-39 chars, alphanumerics and hyphens, must start AND end with
# alphanumeric. Single-char usernames are allowed. Reject trailing hyphen,
# leading hyphen, embedded `..`, etc. Matches GitHub's actual constraints.
_OWNER_RE = re.compile(r'^[A-Za-z0-9]$|^[A-Za-z0-9][A-Za-z0-9-]{0,37}[A-Za-z0-9]$')

# Repo: 1-100 chars, alphanumerics + `._-`, must start and end with
# alphanumeric or underscore (rejects `.repo`, `repo.`, `..repo`,
# `repo--` etc). Single-char repos allowed.
_REPO_RE = re.compile(r'^[A-Za-z0-9_]$|^[A-Za-z0-9_][A-Za-z0-9._-]{0,98}[A-Za-z0-9_]$')

# Ref: 1-255 chars, no slashes (see grammar note); also see _validate_ref for
# additional reserved-sequence checks (`..`, leading dot/hyphen, control chars).
_REF_RE = re.compile(r'^[A-Za-z0-9._-]{1,255}$')

# Path segment: same shape as ref. Rejects `..` etc via _validate_ref-equivalent
# checks performed centrally in _parse_github_url.
_PATH_SEGMENT_RE = re.compile(r'^[A-Za-z0-9._-]{1,255}$')
```

New private dataclasses (scoped to this module) — two-variant source ADT so
`download_skill` can branch cleanly between directory installs (Contents API
recursion) and single-file installs (one raw-content GET):

```python
@dataclass(frozen=True)
class _GithubDirSource:
    """Skill is a directory in a GitHub repo; fetch via Contents API."""

    owner: str
    repo: str
    ref: str  # validated against _REF_RE; no slashes (see grammar note)
    path: str  # dir path within the repo; "" for repo root


@dataclass(frozen=True)
class _GithubFileSource:
    """Skill is a single SKILL.md file accessed via raw.githubusercontent.com.

    Used when the SkillsMP record points at a raw URL or a /blob/ URL whose
    leaf is SKILL.md. We GET the URL directly — no Contents API call, no
    sibling-discovery — and write it as `<extract_dir>/SKILL.md`.
    """

    raw_url: str  # full https://raw.githubusercontent.com/... URL
    ref: str  # ref segment recovered from the URL, surfaced on DownloadResult
```

Type alias: `_GithubSource = _GithubDirSource | _GithubFileSource`.

**Grammar note on refs and slashes.** GitHub branch names can contain slashes
(e.g. `feature/foo`). In `/blob/{ref}/{path}` and `/tree/{ref}/{path}` URLs,
and equivalently in `raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}`,
that ambiguates where the ref ends and the path begins. Without an extra API
call (`GET /repos/{o}/{r}/branches`) there is no reliable way to disambiguate
even with the `refs/heads/` prefix — `refs/heads/feature/foo/path/SKILL.md`
could mean ref `refs/heads/feature` + path `foo/path/SKILL.md`, or ref
`refs/heads/feature/foo` + path `path/SKILL.md`. This rewrite **rejects refs
containing slashes across all URL shapes** (`_REF_RE` excludes `/`). Skills
hosted on slash-containing branches are explicitly unsupported via SkillsMP
URL records and must surface a commit-SHA or single-segment-ref URL instead.
Filing a follow-up task to revisit if real-world records require it.

New helper `_validate_ref()` (module-level, used by both URL parsing and
caller-supplied `version_override`):

```python
def _validate_ref(ref: str, *, allow_slashes: bool = False) -> str:
    """Validate a GitHub ref against our conservative grammar.

    Rejects empty, control-chars, leading dots/dashes, ``..``, and (unless
    ``allow_slashes``) any slash. Returns the ref unchanged on success.
    """
    if not ref:
        raise ValueError("Empty ref")
    if ref.startswith((".", "-")) or ref.endswith("."):
        raise ValueError(f"Ref starts/ends with reserved char: {ref!r}")
    if ".." in ref or "\x00" in ref or any(c.isspace() for c in ref):
        raise ValueError(f"Ref contains reserved sequence: {ref!r}")
    if allow_slashes:
        if not all(_REF_RE.match(seg) for seg in ref.split("/") if seg):
            raise ValueError(f"Ref segment fails grammar: {ref!r}")
    else:
        if not _REF_RE.match(ref):
            raise ValueError(f"Ref fails grammar (no slashes allowed here): {ref!r}")
    return ref
```

New helper `_parse_github_url()`:

```python
def _parse_github_url(
    self,
    source_url: str,
    version_override: str | None = None,
) -> "_GithubSource":
    """Parse a SkillsMP source_url into a _GithubDirSource or _GithubFileSource.

    Accepts:
      - https://github.com/{owner}/{repo}                         → DirSource (root, ref=HEAD)
      - https://github.com/{owner}/{repo}/tree/{ref}/{path...}    → DirSource
      - https://github.com/{owner}/{repo}/blob/{ref}/{path}/SKILL.md
            → FileSource (rewritten to raw.githubusercontent.com)
      - https://github.com/{owner}/{repo}/{path...}/SKILL.md
            → FileSource at HEAD (ref-less GitHub HTML URL — observed in
              SkillsMP records per parent #12068. Treated as a single-file
              fetch from the default branch via Contents API.)
      - https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}/SKILL.md
            → FileSource (used as-is). ``{ref}`` MUST be a single segment
              (commit SHA or no-slash branch/tag name); slash-containing
              branches are rejected (see grammar note above).

    Path segments are percent-decoded then validated against
    ``_PATH_SEGMENT_RE``. Refs are validated against ``_REF_RE`` (no slashes
    anywhere). ``version_override`` is validated against the same grammar
    before substitution.

    Raises ValueError for unsupported hosts, malformed shapes, refs with
    slashes, file URLs whose leaf is not ``SKILL.md``, or any path segment
    failing the grammar.
    """
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if host == "www.github.com":
        host = "github.com"
    raw_segments = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if any("\x00" in s or s in ("", ".", "..") for s in raw_segments):
        raise ValueError(f"Path contains reserved segment: {source_url}")

    if host == "github.com":
        return self._parse_github_html_url(raw_segments, version_override, source_url)
    if host == _GITHUB_RAW_HOST:
        return self._parse_github_raw_url(raw_segments, version_override, source_url)
    raise ValueError(
        f"Unsupported source host {host!r}; expected github.com or {_GITHUB_RAW_HOST}"
    )


def _parse_github_html_url(
    self,
    parts: list[str],
    version_override: str | None,
    source_url: str,
) -> "_GithubSource":
    if len(parts) < 2:
        raise ValueError(f"Malformed GitHub URL, missing owner/repo: {source_url}")
    owner, repo = parts[0], parts[1]
    if not _OWNER_RE.match(owner):
        raise ValueError(f"Invalid GitHub owner: {owner!r}")
    if not _REPO_RE.match(repo):
        raise ValueError(f"Invalid GitHub repo: {repo!r}")

    if len(parts) == 2:
        ref = _validate_ref(version_override) if version_override else 'HEAD'
        return _GithubDirSource(owner=owner, repo=repo, ref=ref, path='')

    # Three accepted shapes after owner/repo:
    #   - parts[2] in ('blob', 'tree') with ref at parts[3] and path at parts[4:]
    #   - parts[2] is anything else (no /blob/ or /tree/ marker) AND the URL
    #     ends in SKILL.md → R2-F1: treat as ref-less file URL at HEAD.
    if parts[2] in ('blob', 'tree'):
        kind = parts[2]
        if len(parts) < 4:
            raise ValueError(f'Malformed {kind} URL, missing ref: {source_url}')
        raw_ref = version_override if version_override else parts[3]
        ref = _validate_ref(raw_ref)  # no slashes — see grammar note
        path_segments = parts[4:]
        for seg in path_segments:
            if not _PATH_SEGMENT_RE.match(seg):
                raise ValueError(f'Invalid path segment: {seg!r}')
        if kind == 'blob':
            if not path_segments or path_segments[-1] != 'SKILL.md':
                leaf = path_segments[-1] if path_segments else '<empty>'
                raise ValueError(f'Blob URL leaf must be SKILL.md, got: {leaf}')
            raw_path = '/'.join(path_segments)
            raw_url = f'https://{_GITHUB_RAW_HOST}/{owner}/{repo}/{ref}/{raw_path}'
            return _GithubFileSource(raw_url=raw_url, ref=ref)
        return _GithubDirSource(
            owner=owner, repo=repo, ref=ref, path='/'.join(path_segments),
        )

    # R2-F1: ref-less file URL shape `github.com/{owner}/{repo}/{path...}/SKILL.md`.
    # Real GitHub HTML URLs always have /blob/ or /tree/, but the parent task
    # observed SkillsMP records with this shape (synthesized from raw repo
    # state). Treat as single-file fetch at HEAD via raw.github with ref
    # resolved to the default branch by GitHub's redirect from
    # `raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}` (raw.github
    # accepts `HEAD` as a ref alias for the default branch). version_override
    # may pin a specific ref.
    path_segments = parts[2:]
    for seg in path_segments:
        if not _PATH_SEGMENT_RE.match(seg):
            raise ValueError(f'Invalid path segment: {seg!r}')
    if not path_segments or path_segments[-1] != 'SKILL.md':
        raise ValueError(
            f'Ref-less GitHub file URL leaf must be SKILL.md: {source_url}'
        )
    ref = _validate_ref(version_override) if version_override else 'HEAD'
    raw_path = '/'.join(path_segments)
    raw_url = f'https://{_GITHUB_RAW_HOST}/{owner}/{repo}/{ref}/{raw_path}'
    return _GithubFileSource(raw_url=raw_url, ref=ref)


def _parse_github_raw_url(
    self,
    parts: list[str],
    version_override: str | None,
    source_url: str,
) -> '_GithubFileSource':
    # Raw form: /{owner}/{repo}/{ref}/{path...}/SKILL.md
    # `ref` MUST be a single segment (commit SHA or no-slash branch/tag).
    # The `refs/heads/<branch>/...` form was dropped in round 2 because
    # multi-segment ref/path disambiguation is unreliable without an API
    # call, and partial extraction silently surfaced wrong refs (e.g.
    # `refs/heads/feature/foo/path/SKILL.md` would extract ref
    # `refs/heads/feature` and path `foo/path/SKILL.md`).
    if len(parts) < 4 or parts[-1] != 'SKILL.md':
        raise ValueError(
            f'raw.githubusercontent.com URL must end in SKILL.md and include '
            f'owner/repo/ref: {source_url}'
        )
    owner, repo = parts[0], parts[1]
    if not _OWNER_RE.match(owner):
        raise ValueError(f'Invalid GitHub owner: {owner!r}')
    if not _REPO_RE.match(repo):
        raise ValueError(f'Invalid GitHub repo: {repo!r}')

    # Reject the multi-segment refs/heads/refs/tags shape explicitly so the
    # error message is clear about the unsupported case.
    if parts[2] == 'refs':
        raise ValueError(
            f'Multi-segment refs (refs/heads/..., refs/tags/...) are not '
            f'supported in raw URLs because ref/path disambiguation requires '
            f'an API call. Use a commit SHA or a no-slash branch/tag name. '
            f'Source: {source_url}'
        )

    raw_ref = version_override if version_override else parts[2]
    ref = _validate_ref(raw_ref)  # no slashes
    path_segments = parts[3:]
    for seg in path_segments:
        if not _PATH_SEGMENT_RE.match(seg):
            raise ValueError(f'Invalid path segment: {seg!r}')
    if not path_segments or path_segments[-1] != 'SKILL.md':
        raise ValueError(f'Raw URL leaf must be SKILL.md: {source_url}')

    raw_path = '/'.join(path_segments)
    raw_url = f'https://{_GITHUB_RAW_HOST}/{owner}/{repo}/{ref}/{raw_path}'
    return _GithubFileSource(raw_url=raw_url, ref=ref)
```

New helpers `_assert_safe_entry_name`, `_assert_safe_entry_path`,
`_fetch_github_dir`, `_fetch_github_file`, `_write_file_safely`:

```python
@staticmethod
def _assert_safe_entry_name(name: str) -> None:
    # R2-F3: filename safety enforced upfront, before skip-or-process logic.
    # Mirrors _write_file_safely's checks so we fail loud at the API
    # response boundary rather than after a recursive descent or write.
    BAD_NAMES = ('', '.', '..')
    if name in BAD_NAMES or '/' in name or '\\' in name or '\x00' in name:
        raise RuntimeError(f'Unsafe entry name from Contents API: {name!r}')


@staticmethod
def _assert_safe_entry_path(entry_path: str) -> None:
    # R2-F3: also validate Contents API's `path` field. Required by parent
    # constraints and protects against API responses that disagree with
    # `name` (defense-in-depth — Contents API normally keeps them aligned).
    if entry_path.startswith('/') or '\x00' in entry_path:
        raise RuntimeError(f'Unsafe entry path from Contents API: {entry_path!r}')
    for seg in entry_path.split('/'):
        if seg in ('', '.', '..'):
            raise RuntimeError(f'Unsafe segment in entry path: {entry_path!r}')


async def _fetch_github_dir(
    self,
    client: httpx.AsyncClient,
    ref: _GithubDirSource,
    rel_path: str,
    target_root: Path,
    depth: int,
) -> None:
    """Fetch a directory tree from GitHub Contents API into target_root.

    rel_path is the path relative to the skill root (ref.path). depth tracks
    recursion; aborts past _MAX_TREE_DEPTH.
    """
    if depth > _MAX_TREE_DEPTH:
        raise RuntimeError(
            f"GitHub tree depth exceeded {_MAX_TREE_DEPTH} levels at {rel_path}"
        )
    full_path = (
        f"{ref.path}/{rel_path}".strip("/") if ref.path else rel_path.strip("/")
    )
    url = f"https://{_GITHUB_API_HOST}/repos/{ref.owner}/{ref.repo}/contents/{full_path}"
    params = {"ref": ref.ref} if ref.ref and ref.ref != "HEAD" else None
    response = await client.get(
        url,
        headers={"Accept": "application/vnd.github.v3+json"},
        params=params,
        timeout=_GITHUB_API_TIMEOUT,
    )
    response.raise_for_status()
    entries = response.json()
    if not isinstance(entries, list):
        raise RuntimeError(
            f"GitHub Contents API returned non-listing at {full_path}"
        )
    for entry in entries:
        # R2-F3: validate name AND entry-reported path BEFORE any
        # skip/process decision. Hidden-file skipping is a separate policy
        # applied AFTER the safety check so a malicious entry like
        # `name='..'` cannot bypass the safety net via the dotfile fast-path.
        name = entry.get('name', '')
        entry_path = entry.get('path', '')
        self._assert_safe_entry_name(name)
        if entry_path:
            self._assert_safe_entry_path(entry_path)
        if name.startswith('.'):
            continue  # skip dotfiles (.git, .DS_Store, etc.) post-validation
        entry_type = entry.get('type')
        if entry_type == 'file':
            download_url = entry.get("download_url")
            if not download_url:
                continue
            # F6: validate Contents-API-reported size before fetching.
            size = entry.get("size")
            if not isinstance(size, int) or size < 0:
                raise RuntimeError(
                    f"GitHub Contents API returned invalid size for {name}: {size!r}"
                )
            if size > _MAX_FILE_BYTES:
                raise RuntimeError(
                    f"File {name} exceeds {_MAX_FILE_BYTES}-byte cap ({size} bytes)"
                )
            # F5: validate download_url host before fetching.
            self._assert_raw_host(download_url)
            content = await self._fetch_github_file(client, download_url)
            # F6 belt-and-suspenders: enforce again on the actual body.
            if len(content) > _MAX_FILE_BYTES:
                raise RuntimeError(
                    f"File {name} body exceeded {_MAX_FILE_BYTES}-byte cap "
                    f"({len(content)} bytes)"
                )
            self._write_file_safely(target_root, rel_path, name, content)
        elif entry_type == "dir":
            await self._fetch_github_dir(
                client,
                ref,
                f"{rel_path}/{name}".strip("/"),
                target_root,
                depth + 1,
            )
        # Ignore symlinks, submodules.

@staticmethod
def _assert_raw_host(url: str) -> None:
    """Reject any URL not served by raw.githubusercontent.com.

    Used as a trust-boundary check before fetching file content. Contents
    API consistently returns ``download_url`` values on
    raw.githubusercontent.com; anything else indicates either a GitHub
    change we need to consciously accept or an exfiltration attempt via a
    malicious record.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError(f"Refusing non-https download URL: {url}")
    if parsed.netloc.lower() != _GITHUB_RAW_HOST:
        raise RuntimeError(
            f"Refusing download URL outside {_GITHUB_RAW_HOST}: {parsed.netloc}"
        )


async def _fetch_github_file(
    self,
    client: httpx.AsyncClient,
    download_url: str,
) -> bytes:
    """Stream a single file from raw.githubusercontent.com.

    Streams with a running byte cap so we abort oversized bodies without
    buffering the whole response. Redirects are disabled — raw.github
    serves 200 directly; a 3xx indicates either an upstream change or a
    redirect-based host swap and is rejected for the same reason as F5.
    """
    self._assert_raw_host(download_url)
    chunks: list[bytes] = []
    total = 0
    async with client.stream(
        "GET",
        download_url,
        timeout=_GITHUB_RAW_TIMEOUT,
        follow_redirects=False,
    ) as response:
        if response.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Refusing redirect on raw fetch: {response.status_code} -> "
                f"{response.headers.get('location')!r}"
            )
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise RuntimeError(
                    f"File body exceeded {_MAX_FILE_BYTES}-byte cap mid-stream"
                )
            chunks.append(chunk)
    return b"".join(chunks)

@staticmethod
def _write_file_safely(
    target_root: Path,
    rel_dir: str,
    name: str,
    content: bytes,
) -> None:
    # F7: reject unsafe filename, then unsafe combined path, then validate
    # the resolved path is strictly below target_root, then convert OSError
    # to RuntimeError so download_skill catches uniformly.
    BAD_NAMES = ('', '.', '..')
    if name in BAD_NAMES or '/' in name or '\\' in name or '\x00' in name:
        raise RuntimeError(f'Unsafe filename from GitHub Contents API: {name!r}')
    rel = Path(rel_dir) / name if rel_dir else Path(name)
    if rel.is_absolute():
        raise RuntimeError(f'Absolute path rejected: {rel}')
    if any(part in BAD_NAMES for part in rel.parts):
        raise RuntimeError(f'Unsafe path segment in {rel}')

    target_resolved = target_root.resolve()
    final = (target_root / rel).resolve()
    try:
        final.relative_to(target_resolved)
    except ValueError as e:
        raise RuntimeError(f'Path escapes target_root: {rel}') from e
    if final == target_resolved:
        raise RuntimeError(f'Path collapses to target_root: {rel}')

    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(content)
    except OSError as e:
        raise RuntimeError(f'Filesystem write failed for {rel}: {e}') from e
```

New `download_skill()`:

```python
async def download_skill(
    self,
    slug: str,
    version: str | None = None,
    target_dir: str | None = None,
) -> DownloadResult:
    # SCOPE NOTE: SkillsMP is consumed by:
    #   - mcp_proxy/tools/skills/install_skill.py:81
    #   - servers/routes/skills.py:386 (install_from_hub)
    # Neither caller passes target_dir. Both read DownloadResult.path,
    # parse SKILL.md content, and persist to SQLite via create_skill;
    # the temp dir is then ignored (skills live in the database, not on
    # disk). target_dir remains in the signature for LSP compatibility
    # with HubProvider.download_skill but is **not honored** here:
    # SkillsMPProvider always returns a fresh temp dir with is_temp=True.
    # If target_dir is supplied (no current caller does), it is silently
    # ignored. A follow-up task should remove target_dir from the
    # abstract base once we confirm no provider implementation uses it.
    #
    # On any failure between mkdir and the success return, the temp dir
    # is removed in the finally block. On success, the temp dir is
    # handed to the caller (is_temp=True) with no further cleanup.
    details = await self.get_skill_details(slug)
    if details is None:
        return DownloadResult(
            success=False, slug=slug, error=f'Skill not found: {slug}',
        )
    if not details.source_url:
        return DownloadResult(
            success=False, slug=slug,
            error=f'No source URL available for skill: {slug}',
        )
    try:
        source = self._parse_github_url(details.source_url, version_override=version)
    except ValueError as e:
        return DownloadResult(success=False, slug=slug, error=str(e))

    if target_dir is not None:
        logger.warning(
            'SkillsMPProvider ignores target_dir=%s; install will use a '
            'temp dir. target_dir is reserved for HubProvider LSP '
            'compatibility and will be removed in a follow-up.',
            target_dir,
        )

    extract_path = Path(tempfile.mkdtemp(prefix='skillsmp_'))
    cleanup_on_failure = True
    try:
        try:
            async with httpx.AsyncClient() as client:
                if isinstance(source, _GithubFileSource):
                    content = await self._fetch_github_file(client, source.raw_url)
                    self._write_file_safely(extract_path, '', 'SKILL.md', content)
                else:
                    await self._fetch_github_dir(
                        client=client,
                        ref=source,
                        rel_path='',
                        target_root=extract_path,
                        depth=0,
                    )
        except httpx.HTTPStatusError as e:
            return DownloadResult(
                success=False, slug=slug,
                error=self._format_github_status_error(e),
            )
        except httpx.RequestError as e:
            return DownloadResult(
                success=False, slug=slug, error=f'GitHub request failed: {e}',
            )
        except (RuntimeError, ValueError) as e:
            return DownloadResult(success=False, slug=slug, error=str(e))

        skill_md = extract_path / 'SKILL.md'
        if not skill_md.exists() or skill_md.stat().st_size == 0:
            return DownloadResult(
                success=False, slug=slug,
                error='SKILL.md not found at source URL',
            )

        # Build the success result first, then disable cleanup, then return.
        # If construction itself raises (pytest instrumentation), the
        # finally block still cleans the temp dir because cleanup_on_failure
        # is still True.
        success_result = DownloadResult(
            success=True, slug=slug,
            path=str(extract_path), version=source.ref, is_temp=True,
        )
        cleanup_on_failure = False  # caller owns the temp dir from here
        return success_result
    finally:
        if cleanup_on_failure:
            shutil.rmtree(extract_path, ignore_errors=True)


@staticmethod
def _format_github_status_error(e: httpx.HTTPStatusError) -> str:
    # R2-F5: GitHub signals throttling and policy denials through several
    # overlapping channels. Detect each in priority order and surface an
    # actionable message; fall through to the generic status message only
    # when no recognized signal is present.
    #
    # Channels handled:
    #   1. SSO required: 403 + X-GitHub-SSO header (org-protected resource).
    #   2. Primary rate limit: 403 + X-RateLimit-Remaining: 0 + Reset.
    #   3. Secondary/abuse rate limit: 403 or 429 + Retry-After (or body
    #      message containing the canonical phrases).
    #   4. Plain 429: Retry-After honored.
    #   5. Generic 4xx/5xx.
    response = e.response
    status = response.status_code
    headers = response.headers

    # Body inspection is bounded to the already-buffered response (Contents
    # API returns small JSON; raw fetches don't enter this code path because
    # they're streamed and use a different exception flow on 4xx).
    body_text = ''
    try:
        body_text = response.text or ''
    except (UnicodeDecodeError, RuntimeError):
        body_text = ''
    body_lower = body_text.lower()

    if status == 403 and headers.get('X-GitHub-SSO'):
        sso = headers.get('X-GitHub-SSO', '')
        return (
            f'GitHub SSO required for this resource ({sso}). '
            f'The repository is gated by an organization SSO policy and '
            f'cannot be installed via SkillsMP without an authorized token.'
        )

    if status == 403:
        remaining = headers.get('X-RateLimit-Remaining')
        reset = headers.get('X-RateLimit-Reset')
        if remaining == '0' and reset:
            try:
                wait_s = max(0, int(reset) - int(time.time()))
                mins = wait_s // 60
                return (
                    f'GitHub primary rate limit exhausted. '
                    f'Resets in {wait_s}s (~{mins} min). '
                    f'Skill installs are unauthenticated; either wait or '
                    f'configure a GitHub token (follow-up).'
                )
            except (TypeError, ValueError):
                return 'GitHub primary rate limit hit (unparseable reset header).'

    if status in (403, 429):
        # Secondary / abuse rate limit detection: GitHub returns Retry-After
        # in seconds (RFC 7231 §7.1.3 form) or with a body message containing
        # 'secondary rate limit' or 'abuse'. Either signal is sufficient.
        retry_after = headers.get('Retry-After')
        is_secondary = (
            'secondary rate limit' in body_lower
            or 'abuse detection' in body_lower
            or 'abuse rate limit' in body_lower
        )
        if retry_after or is_secondary:
            wait_hint = ''
            if retry_after:
                try:
                    wait_hint = f' Retry-After: {int(retry_after)}s.'
                except (TypeError, ValueError):
                    wait_hint = f' Retry-After: {retry_after!r} (unparseable).'
            kind = 'secondary rate limit' if is_secondary else f'throttle ({status})'
            return (
                f'GitHub {kind} hit.{wait_hint} Back off and retry, or '
                f'configure a GitHub token (follow-up).'
            )

    if status == 429:
        return f'GitHub rate-limited (429). Back off and retry.'

    return f'GitHub fetch failed: {status}'
```

Behavioral contract:

- `get_skill_details` returns None → fail with "Skill not found: {slug}".
- Details returned but `source_url` is None → fail with "No source URL
  available...".
- `source_url` points at a non-GitHub / non-raw-GitHub host → fail with a
  ValueError-derived message from `_parse_github_url`.
- `source_url` is a `blob/.../SKILL.md` URL → rewritten to raw URL,
  single-file fetch (no Contents API), written as `target/SKILL.md`. Sibling
  files are **not** discovered — `/blob/` URLs are file pointers.
- `source_url` is a `tree/{ref}/{path}` URL → Contents API recursion against
  that directory.
- `source_url` is a bare repo URL → Contents API recursion against repo root.
- `source_url` is a `raw.githubusercontent.com/{owner}/{repo}/{ref}/.../SKILL.md`
  URL → single-file fetch directly. `{ref}` MUST be a single segment
  (commit SHA or no-slash branch/tag); `refs/heads/...` / `refs/tags/...`
  forms are rejected because ref/path disambiguation is unreliable.
- `source_url` is a `github.com/{owner}/{repo}/{path}/SKILL.md` URL with no
  `/blob/` or `/tree/` marker → single-file fetch at HEAD via raw.github.
  Recognized because the parent task observed this shape in real SkillsMP
  records. `version` override applies if supplied.
- `_parse_github_url` rejects refs containing slashes across all URL shapes.
  Skills hosted on slash-containing branches must surface a commit SHA or
  no-slash ref name in the SkillsMP record.
- Caller supplies `version` arg → validated against `_REF_RE` (no slashes)
  before substitution; ValueError on invalid form.
- GitHub returns 404/500 → fail with status code surfaced.
- GitHub returns 403 with `X-RateLimit-Remaining: 0` → fail with a clear
  primary-rate-limit-exhausted message including reset wait time.
- GitHub returns 403 with `X-GitHub-SSO` → fail with an SSO-required
  message naming the org-policy denial.
- GitHub returns 403 or 429 with `Retry-After` or with body text matching
  `secondary rate limit` / `abuse` → fail with a secondary-rate-limit
  message including the retry-after hint.
- GitHub returns 403 without rate-limit/SSO/secondary signals → fail with
  generic 403.
- GitHub returns 429 without other signals → fail with a 429 throttle
  message.
- GitHub returns entries with `..` in names, `name='.'`, `name=''`,
  embedded slashes, or NUL → `_write_file_safely` rejects; download fails.
- GitHub `download_url` outside `raw.githubusercontent.com` → rejected by
  `_assert_raw_host` before any byte is fetched.
- Raw fetch returns a 3xx → rejected (no host-swap via redirect).
- Contents API entry has missing/non-int `size` → rejected as
  "GitHub Contents API returned invalid size...".
- Per-file size over 10 MB (metadata OR streamed body) → fails clean.
- Subdirectory nesting beyond 3 levels → `_fetch_github_dir` raises;
  download fails.
- Filesystem write failure (permissions, disk-full, IsADirectoryError) →
  caught by `_write_file_safely`, surfaced as RuntimeError, then converted
  to `DownloadResult(success=False)` by `download_skill`.
- All fetches go through a fresh temp directory (`skillsmp_*`) created
  via `tempfile.mkdtemp`. On success the directory is returned to the
  caller with `is_temp=True`; the caller owns its lifecycle. On any
  failure path between mkdir and the success-return statement, the temp
  directory is removed in the `finally` block (no partial-file leaks,
  no temp-dir leaks).
- `target_dir` parameter is **not honored** by SkillsMPProvider. It
  remains in the signature for LSP compatibility with the abstract
  `HubProvider.download_skill`. If a caller supplies `target_dir`, a
  warning is logged and a temp dir is used regardless. No current caller
  passes `target_dir` (verified against `install_skill.py:81` and
  `servers/routes/skills.py:386`); skills are installed to the SQLite
  database, not to a configurable filesystem location. A follow-up task
  should remove `target_dir` from the abstract base provider once the
  other implementations (clawdhub, github_collection, claude_plugins)
  are similarly audited.
- SKILL.md missing or zero-byte in the temp directory → fail with
  "SKILL.md not found at source URL"; temp dir is cleaned.
- **Success-result construction precedes the cleanup-flag flip**: the
  `DownloadResult(success=True, ...)` object is built before
  `cleanup_on_failure = False` flips. If result construction itself
  raises (pytest instrumentation, monkeypatching), cleanup still runs
  in the `finally` block.

Do **not** forward `self.auth_token` on any GitHub request — SkillsMP keys
aren't GitHub keys and public repos don't need auth; sending a bad bearer
could trigger GitHub 401s unnecessarily.

Validation criteria: `download_skill("good-slug")` returns
`DownloadResult(success=True, path=<dir>, version=<ref>)` when mocked
`get_skill_details` yields a record with a GitHub `tree/main/<dir>` URL and
the Contents API is mocked with a listing containing SKILL.md; returns
`DownloadResult(success=False, error=<clear msg>)` for each failure class
above. No dependency on the deleted `/skills/{slug}/download` path anywhere
in the final code.

Test scenarios (auto-wrapped by TDD):

URL parsing / source-shape coverage:

- Happy path with `tree/main/<path>` URL → Contents API returns SKILL.md +
  siblings, all written.
- Happy path with `blob/main/.../SKILL.md` URL → rewritten to raw URL,
  single-file fetch, only SKILL.md written (no Contents API call).
- Happy path with `raw.githubusercontent.com/.../{ref}/.../SKILL.md` URL
  (single-segment ref) → single-file fetch.
- Happy path with ref-less GitHub HTML URL
  `github.com/{owner}/{repo}/{path}/SKILL.md` (no `/blob/`, no `/tree/`) →
  single-file fetch at HEAD (R2-F1).
- Happy path with bare repo URL (no /blob/ or /tree/) → directory fetch at
  repo root with `ref="HEAD"`.
- `version="v2"` override on tree URL → `ref=v2` used on Contents API.
- `version="feature/foo"` override → ValueError (slash rejected).
- `/blob/feature/foo/.../SKILL.md` URL (ref-with-slashes) → ValueError.
- `/blob/main/.../README.md` URL (non-SKILL.md leaf) → ValueError.
- `raw.githubusercontent.com/.../refs/heads/main/.../SKILL.md` →
  ValueError naming the unsupported multi-segment-ref shape (R2-F2).
- Path segment containing `..` or NUL → ValueError before any HTTP call.
- Owner failing grammar: `OWNER!`, trailing-hyphen `owner-`, leading-hyphen
  `-owner`, overlong (>39 chars) → ValueError (R2-F6).
- Repo failing grammar: `..repo`, `.repo`, `repo.`, `repo--`, empty,
  overlong (>100 chars) → ValueError (R2-F6).
- Owner/repo at allowed boundaries: single-char `a`/`a`, 39-char owner,
  100-char repo, dotted repo `my.repo` → succeed (R2-F6 boundary).
- `https://gitlab.com/...` source_url → ValueError before any HTTP call.
- `source_url=None` in details → clean failure.
- `get_skill_details` returns None → clean failure.

GitHub error coverage:

- Contents API returns 500 → clean failure.
- Contents API returns 404 → clean failure.
- Contents API returns 403 with `X-RateLimit-Remaining: 0` and
  `X-RateLimit-Reset` set → error message names the reset wait time
  ("primary rate limit exhausted").
- Contents API returns 403 with `X-GitHub-SSO` header → error message
  names SSO requirement (R2-F5).
- Contents API returns 403 with body `"You have triggered an abuse
  detection mechanism"` → error message names secondary/abuse rate
  limit (R2-F5).
- Contents API returns 429 with `Retry-After: 60` → error message
  includes "Retry-After: 60s" (R2-F5).
- Contents API returns 403 without any rate-limit/SSO/secondary signals →
  generic "GitHub fetch failed: 403" message.
- Contents API returns 403 with malformed `X-RateLimit-Reset` (string
  "soon") → falls through to "unparseable reset header" message.
- Raw fetch returns 302 redirect → `_fetch_github_file` rejects.
- Raw fetch's `download_url` host is not `raw.githubusercontent.com` →
  `_assert_raw_host` rejects before any GET.

Path-safety coverage:

- Contents API returns entry with `..` in `name` → `_write_file_safely`
  rejects; download fails.
- Contents API returns entry with `name='.'` → rejected with clear error
  (regression for the "collapse to target_root" gap).
- Contents API returns entry with `name=''` → rejected.
- Contents API returns entry with `name='a/b'` (embedded slash) → rejected.
- `_write_file_safely` raises `OSError` (simulated via mocked
  `Path.write_bytes`) → converted to RuntimeError, surfaces as clean
  `DownloadResult(success=False)`.

Size / depth coverage (boundary tests at limit and one past):

- Contents API entry with `size = 10*1024*1024` → succeeds.
- Contents API entry with `size = 10*1024*1024 + 1` → fails clean.
- Contents API entry with `size` missing → fails clean as
  "invalid size".
- Contents API entry with `size = '5'` (string) → fails clean as
  "invalid size".
- Streamed body actually exceeds cap (Content-Length lied) → fails clean
  mid-stream.
- Subdirectory depth `_MAX_TREE_DEPTH + 1` → fails clean.
- Subdirectory depth `_MAX_TREE_DEPTH` exactly → succeeds.
- Subdirectory recursion populates nested files at allowed depths.

Path-safety upfront-rejection coverage (R2-F3):

- Contents API entry with `name='..'` → rejected at the loop boundary
  before the dotfile-skip path (regression for the original silent-skip
  bug).
- Contents API entry with `name=''` → rejected at the loop boundary.
- Contents API entry with `path='/etc/passwd'` (absolute) →
  `_assert_safe_entry_path` rejects.
- Contents API entry with `path='a/../../b'` → `_assert_safe_entry_path`
  rejects.
- Hidden-file skip still works for safe names: entry with
  `name='.gitignore'` → skipped silently, not written.

Temp-dir lifecycle coverage:

- `target_dir=None` (the only real-world case) → fresh temp dir
  created, populated by fetch, returned with `is_temp=True`. The temp
  dir exists after the function returns; caller owns cleanup.
- Fetch fails mid-way (HTTPStatusError, RequestError, RuntimeError) →
  temp dir is removed by the `finally` block (no leak).
- `target_dir` supplied (non-current case, kept for LSP) → logged
  warning, temp dir used regardless; success returns
  `path=<temp dir>, is_temp=True`. Test asserts the warning is logged
  and that `target_dir` is left untouched.
- **Cleanup-on-construction-raise test**: monkeypatch
  `DownloadResult.__init__` to raise on the success path after a
  happy-path fetch. Assert the temp dir does not exist after the
  function exits (cleanup_on_failure was still True when the raise
  occurred, `finally` cleaned it up).

Output coverage:

- No SKILL.md in returned tree (after directory fetch with non-SKILL.md
  files only) → clean failure, temp dir cleaned.
- Zero-byte SKILL.md → clean failure, temp dir cleaned.

### 2.3 Remove dead ZIP path and stale test [category: refactor] (depends: 2.2)

Target: `src/gobby/skills/hubs/skillsmp.py`,
`tests/skills/hubs/test_skillsmp.py`

Delete the now-unused `_download_and_extract()` method
(`src/gobby/skills/hubs/skillsmp.py:233-270`) and the `zipfile` / `BytesIO`
imports at the top of that module. Keep `tempfile` — `download_skill()` still
uses `tempfile.mkdtemp` for the default target dir.

Delete the stale test `TestSkillsMPDownload.test_download_no_url_returns_error`
at `tests/skills/hubs/test_skillsmp.py:371` (and the enclosing
`TestSkillsMPDownload` class if no other tests remain in it — otherwise leave
the class and just remove that test). This test mocks
`_make_request({"download_url": ""})`, which is the shape of the deleted
endpoint path. The new-flow equivalents (rejecting None source_url, rejecting
non-GitHub hosts, etc.) live with task 2.2.

Spot-check `tests/servers/routes/test_skills_routes.py::TestHubs` after the
refactor — those tests already mock `download_skill` at the provider
boundary, so they should pass untouched. If any test explicitly referenced
`_download_and_extract` (a private method), adjust it here; otherwise leave
the class alone.

After the delete, the `skillsmp.py` module must still pass
`uv run ruff check src/gobby/skills/hubs/skillsmp.py` and
`uv run mypy src/gobby/skills/hubs/skillsmp.py` with no new warnings.

### 2.4 Wire consumer cleanup of temp downloads at hub install call sites [category: code] (depends: 2.2)

Targets: `src/gobby/mcp_proxy/tools/skills/install_skill.py`,
`src/gobby/servers/routes/skills.py`,
`tests/mcp_proxy/tools/skills/test_install_skill.py` (or wherever the
hub-install path is covered),
`tests/servers/routes/test_skills_routes.py`.

**Why this is in scope.** §2.2 returns `DownloadResult(is_temp=True)` on
success, which by the existing `DownloadResult` contract means the caller
owns cleanup. Today neither real consumer cleans up: every successful
SkillsMP install leaks one `/tmp/skillsmp_*` directory until the OS
reaps it. This is not SkillsMP-specific — every provider that returns
`is_temp=True` triggers the same leak — but shipping the SkillsMP rewrite
without fixing the leak ships a known-leaking happy path. The fix is
4 lines per call site; bundle it here rather than file a separate task.

**Call site 1**: `src/gobby/mcp_proxy/tools/skills/install_skill.py`,
inside the `if hub_match and not source.startswith('http'):` branch
(currently around L66–L93). Wrap the post-download work in a
`try/finally`:

```python
provider = ctx.hub_manager.get_provider(hub_name)
download_result = await provider.download_skill(skill_slug)

if not download_result.success or not download_result.path:
    return {
        'success': False,
        'error': f'Failed to download from hub: {download_result.error or "Unknown error"}',
    }

try:
    skill_path = Path(download_result.path)
    parsed_skill = ctx.loader.load_skill(skill_path, check_dir_name=False)
    source_type = 'hub'
finally:
    if download_result.is_temp and download_result.path:
        shutil.rmtree(download_result.path, ignore_errors=True)
```

Add `import shutil` at module top if not already present.

`load_skill` populates `parsed_skill.loaded_files[*].content` eagerly
(verified at the existing call site that builds `SkillFile` rows from
`lf.content`), so cleanup after `load_skill` is safe — the persistence
step at the bottom of `install_skill` reads from `parsed_skill` which
is in memory.

The outer `try/except Exception` block already in `install_skill` still
catches any later persistence error; the inner `finally` only owns the
temp-dir cleanup and runs even when the persistence path raises later.

**Call site 2**: `src/gobby/servers/routes/skills.py::install_from_hub`
(currently around L380–L426). Same pattern:

```python
provider = server.hub_manager.get_provider(request_data.hub_name)
download = await provider.download_skill(
    slug=request_data.slug,
    version=request_data.version,
)

if not download.success:
    raise HTTPException(
        status_code=502,
        detail=f'Download failed: {download.error}',
    )

try:
    from gobby.skills.loader import SkillLoader

    loader = SkillLoader(default_source_type='hub')
    parsed = loader.load_skill(download.path, validate=True, check_dir_name=False)

    skill = server.skill_manager.create_skill(
        name=parsed.name,
        description=parsed.description,
        # ... existing kwargs ...
        project_id=request_data.project_id,
    )
    await _broadcast_skill('skill_created', skill.id)
    return {'installed': True, 'skill': skill.to_dict()}
finally:
    if download.is_temp and download.path:
        shutil.rmtree(download.path, ignore_errors=True)
```

Add `import shutil` at module top if not already present.

`_broadcast_skill` is awaited inside the `try`; the `finally` runs after
the await completes (or the await raises). The route-level
`try/except (HTTPException, ValueError, Exception)` handlers still see
the raise and produce the right status code; only the temp-dir cleanup
is added.

**Behavioral contract** (additive across both call sites):

- Hub install succeeds → temp dir is removed after persistence.
- `SkillLoader.load_skill` raises (malformed SKILL.md, validation fail) →
  temp dir is removed; the loader exception propagates through the
  outer handler unchanged.
- `create_skill` raises (DB error, duplicate name) → temp dir is
  removed; the storage exception propagates through the outer handler
  unchanged.
- `_broadcast_skill` raises (only relevant on the route path) → temp
  dir is removed; the broadcast exception propagates.
- `download.is_temp` is `False` (provider returned a non-temp path,
  e.g., a future provider that honors `target_dir`) → cleanup skipped;
  caller-supplied directory left untouched.
- `download.path` is empty/None on success (defensive guard) → cleanup
  skipped (the success-without-path case already returns failure
  earlier in `install_skill`; on the route path, `download.path` is
  required by the contract but the guard is cheap).

**Tests**:

`tests/mcp_proxy/tools/skills/test_install_skill.py` (or equivalent):

- `test_hub_install_cleans_temp_dir_on_success` — mock provider returns
  a real `tempfile.mkdtemp()` path with a `SKILL.md` inside,
  `is_temp=True`. Assert the directory does NOT exist after the install
  call returns success.
- `test_hub_install_cleans_temp_dir_on_loader_failure` — mock provider
  returns a temp path with invalid SKILL.md content so
  `load_skill` raises. Assert temp dir is removed after the install
  call returns failure.
- `test_hub_install_cleans_temp_dir_on_persistence_failure` — same as
  above but force `ctx.storage.create_skill` to raise; assert temp dir
  is removed.
- `test_hub_install_skips_cleanup_when_not_is_temp` — mock provider
  returns `is_temp=False` and a real directory; assert directory is
  preserved after install.

`tests/servers/routes/test_skills_routes.py::TestHubs`:

- `test_install_from_hub_cleans_temp_dir_on_success` — same shape.
- `test_install_from_hub_cleans_temp_dir_on_create_skill_error` — force
  `create_skill` to raise; assert cleanup ran and the route returned
  500.
- `test_install_from_hub_cleans_temp_dir_on_broadcast_error` — force
  `_broadcast_skill` to raise; assert cleanup ran.

Validation criteria: hub installs through both the MCP and HTTP routes
no longer leak `/tmp/skillsmp_*` directories. After running the
existing test suite for the two files plus the four new tests, no test
is left with a lingering temp dir under `/tmp/`. The cleanup is
contract-driven (`download.is_temp`), not provider-specific, so a
future provider that returns `is_temp=False` is unaffected.

## Phase 3: Live Verification

**Goal**: Confirm the rewritten install flow works end-to-end against the
real skillsmp.com API and a real GitHub-hosted skill.

### 3.1 Live MCP smoke-test install-from-hub via skillsmp [category: manual] (depends: Phase 2)

Target: no file — manual verification against the running daemon and the
live skillsmp.com API.

Preconditions:

- Daemon restarted so the rewritten `skillsmp.py` and the 2.1.0 `plan` skill
  (already shipped) are loaded.
- `SKILLSMP_API_KEY` stored in the Gobby secrets store (via
  `gobby install` or `gobby secrets set SKILLSMP_API_KEY`). Environment
  variables are **not** consulted — `tests/mcp_proxy/test_registries.py:581`
  asserts env-var bleed is ignored. The provider requires this key for
  `/skills/search`.

Verification steps:

1. MCP `search_tools` (sanity) to confirm the skills MCP surface is
   reachable.
2. `search_hub` for skillsmp with several distinct keywords (e.g. `commit`,
   `python`, `markdown`) and `limit=20` each. From the union of results,
   classify by `source_url` shape and **identify at least one slug per
   shape exercised below**:
   - **Directory shape**: `source_url` matching
     `https://github.com/.../tree/...` or bare repo URL.
   - **Single-file shape**: `source_url` matching
     `https://raw.githubusercontent.com/.../SKILL.md` or
     `https://github.com/.../blob/.../SKILL.md` or the ref-less
     `https://github.com/.../{path}/SKILL.md` shape.
   Record the chosen slugs and their source-shape classification. If the
   live corpus exposes only one shape, document the missing shape and
   confirm it is covered by the §2.2 mocked tests; do not block the
   review on a corpus that lacks one shape.
3. For each captured slug, run `install_skill` with `hub_name="skillsmp"`.
   Expected: `installed: true` and a populated `skill` dict.
4. `list_skills` — confirm each newly installed skill appears with
   `source_type="hub"`, `hub_name="skillsmp"`, `hub_slug=<captured slug>`.
5. `get_skill(name=...)` for each — confirm SKILL.md content, description,
   and `content` are populated.
6. **Temp-dir cleanup check (§2.4)**: before step 3, snapshot
   `/tmp/skillsmp_*` (e.g., `ls -d /tmp/skillsmp_* 2>/dev/null`). After
   each successful install, re-run the same command and confirm no new
   `/tmp/skillsmp_*` directories remain. The cleanup wired by §2.4
   should leave `/tmp` exactly as it was.

Failure modes to check and report:

- 404s from `/skills/{slug}` or `/skills/{slug}/download` anywhere in the
  daemon logs — zero expected.
- GitHub 403 / 429 rate-limiting on Contents API or raw fetches — mention
  in verification notes if seen; does not block the close, but flag the
  reset/retry-after window. Re-run after the window if observed.
- `SKILL.md not found at source URL` for any skillsmp slug that used to
  install cleanly — investigate the search record shape via
  `search_hub` and inspect the resulting `source_url` against the
  classifications in step 2.

Close this task with a `changes_summary` reporting:

- The captured slugs, their source-shape classification (directory /
  single-file), and which shapes were exercised live vs deferred to mocks.
- For each directory install: a one-line log excerpt with
  `api.github.com/repos` confirming the Contents API path was exercised.
- For each single-file install: a one-line log excerpt with
  `raw.githubusercontent.com` confirming the streaming raw fetch was
  exercised.
- If only one shape was available in the live corpus, name the
  uncovered-by-live shape and reference the corresponding §2.2 mocked
  test that covers it.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
