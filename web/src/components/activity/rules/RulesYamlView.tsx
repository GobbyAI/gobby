import type { RuleDetail } from "../../../hooks/useRules";
import { CodeMirrorEditor } from "../../shared/CodeMirrorEditor";

interface RulesYamlViewProps {
  detail: RuleDetail;
  bundled: boolean;
  yamlText: string;
  yamlError: string | null;
  onYamlChange: (value: string) => void;
  onYamlSave: () => void;
}

export function RulesYamlView({
  detail,
  bundled,
  yamlText,
  yamlError,
  onYamlChange,
  onYamlSave,
}: RulesYamlViewProps) {
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
      {bundled && (
        <div className="rules-locked-name">
          <span className="rules-locked-name__label">Name</span>
          <span className="rules-locked-name__value">{detail.name}</span>
          <span className="rules-locked-name__hint">
            Bundled template rule names are read-only
          </span>
        </div>
      )}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <span className="text-sm font-medium text-foreground">Rule YAML</span>
        <div className="min-h-80 w-full min-w-0 flex-1 overflow-hidden rounded-md border border-border bg-[var(--bg-secondary)] [&_.codemirror-container]:h-full">
          <CodeMirrorEditor
            content={yamlText}
            language="yaml"
            ariaLabel="Rule YAML"
            editorId="rule-yaml-editor"
            onChange={onYamlChange}
            onSave={onYamlSave}
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
