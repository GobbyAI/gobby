import type { RuleDetail } from "../../../hooks/useRules";
import { CodeMirrorEditor } from "../../shared/CodeMirrorEditor";
import { EditableViewActions } from "../../shared/EditableView";
import { useEditableContent } from "../../shared/editableContent";

interface RulesYamlViewProps {
  detail: RuleDetail;
  bundled: boolean;
  yamlText: string;
  yamlError: string | null;
  /** Parse and persist the buffered YAML; resolve true on success. */
  onYamlSave: (text: string) => Promise<boolean>;
}

export function RulesYamlView({
  detail,
  bundled,
  yamlText,
  yamlError,
  onYamlSave,
}: RulesYamlViewProps) {
  const editState = useEditableContent({ content: yamlText, onSave: onYamlSave });
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
      {bundled && (
        <div className="grid min-w-0 gap-1">
          <span className="text-[length:var(--text-xs)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">Name</span>
          <span className="min-h-11 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-[0.65rem] text-[length:var(--text-sm)] text-[var(--text-primary)] [overflow-wrap:anywhere]">{detail.name}</span>
          <span className="text-[length:var(--text-xs)] text-[var(--text-muted)]">
            Bundled template rule names are read-only
          </span>
        </div>
      )}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium text-foreground">Rule YAML</span>
          <div className="flex shrink-0 items-center gap-2">
            <EditableViewActions
              isEditing={editState.isEditing}
              onEdit={editState.beginEdit}
              onSave={() => void editState.saveEdit()}
              onCancel={editState.cancelEdit}
              saving={editState.saving}
            />
          </div>
        </div>
        <div className="min-h-80 w-full min-w-0 flex-1 overflow-hidden rounded-md border border-border bg-[var(--bg-secondary)] [&_.codemirror-container]:h-full">
          <CodeMirrorEditor
            content={editState.isEditing ? editState.editContent : yamlText}
            language="yaml"
            readOnly={!editState.isEditing}
            ariaLabel="Rule YAML"
            editorId="rule-yaml-editor"
            onChange={editState.isEditing ? editState.setEditContent : undefined}
            onSave={editState.isEditing ? () => void editState.saveEdit() : undefined}
          />
        </div>
      </div>
      {yamlError && (
        <p className="text-sm text-[var(--color-error)]" role="alert">
          {yamlError}
        </p>
      )}
    </div>
  );
}
