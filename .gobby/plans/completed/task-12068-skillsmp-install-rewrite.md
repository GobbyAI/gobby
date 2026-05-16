# Lean SkillsMP Install Rewrite

## Summary

- Fix `#12068` by treating SkillsMP as a search index over GitHub-hosted skills.
- Keep the fix inside `SkillsMPProvider` plus focused tests. Preserve existing
  `HubProvider` method signatures, MCP tool schemas, HTTP routes, and storage
  schema.
- Ignore `skillUrl`; it is a SkillsMP detail page, not an install source.

## Implementation Changes

- In `src/gobby/skills/hubs/skillsmp.py`, add private helpers for auth guard,
  exact search lookup, details mapping, GitHub URL parsing, and directory copy.
- Rewrite `get_skill_details(slug)` to call `/skills/search?q=<slug>&limit=10`,
  require an exact `id` or `slug` match, and return `HubSkillDetails`; return
  `None` for search misses or upstream request failures.
- Rewrite `download_skill(slug, version=None, target_dir=None)` to:
  - Look up the exact SkillsMP record through the same search helper.
  - Require `githubUrl`; ignore `skillUrl`.
  - Resolve supported GitHub source shapes to either a skill directory or the
    parent directory of `SKILL.md`: repo root, `/tree/<branch>/<path>`,
    `/blob/<branch>/<path>/SKILL.md`, `raw.githubusercontent.com/.../SKILL.md`,
    and observed `github.com/<owner>/<repo>/<path>/SKILL.md`.
  - Use existing `GitHubRef` and `clone_skill_repo` for GitHub downloads, then
    validate that the resolved directory contains `SKILL.md`.
  - Copy the resolved skill directory to `target_dir` when provided; otherwise
    return the cached resolved path.
  - Return `DownloadResult(success=False, error=...)` for missing source URL,
    unsupported URL shape, clone failure, or missing `SKILL.md`.
- Remove the obsolete ZIP download path and imports from `SkillsMPProvider`.

## Public Interfaces

- No new `HubSkillInfo` or `HubSkillDetails` fields.
- No MCP, HTTP, config, DB, or storage contract changes.
- Keep SkillsMP auth behavior aligned with existing provider behavior: search
  and details require the configured SkillsMP API key; download reports auth
  failure as a failed `DownloadResult`.

## Test Plan

- Update `tests/skills/hubs/test_skillsmp.py` with mocked HTTP/search tests for
  exact details lookup, no exact match, upstream failure, missing auth, missing
  `githubUrl`, unsupported source URL, and successful GitHub directory install.
- Patch `clone_skill_repo` in download tests and use temporary skill directories
  containing `SKILL.md` plus at least one extra file to prove multi-file skills
  survive.
- Verify no calls target `/skills/{slug}` or `/skills/{slug}/download`.
- Run:
  - `uv run pytest tests/skills/hubs/test_skillsmp.py -v`
  - `uv run ruff check src/gobby/skills/hubs/skillsmp.py tests/skills/hubs/test_skillsmp.py`
- Live smoke after tests: use `search_hub(query="openapi", hub_name="skillsmp",
  limit=1)`, install the returned slug with `install_skill(source="skillsmp:<slug>")`,
  confirm it appears in `list_skills`, then remove the smoke-installed skill.

## Assumptions

- `githubUrl` is the only install source from SkillsMP records.
- Branch names with slashes remain unsupported for raw/blob URL parsing unless a
  real SkillsMP record proves the need; current tree URLs from live search use
  single-segment refs such as `main`.
