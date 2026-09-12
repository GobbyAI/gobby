# Author pipeline definitions

Load before creating, editing, exporting, duplicating or retiring a pipeline.
Discover with `list_pipelines` on `gobby-workflows`; `get_pipeline(name=...)`
summarizes a subset of step fields. Use `export_pipeline` for full YAML.

1. Choose a unique descriptive name and explicit project scope. Prepare YAML
   with `type: pipeline`, unique step IDs and at least one executable step.
2. Load validation guidance, then call `create_pipeline(yaml_content=...,
   project_id=...)` for the intended project UUID. Omitted scope is global.
3. For updates, use `definition_id` for project definitions. Name-only management
   resolves the global row; runtime loading prefers the project row, even disabled. `update_pipeline`
   accepts field changes or full YAML; explicit fields override YAML fields.
4. Re-read/export the installed definition. Save an exported file explicitly
   when needed: MCP create/update do not automatically write project YAML.

Runtime loads PostgreSQL. Project `.gobby/workflows/pipelines/`, global
`~/.gobby/workflows/pipelines/`, and bundled workflow directories are authoring
sources. Customize bundled behavior in a custom copy. Explicit enabled pins
survive ordinary bundled sync; unpinned values follow the bundled template.

`delete_pipeline` soft-deletes; bundled deletion requires `force=true` and can be
restored by later definition drift. Retire the appropriate source when permanent
removal is intended. Deleting a definition and deleting run history are separate.
Export retained evidence first; never delete merely to work around a duplicate.

Operator HTTP definition routes additionally support templates, duplication,
scope moves, soft-delete restoration, and restoring a bundled definition body.
Operator `gobby pipelines import PATH [-o OUTPUT]` converts an external-format
file; it does not install a runtime DB definition. Default output is
`.gobby/workflows/NAME.yaml`; inspect and install the resulting YAML separately.

After authorized source changes, `reload_cache(project_path=..., project_id=...)`
imports workflow files and synchronizes bundled rules, agents, pipelines,
variables and detection manifests. It mutates broader installed state; coordinate
scope and inspect returned sync errors/counts. With no project path it scans
all locally checked-out projects; an explicit path requires its project UUID. Project imports preserve same-named global rows. It is not a read-only retry.

On duplicate name, inspect/update the existing target. On invalid YAML, correct
model errors before creation. Do not infer successful installation from a file
write or from a CLI conversion receipt.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#installation-and-operator-boundaries).

_Last verified: 2026-09-12_
