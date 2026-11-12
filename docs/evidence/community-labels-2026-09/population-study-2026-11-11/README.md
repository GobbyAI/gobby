# Full-population audit before expanded naming

Josh approved expanding beyond the 15-community pilot on 2026-11-11, then asked
whether the Leiden communities themselves were accurate. The read-only snapshot
`population-before-fix.json.gz` preserves all 3074 stored communities and their member
signatures before the importer-scoping correction. The 191819-byte gzip decompresses
byte-for-byte to the original snapshot. Its uncompressed SHA-256 is:
`ca4a1d6e7dd339f85df25821e17e4a2b3fd727342fee68485a7977fda1923cb7`.

| Population | Communities | Files |
| --- | ---: | ---: |
| Singleton | 2915 | 2915 |
| Two files | 20 | 40 |
| Three or more files | 139 | 6508 |
| Total | 3074 | 9463 |

Singletons are 94.8% of communities and 30.8% of files. All singleton boundary
lists are empty. Their extension distribution includes 1328 Markdown, 286 YAML,
278 JSON, 367 Rust and 193 Python files. Every indexed Markdown and YAML file is
a singleton; 367/1135 Rust files, 193/5123 Python files, 50/417 TypeScript files
and 3/389 TSX files are singletons. Counting singleton documents as independent
evidence of failed code clustering would be misleading.

## Confirmed file-import defect

The focused regression
`relative_imports_in_different_packages_keep_their_local_edges` constructs two
packages, each importing its own `.models`. Before the production change:

```text
cargo nextest run -p gobby-code -E 'test(relative_imports_in_different_packages_keep_their_local_edges)'
nextest b0a92994-7bea-4321-8b83-950ca52de90a
actual edges: []
expected: alpha/service.py -> alpha/models.py; beta/service.py -> beta/models.py
1 failed, 1440 skipped
```

The module-view identity intentionally unions providers by module string across
the project. File partitioning incorrectly reused that global uniqueness test,
losing otherwise unambiguous local edges. A complementary case checks that a
missing local `.models` cannot borrow the sole provider in another package.
The correction retains importer-scoped candidates for file partitioning and keeps
the module view's global aliases and ambiguity rules intact. The first scoped
rerun passed 75 community/module-view tests. The final source also passed:

- `GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(communities::) | test(mcg)'`:
  100 passed, 1397 skipped, including isolated database refresh and stale-version
  rebuild tests (nextest `07ae645f-964c-40e9-8502-5a27809ab6ed`).
- `cargo clippy -p gobby-code --all-targets -- -D warnings`: no issues.
- `cargo fmt -p gobby-code -- --check`: passed.
- Changed Rust test-quality audit: three files, 22 tests, zero findings.
- Python suppression ratchet: 5184 files, 166 baseline suppressions, zero new/stale.

Algorithm-cache version 3 forces the prior stored partition to rebuild. The release
policy changed during this work: `land_commit` now generates binary patch versions,
lockfile entries and pins against the locked integration tip. The manual release
edits were removed and the pre-land release build cancelled. Rebuild and promotion
must use the landed identity. At the time of this initial audit, nothing from this
source fix was installed. **Follow-up:** gcode 1.9.117 is now built, promoted and
the partition refreshed; see the [expanded study](../expanded-study-2026-11-11/README.md)
for corrected population counts, the 169 authored names and completed Clef diagnostics.

The global alias contract was checked in
`crates/gcode/src/communities/identity.rs:16–21` at
`74c6d74a127935d32f55b713944f412023929bcf`: “Alias membership is global”;
the complete source excerpt hash is
`483902af8f904c7d2b301a117d75ed6ff8b31a742571d7bc6a041d02af3a2c82`.

This proves an input-graph defect; it does not quantify how many live singletons
the correction removes. Module declarations and qualified calls also differ from
explicit imports, so import communities are not a complete ownership graph.
Expanded name authoring and Clef evaluation must use the corrected frozen
population. The original 15-community study and Josh's ratings stay unchanged.
