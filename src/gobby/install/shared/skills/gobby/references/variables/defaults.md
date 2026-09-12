# Variable defaults

Load when explaining a default or changing its source. Discover installed rows
with `gobby-workflows:list_variables` and `get_variable_definition`; read live
values separately. A bundled template never proves active state.

Effective precedence is enabled global definitions, enabled project definitions,
then stored session overrides. Whole keys replace, including objects and nulls.
Disabling a project default can reveal a global one. Cache invalidation follows
the variables revision, but persisted overrides still mask changed defaults.

Bundled grouped YAML is synced separately from named user definitions. Inspect
sync counts/errors before claiming success. User rows and pinned toggles are
preserved; managed deleted rows may return. Agent selectors affect materialized
activation defaults, not the ordinary read layer or access authorization.
See [lifecycle](../../../../../../../../docs/guides/variables.md#variable-lifecycle), [initialization](../../../../../../../../docs/guides/variables.md#initialization),
and [selectors](../../../../../../../../docs/guides/variables.md#variable-selectors). If changes do not appear, inspect the
target project and stored override before retrying sync.
