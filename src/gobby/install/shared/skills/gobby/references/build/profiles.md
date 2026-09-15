# Build profiles

Load before selecting, creating, modifying, restoring, enabling, disabling, or
deleting a build profile. Start with `gobby-profiles:list_profiles`, then
`show_profile` with an explicit `source` and project when inspecting an override.
Show defaults to `installed`; mutations generally default to `project`, while
restore defaults to `installed`. Always check the current schema.

Resolution checks project-specific `source=project`, then global
`source=project`, then global `source=installed`. The first non-deleted matching
row wins. A disabled match fails; it does not fall back to a bundled profile.
The omitted profile name is `default`. Explicit request fields override the
corresponding profile values, including explicit false/zero/empty-list values.

Profiles cover skipped stages, isolation, unattended posture, local delivery,
and enhancement-round defaults. Inspect actual installed
rows rather than declaring bundled `autopilot`, `fix`, `fix-merge`, or `submit`
active. Delivery is local merge/publication; source-control guidance owns
branch push and workspace landing.

Use `create_profile` for a project preset, `update_profile` for selected fields,
and `enable_profile`/`disable_profile` for availability. Names are immutable.
Prefer a project preset/override for durable customization. Installed rows are
Gobby-owned sync inputs; an edit is not a promise of persistence across sync.
`restore_profile` replaces an installed bundled row with its bundled definition;
it is not a generic undo for project presets. `delete_profile` soft-deletes by
default; purge is restricted to project rows. Inspect scope before mutation.

Distinguish `build_resume` from a new `build_task` request on an existing tree.
Resume restores automation without selecting a new profile. Reissuing
`build_task` resolves profile isolation again and validates existing workspace
pointers against it; specify the intended isolation explicitly. Existing
manifests normally ignore profile skips with a warning and reject explicit
skip changes. The expanded-epic PR-skip repair is a bounded exception described
in the stages topic. Use restart for deliberate lifecycle reconstruction.

Recovery: inspect disabled/deleted or shadowing rows, select the correct scope,
and correct the input or authorized project override. Do not silently enable a
disabled profile, discard workspace artifacts, or change delivery intent to
make a launch pass.

Guide: [Build state and profiles](../../../../../../../../docs/guides/orchestration.md#build-state).

_Last verified: 2026-09-12_
