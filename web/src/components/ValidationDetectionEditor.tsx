import { useEffect, useMemo, useState } from "react";

import { Button } from "./ui/Button";
import { Input } from "./ui/Input";
import { Textarea } from "./ui/Textarea";
import { configurationClient } from "../api/config";

const DEFAULT_DETECTION_CONFIG = {
  enabled: true,
  builtin_matchers_enabled: true,
  disabled_builtin_matcher_ids: [],
  recognized_wrappers: [],
  wrapper_rules: [],
  custom_matchers: [],
};

interface ValidationDetectionEditorProps {
  value: unknown;
  onChange: (value: Record<string, unknown>) => void;
  onValidityChange?: (isValid: boolean) => void;
  title?: string;
}

interface PreviewResult {
  matched: boolean;
  matcher_id?: string;
  label?: string;
  categories?: string[];
  languages?: string[];
}

function normalizeValue(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return DEFAULT_DETECTION_CONFIG;
}

export function ValidationDetectionEditor({
  value,
  onChange,
  onValidityChange,
  title = "Validation Detection",
}: ValidationDetectionEditorProps) {
  const normalized = useMemo(() => normalizeValue(value), [value]);
  const incoming = useMemo(
    () => JSON.stringify(normalized, null, 2),
    [normalized],
  );
  const [jsonText, setJsonText] = useState(incoming);
  const [syncedValue, setSyncedValue] = useState(incoming);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [command, setCommand] = useState("");
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  let editorJsonText = jsonText;
  let editorJsonError = jsonError;
  if (syncedValue !== incoming) {
    let currentCanonical: string | null = null;
    try {
      currentCanonical = JSON.stringify(
        normalizeValue(JSON.parse(jsonText)),
        null,
        2,
      );
    } catch {
      currentCanonical = null;
    }
    if (currentCanonical !== incoming) {
      editorJsonText = incoming;
      editorJsonError = null;
    }
  }

  useEffect(() => {
    onValidityChange?.(!editorJsonError);
  }, [editorJsonError, onValidityChange]);

  const handleJsonChange = (next: string) => {
    setSyncedValue(incoming);
    setJsonText(next);
    try {
      const parsed = JSON.parse(next);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setJsonError("Expected a JSON object");
        return;
      }
      setJsonError(null);
      onChange(parsed as Record<string, unknown>);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : String(error));
    }
  };

  const previewCommand = async () => {
    setPreview(null);
    setPreviewError(null);
    try {
      const parsed = JSON.parse(editorJsonText) as Record<string, unknown>;
      const result = await configurationClient.previewValidationDetection(
        command,
        parsed,
      );
      if (!result.ok) {
        setPreviewError(
          typeof result.body.detail === "string"
            ? result.body.detail
            : "Preview failed",
        );
        return;
      }
      setPreview(result.body as unknown as PreviewResult);
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="mb-5 overflow-hidden rounded-lg border border-[var(--border)]">
      <div className="flex items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5 select-none">
        <span className="text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]">
          {title}
        </span>
      </div>
      <div className="flex flex-col gap-3 px-3.5 py-3">
        <div className="flex flex-col gap-1">
          <label
            className="text-[length:var(--text-md)] font-medium text-[var(--text-primary)]"
            htmlFor="validation-detection-json"
          >
            Matcher Config
          </label>
          <span className="text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-muted)]">
            JSON object with enabled, builtin_matchers_enabled,
            disabled_builtin_matcher_ids, recognized_wrappers, wrapper_rules,
            and custom_matchers.
          </span>
          <Textarea
            id="validation-detection-json"
            className="min-h-[220px] resize-y rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 font-mono text-[length:var(--text-md)] leading-5 text-[var(--text-primary)]"
            value={editorJsonText}
            spellCheck={false}
            onChange={(event) => handleJsonChange(event.target.value)}
          />
          {editorJsonError && (
            <span className="text-[length:var(--text-sm)] text-[var(--color-error)]">
              {editorJsonError}
            </span>
          )}
        </div>

        <div className="flex flex-col gap-1">
          <label
            className="text-[length:var(--text-md)] font-medium text-[var(--text-primary)]"
            htmlFor="validation-detection-preview"
          >
            Preview Command
          </label>
          <div className="flex gap-2 max-sm:flex-col">
            <Input
              id="validation-detection-preview"
              wrapperClassName="flex-1"
              className="rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 font-mono text-[length:var(--text-md)] text-[var(--text-primary)]"
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              placeholder="cargo clippy --no-default-features -- -D warnings"
            />
            <Button
              type="button"
              size="sm"
              className="cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]"
              onClick={() => {
                void previewCommand();
              }}
              disabled={!command.trim() || Boolean(editorJsonError)}
            >
              Preview
            </Button>
          </div>
          {preview && (
            <span className="text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-muted)]">
              {preview.matched
                ? `Matched ${preview.matcher_id}: ${preview.label}`
                : "No validation matcher matched"}
            </span>
          )}
          {previewError && (
            <span className="text-[length:var(--text-sm)] text-[var(--color-error)]">
              {previewError}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
