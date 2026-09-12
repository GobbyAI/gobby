# Install instruction bundles

Load before an authorized skill installation or source refresh.
Discover installed names and source provenance first, then call
`install_skill(source="<selected-source>", project_scoped=true)` for one
project, or omit `project_scoped` for global installed scope. Confirm caller
project context before requesting project scope. Use absolute local paths
when daemon working-directory resolution would be ambiguous.

Supported sources are local directory/SKILL.md, local ZIP, explicit GitHub
reference/URL, and configured hub identifiers. Existing local paths take
precedence over ambiguous owner/repo strings. Arbitrary HTTP download URLs are
rejected. A source containing multiple skills installs the first through MCP;
select an exact skill path instead of assuming a collection import.

The loader validates content and publishes the entrypoint and attached text
files atomically. Install scans the full text surface; external sources fail
closed without the scanner. A safety failure requires fixing/replacing the
source, not overriding it. Name/scope conflicts require inspecting the existing
row before choosing a replacement. Installing a bundled template path as a
project skill is rejected; copy custom authoring sources outside the bundle.

After success inspect the returned installed identity, rediscover in scope,
and load it completely. Installation itself satisfies no instruction gate.
For updates use `update_skill`; inspect `updated`, `skipped` and `skip_reason`.
Only local/filesystem and GitHub sources refresh through the updater; hub/ZIP
or absent provenance can be skipped. Failed refresh retains/restores the stored
revision and files. Do not interpret success with `updated=false` as new content.

Operator CLI install/update/remove delegate to the daemon. HTTP import requires
a project for local/ZIP paths, confines resolved paths to its registered local
checkout, and reports per-item errors when importing multiple skills. Use
isolated fixtures for installation and upgrade verification.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#installation-and-update-boundaries).

_Last verified: 2026-09-12_
