# Materialize and use skill scripts

Load when a selected skill requires files from its `scripts/` directory.
First load its instructions and inspect the script inventory/provenance. Call
`materialize_skill_scripts(name="<installed-name>")` after schema discovery.
Use the returned absolute `scripts_dir` and `environment` for the documented
command; do not guess a checkout-relative or cache path.

Materialization resolves the selected DB revision into a content-addressed,
atomically published cache generation. It does not execute the skill's task
script or satisfy instruction requirements. It may prepare managed dependencies
and browser artifacts for trusted sources, so use it within the authorized
script workflow rather than to browse help.

Inspect `parser_deps.installed`/`warning`, `browser.ready`/`warning`, and
`node.version`/`satisfies_floor` before assuming runtime readiness. Missing Node,
npm, lockfiles or browser artifacts can yield a usable scripts directory with
warnings; use only the fallback the skill explicitly supports, or repair the
runtime and materialize again. No top-level success assumption replaces these
readiness fields.

External scripts are scanned and cannot install executable dependencies or
managed browser artifacts. Trusted Node dependency preparation uses a lockfile
and `npm ci --ignore-scripts`; runtime declarations do not grant broader
execution authority. Validate metadata.gobby.runtime fields when authoring.
Never edit shared cache generations as a source fix or remove another process's
live staging directory. Correct the source/installed revision and resolve again;
keep returned environment settings, including browser-cache location.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#script-materialization).

_Last verified: 2026-09-12_
