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
- `githubUrl` is not guaranteed on every record — fall back to `skillUrl`.
  Reject both-absent with a clear error rather than guessing.
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
    so the MCP layer can ranks consistently with other hubs. ``githubUrl``
    (preferred) or ``skillUrl`` populates ``source_url`` so callers can
    resolve the skill to a GitHub location without a second round-trip.
    ``version`` is not provided by the list/search endpoints today.
    """
    stars = skill.get("stars")
    score = (
        float(stars)
        if isinstance(stars, int | float) and not isinstance(stars, bool)
        else None
    )
    source_url = skill.get("githubUrl") or skill.get("skillUrl")
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
    source_url = skill.get("githubUrl") or skill.get("skillUrl")
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
- Exact match record missing `githubUrl` but carrying `skillUrl` → `source_url`
  populated from `skillUrl` (fallback).
- Exact match record missing both → `source_url = None` (download will fail
  clearly at its own guard; `get_skill_details` itself still returns the
  populated details).
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
- `get_skill_details_falls_back_to_skillUrl` — record missing `githubUrl` but
  with `skillUrl`.
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
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from gobby.skills.hubs.base import DownloadResult, HubProvider, HubSkillDetails, HubSkillInfo
```

(Remove `zipfile` and `BytesIO` imports — they were only used by the deleted
`_download_and_extract`. See task 2.3.)

New module-level constants:

```python
_GITHUB_CONTENTS_HOST = "api.github.com"
_GITHUB_API_TIMEOUT = 30.0
_GITHUB_RAW_TIMEOUT = 60.0
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per-file cap
_MAX_TREE_DEPTH = 3
```

New private `_GithubRef` dataclass (scoped to this module):

```python
@dataclass(frozen=True)
class _GithubRef:
    owner: str
    repo: str
    ref: str
    path: str  # dir path within the repo; "" for repo root
```

New helper `_parse_github_url()`:

```python
def _parse_github_url(
    self,
    source_url: str,
    version_override: str | None = None,
) -> _GithubRef:
    """Parse a GitHub URL from a SkillsMP record into owner/repo/ref/path.

    Accepts:
      - https://github.com/{owner}/{repo}/blob/{ref}/{path}
      - https://github.com/{owner}/{repo}/tree/{ref}/{path}
      - https://github.com/{owner}/{repo}

    For blob URLs, the skill directory is the parent directory of the file
    (the file is typically SKILL.md). For tree URLs, the path is the skill
    directory itself. For a bare repo URL, the repo root is the skill dir.

    Raises ValueError for non-github.com hosts or malformed paths.
    """
    parsed = urlparse(source_url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        raise ValueError(f"Only github.com source_url is supported, got: {parsed.netloc}")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Malformed GitHub URL, missing owner/repo: {source_url}")
    owner, repo = parts[0], parts[1]
    if len(parts) == 2:
        ref = version_override or "HEAD"
        return _GithubRef(owner=owner, repo=repo, ref=ref, path="")
    if len(parts) < 4 or parts[2] not in ("blob", "tree"):
        raise ValueError(f"Malformed GitHub URL, expected blob/tree: {source_url}")
    kind = parts[2]
    ref = version_override or parts[3]
    path_parts = parts[4:]
    if kind == "blob":
        # File URL — skill dir is the parent.
        path_parts = path_parts[:-1]
    return _GithubRef(owner=owner, repo=repo, ref=ref, path="/".join(path_parts))
```

New helpers `_fetch_github_dir`, `_fetch_github_file`, `_write_file_safely`:

```python
async def _fetch_github_dir(
    self,
    client: httpx.AsyncClient,
    ref: _GithubRef,
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
    url = f"https://{_GITHUB_CONTENTS_HOST}/repos/{ref.owner}/{ref.repo}/contents/{full_path}"
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
        name = entry.get("name", "")
        if not name or name.startswith("."):
            continue
        entry_type = entry.get("type")
        if entry_type == "file":
            download_url = entry.get("download_url")
            if not download_url:
                continue
            size = entry.get("size", 0)
            if size > _MAX_FILE_BYTES:
                raise RuntimeError(
                    f"File {name} exceeds {_MAX_FILE_BYTES}-byte cap ({size} bytes)"
                )
            content = await self._fetch_github_file(client, download_url)
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

async def _fetch_github_file(
    self,
    client: httpx.AsyncClient,
    download_url: str,
) -> bytes:
    response = await client.get(
        download_url,
        timeout=_GITHUB_RAW_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content

@staticmethod
def _write_file_safely(
    target_root: Path,
    rel_dir: str,
    name: str,
    content: bytes,
) -> None:
    """Write content to target_root/rel_dir/name, rejecting unsafe paths."""
    rel = Path(rel_dir) / name if rel_dir else Path(name)
    # Reject absolute or traversal-bearing paths outright.
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError(f"Unsafe path from GitHub Contents API: {rel}")
    final = (target_root / rel).resolve()
    if target_root.resolve() not in final.parents and final != target_root.resolve():
        raise RuntimeError(f"Path escapes target_root: {rel}")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(content)
```

New `download_skill()`:

```python
async def download_skill(
    self,
    slug: str,
    version: str | None = None,
    target_dir: str | None = None,
) -> DownloadResult:
    """Download a SkillsMP-indexed skill from its GitHub source.

    Resolves the skill's githubUrl via get_skill_details, parses it into
    owner/repo/ref/path, and fetches the directory tree through the GitHub
    Contents API. Validates SKILL.md is present before returning success.
    """
    details = await self.get_skill_details(slug)
    if details is None:
        return DownloadResult(
            success=False,
            slug=slug,
            error=f"Skill not found: {slug}",
        )
    if not details.source_url:
        return DownloadResult(
            success=False,
            slug=slug,
            error=f"No source URL available for skill: {slug}",
        )
    try:
        ref = self._parse_github_url(details.source_url, version_override=version)
    except ValueError as e:
        return DownloadResult(success=False, slug=slug, error=str(e))

    if target_dir:
        extract_path = Path(target_dir)
        is_temp = False
        extract_path.mkdir(parents=True, exist_ok=True)
    else:
        extract_path = Path(tempfile.mkdtemp(prefix="skillsmp_"))
        is_temp = True

    try:
        async with httpx.AsyncClient() as client:
            await self._fetch_github_dir(
                client=client,
                ref=ref,
                rel_path="",
                target_root=extract_path,
                depth=0,
            )
    except httpx.HTTPStatusError as e:
        return DownloadResult(
            success=False,
            slug=slug,
            error=f"GitHub fetch failed: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        return DownloadResult(
            success=False,
            slug=slug,
            error=f"GitHub request failed: {e}",
        )
    except (RuntimeError, ValueError) as e:
        return DownloadResult(success=False, slug=slug, error=str(e))

    skill_md = extract_path / "SKILL.md"
    if not skill_md.exists() or skill_md.stat().st_size == 0:
        return DownloadResult(
            success=False,
            slug=slug,
            error="SKILL.md not found at source URL",
        )

    return DownloadResult(
        success=True,
        slug=slug,
        path=str(extract_path),
        version=ref.ref,
        is_temp=is_temp,
    )
```

Behavioral contract:

- `get_skill_details` returns None → fail with "Skill not found: {slug}".
- Details returned but `source_url` is None → fail with "No source URL
  available...".
- `source_url` points at a non-GitHub host → fail with a ValueError-derived
  message from `_parse_github_url`.
- `source_url` is a `blob/.../SKILL.md` URL → skill dir is the parent dir;
  Contents API is called against the parent, siblings are fetched alongside
  SKILL.md.
- `source_url` is a `tree/...` URL → Contents API called against that dir.
- Caller supplies `version` arg → overrides the ref parsed from the URL.
- GitHub returns 404/500 → fail with status code surfaced.
- GitHub returns entries with `..` in names → `_write_file_safely` rejects;
  download fails.
- Subdirectory nesting beyond 3 levels → `_fetch_github_dir` raises; download
  fails.
- Per-file size over 10 MB → `_fetch_github_dir` raises; download fails.
- Temp-dir path returned with `is_temp=True` when caller passed `target_dir=None`.
- SKILL.md missing from the fetched tree → fail with "SKILL.md not found at
  source URL".

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

- Happy path with `tree/main/<path>` URL, Contents API returning SKILL.md +
  siblings.
- Happy path with `blob/main/.../SKILL.md` URL — resolves to parent dir.
- `version="v2"` override uses `ref=v2` on Contents API.
- Non-github.com source_url rejected before any HTTP call.
- `source_url=None` in details → clean failure.
- `get_skill_details` returns None → clean failure.
- Contents API returns 500 → clean failure.
- Contents API returns entry with `..` in name → rejected; download fails.
- Subdirectory recursion populates nested files.
- No SKILL.md in returned tree → clean failure.
- Per-file size cap breached → clean failure.
- `target_dir=None` → creates temp dir, `is_temp=True` on result.

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

1. MCP `call_tool("gobby", "call_tool", {"server_name": "gobby",
   "tool_name": "search_tools", ...})` to confirm the skills MCP surface is
   reachable (sanity; existing tools only — the rewrite does not add new
   ones).
2. MCP `call_tool("gobby", "call_tool", {"server_name": "skills",
   "tool_name": "search_hub", "arguments": {"hub_name": "skillsmp",
   "q": "<any common keyword, e.g. 'commit'>", "limit": 5}})`. Expected:
   non-empty list of results, each with `source_url` populated from
   `githubUrl`. Capture one slug for step 3.
3. MCP `call_tool("gobby", "call_tool", {"server_name": "skills",
   "tool_name": "install_skill", "arguments": {"hub_name": "skillsmp",
   "slug": "<captured slug>"}})`. Expected: response with `installed: true`
   and a populated `skill` dict.
4. MCP `call_tool("gobby", "call_tool", {"server_name": "skills",
   "tool_name": "list_skills", "arguments": {}})`. Expected: the new skill
   appears with `source_type="hub"`, `hub_name="skillsmp"`,
   `hub_slug="<captured slug>"`.
5. MCP `call_tool("gobby", "call_tool", {"server_name": "skills",
   "tool_name": "get_skill", "arguments": {"name": "<installed skill
   name>"}})`. Expected: full SKILL.md content rendered, `description` and
   `content` populated.

Failure modes to check and report:

- 404s from `/skills/{slug}` or `/skills/{slug}/download` anywhere in the
  daemon logs — zero expected.
- GitHub 403 rate-limiting on Contents API — mention in verification notes
  if seen; does not block the close, but flag it.
- `SKILL.md not found at source URL` for any skillsmp slug that used to
  install cleanly — investigate the search record shape (githubUrl vs
  skillUrl, tree vs blob).

Close this task with a `changes_summary` reporting the captured slug, the
installed skill's `hub_slug`, and a one-line log excerpt confirming the
Contents API path was exercised (grep for `api.github.com/repos` in the
daemon log).

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
