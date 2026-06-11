# Review: skills

- **Scope:** `src/gobby/skills/` — loader/parser/validator/scanner/scaffold/metadata,
  sync/manager/updater/formatting, search/injector, and `hubs/` (manager, base,
  github_collection, skillsmp, clawdhub, claude_plugins). Plus the storage seam
  (`storage/skills/`), the `gobby-skills` MCP tool surface (`mcp_proxy/tools/skills/`),
  and the HTTP skills routes (`servers/routes/skills.py`). Cross-seam reads into
  `install/shared/skills/` (bundled templates), `workflows/sync_rules.py` (the sibling
  bundled-sync module), and tests.
- **Reviewer:** Claude Fable 5 — 3-agent parallel fan-out, all Blockers synthesizer-verified.
- **Commit / branch:** `0.5.0` @ HEAD `d4041227c` (working tree clean at review time).
- **Summary:** 6 Blocker · 9 Important · 5 Nit — the skill install pipeline is the highest-
  risk surface in this review and its security gate is structurally broken: the scan runs on
  only one of three install paths, inspects only the markdown body (not the bundled scripts),
  and fails open when its dependency is missing; two hub providers have SSRF/path-traversal
  on remote-controlled values; and the bundled-sync module inherits the permanent-silent-wipe
  family from `sync_rules.py`. The ZIP and skillsmp paths are well-hardened — the gaps are in
  the newer providers and the HTTP route.

## Findings

### [BLOCKER] HTTP skill-install routes bypass the security scanner entirely

- **Where:** `servers/routes/skills.py:331` and `:479` (`import_skill`/`install_from_hub` call `server.skill_manager.create_skill(...)` directly) — verified: the scanner (`scan_skill_content`) is imported and called only at the separate, optional `POST /scan` endpoint (`:365-371`) and in the MCP `install_skill` tool (`mcp_proxy/tools/skills/install_skill.py:189-191`). The import/hub routes never invoke it.
- **Failure mode:** The MCP `install_skill` tool runs `scan_skill_content()` and refuses to persist when `is_safe` is false. The HTTP `POST /api/skills/import` and `/install_from_hub` routes — the paths the web UI uses — load from GitHub/ZIP/local/hub and `create_skill(... enabled=True)` with no scan. The "hub install scanner gate" the task flags is trivially bypassed by using the HTTP route instead of the MCP tool. The HTTP server binds `0.0.0.0` by default (`servers/http.py:624`) and these routes carry no visible auth dependency.
- **Why it matters:** Any caller of the HTTP install routes installs and auto-enables arbitrary skill content with no gate. Two install surfaces, one gated.
- **Minimal fix:** Move the scan into the shared `create_skill`/loader seam so every install path is gated by construction; at minimum call `scan_skill_content` in the import/hub routes and reject on `not is_safe`.
- **Confidence:** high (verified).

### [BLOCKER] The scanner inspects only SKILL.md body — bundled scripts/resources are stored unscanned

- **Where:** `mcp_proxy/tools/skills/install_skill.py:191-193` (scans `parsed_skill.content` only) then `:237-248` persists `parsed_skill.loaded_files` unconditionally; `skills/scanner.py:98-129` writes only `content` to the temp `SKILL.md`.
- **Failure mode:** `scan_skill_content` receives only the markdown body. The skill's `loaded_files` — everything under `scripts/`, `references/`, `assets/`, populated by `loader._load_skill_files` — are never passed to the scanner yet are persisted to `skill_files` right after the gate. A malicious skill ships an innocuous SKILL.md prose body plus a hostile `scripts/payload.sh`/`setup.py`; it passes the gate and the payload is stored verbatim.
- **Why it matters:** The gate inspects the one file least likely to carry the payload and ignores the executable companions — exactly where a real attack lives.
- **Minimal fix:** Scan every `loaded_file` (or write the full skill directory to the scanner's temp root and let ClawCare's directory walk cover it) and gate on the max severity across all files.
- **Confidence:** high.

### [BLOCKER] The scanner is fail-open — a missing `clawcare` dependency silently skips the gate

- **Where:** `mcp_proxy/tools/skills/install_skill.py:210-213` (verified: `except ImportError: logger.warning(...)` then falls through to persist).
- **Failure mode:** If `clawcare` isn't importable, the scan is skipped and the skill installs and auto-enables with only a debug-level warning; the returned dict still reports `success: True`. No config requires the scanner to be present, so a deployment without it accepts every skill unscanned.
- **Why it matters:** The only content gate for remote skills degrades to "no gate" on an absent dependency, silently. Security controls should fail closed.
- **Minimal fix:** For remote source types (`hub`/`github`/`zip`), fail closed when the scanner is unavailable, or surface a `scan_skipped: true` flag and a loud warning.
- **Confidence:** high.

### [BLOCKER] `claude-plugins` `download_skill` fetches an attacker-controlled `rawFileUrl` (SSRF, unbounded, redirect-following)

- **Where:** `skills/hubs/claude_plugins.py:294-298` — verified: `raw_url = metadata.get("rawFileUrl")` then `await client.get(raw_url, timeout=30.0, follow_redirects=True)` with no scheme/host validation; `content = response.text` (unbounded).
- **Failure mode:** `rawFileUrl` is taken verbatim from the remote registry JSON and fetched following redirects. A malicious/compromised `claude-plugins.dev` (or MITM) can point it at `http://169.254.169.254/...` (cloud metadata/IAM creds), `http://127.0.0.1:<port>/...` (the local Gobby daemon's own HTTP API, reachable since it binds `0.0.0.0`), or any internal host; redirect-following lets even a benign `https://` 302 into the internal range. The fetched body is written to SKILL.md and trusted. `.text` reads the whole body with no size cap (memory DoS). Contrast skillsmp, which enforces a `github.com`/`raw.githubusercontent.com` host allowlist.
- **Minimal fix:** Require `https`, allowlist the registry's known raw-content hosts, reject private/link-local IPs, disable or re-validate redirects, and cap the streamed size.
- **Confidence:** high (verified `:296`).

### [BLOCKER] `github-collection` `_clone_skill` path traversal via `slug` — copytree from outside the repo

- **Where:** `skills/hubs/github_collection.py` `_clone_skill` (`skill_subpath = f"{self._path.strip('/')}/{slug}"`, `skill_path = repo_path / skill_subpath`, `shutil.copytree(skill_path, target)`); `loader._validate_github_ref:317-354` validates owner/repo/branch but **not** `ref.path`.
- **Failure mode:** `slug` flows unsanitized into `skill_subpath`, so `repo_path / "../../../../etc/ssh"` resolves outside the clone cache; `skill_path.exists()` and `copytree` follow `..` at the OS level, and the copied directory becomes the "skill" loaded and persisted. Reachable via the unvalidated HTTP `install_from_hub` route (`slug` with `../`); the MCP path's `[A-Za-z0-9_-]+` regex blocks `/`. Arbitrary local directories (secrets/keys) get ingested into a skill record and injected into agent context. Contrast skillsmp's `_join_safe_path` containment.
- **Minimal fix:** Validate `ref.path`/`slug` (reject `..`, leading `/`, empty segments — reuse `_join_safe_path`); assert `skill_path.resolve()` is within `repo_path.resolve()` before `copytree`. Best: extend `_validate_github_ref` to cover `ref.path` for all providers.
- **Confidence:** high on the code path; med on severity (read-side, self-targeted).

### [BLOCKER] Bundled-sync inherits the permanent-silent-wipe family from `sync_rules.py`

- **Where:** `skills/sync.py:238-254` — verified: `on_disk` is built only from successfully-parsed skills (`on_disk.add(parsed.name)` at `:241`); the orphan pass `if _is_gobby_owned(skill) and skill.name not in on_disk: storage.delete_skill(skill.id)` (`:253-254`) is unconditional; `loader.load_directory:773-779` swallows `SkillLoadError` and the drop is **never recorded in `result["errors"]`** (worse than sync_rules, which at least records it).
- **Failure mode:** Two sub-cases. (a) **Parse failure:** a bundled `SKILL.md` present-on-disk but failing to parse (bad YAML, missing field, dir-name mismatch) is dropped, its name never enters `on_disk`, and the still-live installed row is soft-deleted — `success=True`, `errors=[]`. The unique constraint `(name, project_id, source)` NULLS-NOT-DISTINCT excludes `deleted_at`, so the soft-deleted row blocks clean re-create, and because parsing keeps failing the restore-on-next-sync net never fires — **permanent silent wipe across every project**. (b) **Empty/partial dir:** the early guard checks only `skills_path.exists()` (`:218`); an existing-but-empty `skills/` (partial install, packaging regression) yields empty `on_disk` and soft-deletes the entire bundled skill set. The HTTP `restore_defaults` route is a second trigger beyond startup.
- **Minimal fix:** Distinguish "removed" from "failed to parse" — exclude attempted-but-failed names from orphan eligibility, skip the orphan pass (and set `success=False`) when any skill failed or zero skills parsed, and surface dropped names into `errors`.
- **Confidence:** high (verified).

### [IMPORTANT] No size cap on skill `content`, loaded files, or YAML frontmatter — OOM / DB-bloat / billion-laughs

- **Where:** `parser.py:481` (`path.read_text()` uncapped), `loader.py:716` (per-file `read_text()` uncapped), `parser.py:249` (`yaml.safe_load` with no frontmatter size bound — `safe_load` blocks code exec but still expands recursive anchors/aliases). The validator bounds name/description/tags but never `content`.
- **Failure mode:** A skill (especially from ZIP/GitHub/hub) with a multi-GB SKILL.md or many large files OOMs the daemon on parse and bloats `skills`/`skill_files`; a crafted YAML anchor bomb blows up CPU/memory during parse. The two largest attacker-controlled fields are the only unbounded ones — inverted threat model. Same unbounded-fetch issue in the hub providers (`github_collection._fetch_skill_content`, `claude_plugins.download_skill` both `response.text` with no cap).
- **Minimal fix:** Enforce max byte size on SKILL.md, each loaded file, the total per skill, and the frontmatter block; stream hub fetches with a hard cap.

### [IMPORTANT] Install gate ignores `allowed_tools` metadata — remote skill self-declares capabilities, stored verbatim and auto-enabled

- **Where:** `parser.py:367-375` (parsed), validator never validates it, `storage/skills/_metadata.py:140` (stored as JSON), `install_skill.py` persists `allowed_tools=parsed_skill.allowed_tools` with `enabled=True`.
- **Failure mode:** A remote skill declares any `allowed-tools` list; nothing validates the values or restricts what a skill may claim, and the scan never sees them. No current path auto-grants tool execution from a skill's `allowed_tools` (verified: `hooks/skill_manager.py:58` round-trips it; the workflow-engine `allowed_tools` is a separate concept) — so this is latent, not a live escalation. But the moment a consumer treats it as an auto-approve grant, an installed skill self-escalates.
- **Minimal fix:** Validate `allowed_tools` against a known tool allowlist; document it as descriptive until a deliberate enforcement design exists.

### [IMPORTANT] One bad skill aborts the whole batch / bundled sync

- **Where:** `loader.py:774-779` (`load_directory` per-skill loop catches only `SkillLoadError`); `sync.py:227-235` (`load_directory(... validate=False)` wrapped in `except Exception` that aborts the whole sync with `success=False`).
- **Failure mode:** A skill that raises any non-`SkillLoadError` exception (the non-string-`name` `AttributeError` below, an `OSError` from `read_text`, a YAML blowup) propagates out of the loop, dropping every remaining skill in a multi-skill ZIP/GitHub `load_all`, and aborting sync of all bundled skills. (Note: a non-`SkillParseError` from a bundled file aborts sync *before* the orphan pass, so it's a denial-of-all, not a wipe — distinct from the wipe Blocker.)
- **Minimal fix:** Broaden the per-skill catch to `except Exception` (log-and-skip).

### [IMPORTANT] Non-string `name` (and other fields) crash the validator with an uncaught AttributeError

- **Where:** `parser.py:355-357` (`name = frontmatter.get("name")`, only truthiness checked) → `validator.py:98` (`if name != name.lower()` on a list/int → AttributeError).
- **Failure mode:** YAML `name: [a, b]` or `name: 123` is truthy, passes the guard, and reaches `validate_skill_name` which crashes instead of returning a clean ValidationResult. In `install_skill` it's caught by a broad except; in `load_directory` it is not, escalating to the batch-abort above.
- **Minimal fix:** `if not isinstance(name, str): raise SkillParseError(...)` in the parser; guard non-str in the validator.

### [IMPORTANT] Root `SKILL.md` read has no symlink guard (GitHub clones)

- **Where:** `loader.py:568-575` + `parser.py:478-482` (`path / "SKILL.md"` then `parse_skill_file` reads it; no `is_symlink()` check, unlike `_load_skill_files:686` and `_scan_subdirectory:637`).
- **Failure mode:** A GitHub repo (git creates real symlinks; `check_dir_name=False` for GitHub/ZIP) can ship `SKILL.md` as a symlink to an arbitrary host file, read via `parse_frontmatter`. Exfil is constrained — the target must parse as `---`-delimited frontmatter to be stored — but it's an unguarded host-file read on the trusted-loader path. (ZIP symlinks are neutralized: `extract_zip` copies entry bytes.)
- **Minimal fix:** Reject `skill_file.is_symlink()`/`path.is_symlink()` before parsing.

### [IMPORTANT] `restore_defaults` and scope-move routes run synchronous sync/DB work on the event loop

- **Where:** `servers/routes/skills.py:285` (`sync_bundled_skills(...)` directly in an async route — walks the bundled tree + many DB transactions on the loop), `:568`/`:581` (move handlers call `skill_manager.move_to_*` synchronously), while the same file uses `run_in_threadpool` for far cheaper calls.
- **Failure mode:** "Restore defaults" / scope-move clicks stall the daemon's HTTP/WebSocket loop for the full sync duration; under load can wedge unrelated requests and heartbeats.
- **Minimal fix:** `await run_in_threadpool(partial(sync_bundled_skills, db))`; route move calls through the threadpool.

### [IMPORTANT] `updater._apply_update` is non-atomic; rollback never restores skill files

- **Where:** `updater.py:396+` (separate `update_skill` then `set_skill_files` transactions), `_create_backup:263` snapshots only metadata, `_restore_backup:277` restores only metadata.
- **Failure mode:** If `set_skill_files` raises after the metadata commit, `_restore_backup` restores description/content/version but never re-writes the skill files (they were never snapshotted) — a failed update returns `rolled_back=True` while leaving the installed copy's `scripts/references/assets` in a partial state. Violates the updater's core contract (failed update must not clobber the working copy); bundled-skill refresh is the primary target.
- **Minimal fix:** Snapshot existing skill files in `_create_backup`/restore them, or wrap metadata + file writes in one transaction.

### [IMPORTANT] Scope-move hits the unique constraint as an unguarded 500; clawdhub argument injection

- **Where:** `storage/skills/_metadata.py:494-523` (`move_to_project`/`move_to_installed` do `get_skill` then `update_skill(source=..., project_id=...)` with no destination-collision pre-check; the unique constraint includes soft-deleted rows) → route bare `except Exception → 500`; `skills/hubs/clawdhub.py` (`_run_cli_command` extends argv with `[slug]`/`[query]` and no `--` end-of-options separator — the install regex `[A-Za-z0-9_-]+` allows leading hyphens, so `clawdhub:--force` reaches the CLI as a flag).
- **Failure mode:** Moving a skill whose name collides with a bundled skill (common — bundled skills own many names) raises a DB IntegrityError surfaced as an opaque 500; a remote/attacker-influenced slug like `--dir`/`--token` is interpreted by the clawhub CLI as an option (argv injection, not shell — bounded by clawhub's flag surface).
- **Minimal fix:** Pre-check the destination scope (including soft-deleted) and raise a clean 409; insert `--` before remote-derived positionals and reject leading `-`.

### [NIT] Hub install hygiene and missing tests

- **Where:** `install_skill.py` hub branch never honors `DownloadResult.is_temp` → temp dirs (clawdhub `clawdhub_*`, claude_plugins `claude_plugins_*`, github clone cache) leak per install; `github_collection` nested-layout slugs (`category/name`) are discoverable but uninstallable via the MCP `hub:slug` regex (no `/`); `tests/storage/test_skill_sync.py` has the orphan-removal test but **no** test that a present-but-unparseable bundled skill or an empty dir is NOT orphaned — the exact wipe Blocker is untested.

## Systemic patterns

1. **Two install surfaces, one security gate.** The scan + slug validation live only in the MCP `install_skill` tool; the HTTP `import`/`install_from_hub`/`restore_defaults` routes and the bundled `sync` all reach `create_skill` without them. Security belongs at the shared `create_skill`/loader seam, not bolted onto one of several callers.
2. **The gate inspects the wrong/smallest surface and fails open.** Scanning only `content` while persisting `loaded_files` and `allowed_tools` unscanned, and skipping the scan entirely when `clawcare` is absent, is a structural mismatch between what's checked and what's trusted.
3. **Remote-controlled values reach sinks without per-provider validation parity.** skillsmp is the well-defended reference (host allowlist + `_join_safe_path` + containment); github-collection (`ref.path`) and claude-plugins (`rawFileUrl`) skip the equivalent checks. `_validate_github_ref` validates owner/repo/branch but never `ref.path`, so every provider that sets `ref.path` must self-defend and one doesn't.
4. **Unbounded attacker-controlled inputs.** `content`, loaded files, YAML frontmatter, and hub fetch bodies all lack size/expansion caps while scalar metadata is carefully bounded — inverted threat model.
5. **The bundled-sync wipe family** (`skills/sync.py` and `workflows/sync_rules.py`): per-file `except → skip` followed by an unconditional orphan-cleanup keyed off the successfully-parsed set; any parse failure or empty/partial source dir is indistinguishable from intentional removal → silent soft-delete. The skills variant is worse — the drop isn't even recorded in `errors`.
6. **Narrow exception catches escalate "skip one" into "abort all"** — `load_directory` catches only `SkillLoadError`; any other error aborts the batch/sync.

## Verified non-bugs (cleared — don't re-chase)

- **Zip-slip is correctly handled** — `extract_zip` resolves and `relative_to`-checks every member; `_resolve_within_directory` guards the internal path; ZIP symlink entries are neutralized to plain files via `copyfileobj`.
- **No SSRF in the GitHub *loader* path** — `clone_url` is hardcoded `https://github.com/{owner}/{repo}.git`; owner/repo/branch are regex-validated with shell metachars rejected; git is invoked as list-form argv (no shell). The gap is only `ref.path` (filed).
- **skillsmp is the hardened reference** — `_join_safe_path` rejects `..`/empty/`.`; `_parse_github_url` restricts host to `github.com`/`raw.githubusercontent.com`. No traversal or SSRF there.
- **No arbitrary code execution from frontmatter** (`yaml.safe_load`, non-mapping rejected); `_load_skill_files`/`_scan_subdirectory` reject symlinks and verify containment.
- **enabled/disabled preserved across sync** (`_handle_existing_gobby_skill` passes `enabled=existing.enabled` on drift refresh; only restore forces re-enable); **user-owned skills are never overwritten or orphaned** (`_is_gobby_owned` gate).
- **Bug-family #2 (caller passes a non-bundled directory): IMMUNE** — `sync_bundled_skills(db)` takes no path arg, always resolves `get_bundled_skills_path()`.
- **A non-parse error (OSError/UnicodeDecodeError) does NOT wipe** — it aborts sync before the orphan pass with `success=False`; the wipe is specifically a parse-failure / empty-dir hazard.
- **`set_skill_files` internal atomicity is correct** (single transaction, hash-based skip/update + orphan soft-delete); auth tokens come from `SecretStore`, not env/URLs (only clawdhub passes `--token` as an argv, minor `ps`-visibility).
- **search.py has no query injection or scope leak in itself** (in-memory tokenized matching; scoping is the caller's filter); **injector.py is pure selection logic** with no prompt-envelope templating (and `select_skills` has no live callers yet).
- **`%s` placeholders are correct** per repo convention (CLAUDE.md's `$N` mandate is stale doc drift).
