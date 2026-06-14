# Compile Workflow

Use this prompt when turning accepted research notes into wiki pages.

- Select sources explicitly with repeatable `gwiki compile --source <SOURCE_ID_OR_PATH>` after ingesting or collecting notes.
- Prefer source IDs; raw paths `raw/<id>.md` and exact manifest locations also work.
- Preserve source provenance and citation strings.
- Write canonical pages only through compile actions.
- Keep generated bundles and handoff reports in `outputs/`.
- Prefer Obsidian-compatible links between related wiki pages.
