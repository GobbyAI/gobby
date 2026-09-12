# Select and preserve skill levels

Load when a standalone skill declares levels, an invocation includes a level,
or a consumer must preserve level state. Discover metadata with the full skill
view or source; valid values come from `metadata.gobby.levels`.

Call `get_skill(name="brevity", level="max")` to select an explicit level.
Without an explicit selection, loading uses `default_level`, then the first
listed level. The response prepends `Active level: <value>` to the unchanged
instruction body; read the whole body, not only the matching subsection.
Unknown levels and levels supplied to an unleveled skill return `invalid_level`.
Use the returned choices rather than inventing aliases.

Completion records `<skill_name_with_underscores>_level` independently from the
loaded-skill set. Cursor continuation retains its original level and view; do
not add a different level mid-stream. Plain instruction requirements accept a
completed skill load, not a particular level. Loading a reference neither
selects nor clears a standalone level. Preserve levels when migrating consumers
or resetting instruction tracking; changing a level requires an explicit load.

The standalone router dispatch must preserve trailing request arguments and
level selection. Capability names take precedence; `skill <name>` explicitly
selects a standalone skill when names collide.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#complete-loading-and-levels).

_Last verified: 2026-09-12_
