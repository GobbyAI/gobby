# Impeccable release upgrade

This repository-only tool re-vendors Impeccable from an exact npm CLI release.
Pass the candidate as a required `MAJOR.MINOR.PATCH` argument:

```bash
uv run python src/gobby/install/shared/skills/impeccable/.upgrade/transform.py 3.5.0
```

The candidate has no default. Use the current `IMPECCABLE_RELEASE.version`
explicitly for a same-release reproduction check.

## Upgrade flow

1. **Stage and preflight.** The tool installs `impeccable@<candidate>` in a
   temporary npm harness, generates the Claude release artifact, and validates
   the package identity, exact resolved version, dependency maps, generated
   skill release, and single inclusive Node engine floor. It also generates and
   validates both lockfiles. Rejection prints `JUDGMENT NEEDED` and leaves the
   destination tree untouched.
2. **Scripts.** Released `scripts/**` are replaced wholesale from generated
   output. Gobby's `scripts/package.json` identity is retained while its skill
   release, dependencies, and optional dependencies are refreshed; its
   `package-lock.json` is regenerated.
3. **References.** Released references receive the session-continuation
   preamble, `<scripts_dir>` resolver and `PUPPETEER_CACHE_DIR` contract,
   `get_skill_file` cross-reference expansion, `.impeccable.md` naming, and
   placeholder substitutions. Near-verbatim and vendored-as-is catalogue
   entries follow their declared policy. Curated Gobby adaptations remain in
   place and appear as unified diffs under `JUDGMENT NEEDED` for manual review.
4. **Pins and manifest.** One pass updates `SKILL.md` runtime CLI, skill release,
   and normalized Node floor; NOTICE provenance; `IMPECCABLE_RELEASE.version`;
   `IMPECCABLE_RELEASE.lockfile_sha256`; `IMPECCABLE_NODE_MIN_VERSION`; the
   managed CLI lockfile; and the bundled-content manifest. The manifest excludes
   every leading-dot path component, matching packaged wheel membership.

Review each judgment diff before committing. A Node-floor change is also
reported because downstream literal witnesses are deliberately outside the
machine-edited pin set. Run the same exact candidate again after review; a
coherent release produces no file changes, while catalogued curated diffs remain
visible for confirmation.
