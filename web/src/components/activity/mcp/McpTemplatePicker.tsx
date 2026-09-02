import { useEffect, useRef } from "react";

import type { McpTemplate, McpTemplateParam } from "../../../hooks/useMcp";
import { Button } from "../../ui/Button";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";
import { NativeSelect } from "../../ui/NativeSelect";
import type { McpServerDraft } from "./McpTabActions";

type McpTemplateSelection = Pick<
  McpServerDraft,
  "template" | "values" | "scope" | "project_id"
>;

interface McpTemplatePickerProps {
  selection: McpTemplateSelection;
  currentProjectId: string;
  templates: readonly McpTemplate[];
  loading: boolean;
  error: string | null;
  validationErrors: Readonly<Record<string, string>>;
  fetchTemplates: (projectId?: string) => Promise<void>;
  onChange: (changes: Partial<McpTemplateSelection>) => void;
}

const TEMPLATE_ERROR_KEY = "$template";

function paramLabel(param: McpTemplateParam): string {
  return param.description?.trim() || param.name;
}

function requiredLabel(label: string, required: boolean) {
  if (!required) return label;
  return (
    <>
      {label} <span aria-hidden="true">*</span>
      <span className="sr-only"> required</span>
    </>
  );
}

export function McpTemplatePicker({
  selection,
  currentProjectId,
  templates,
  loading,
  error,
  validationErrors,
  fetchTemplates,
  onChange,
}: McpTemplatePickerProps) {
  const secretBuffers = useRef<Record<string, string>>({});
  const selectedTemplate = templates.find(
    (template) => template.name === selection.template,
  );

  useEffect(() => {
    void fetchTemplates(currentProjectId || undefined);
  }, [currentProjectId, fetchTemplates]);

  useEffect(() => {
    secretBuffers.current = {};
  }, [selection.template]);

  const updateValue = (name: string, value: string) => {
    onChange({ values: { ...selection.values, [name]: value } });
  };

  const clearValue = (name: string) => {
    const values = { ...selection.values };
    delete values[name];
    delete secretBuffers.current[name];
    onChange({ values });
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 md:grid-cols-2">
        <FormField
          label="Template"
          error={validationErrors[TEMPLATE_ERROR_KEY]}
        >
          {({ id, describedBy, invalid }) => (
            <NativeSelect
              id={id}
              aria-label="Template"
              aria-describedby={describedBy}
              error={invalid}
              value={selection.template}
              disabled={loading || templates.length === 0}
              onChange={(event) =>
                onChange({ template: event.target.value, values: {} })
              }
            >
              <option value="">
                {loading ? "Loading templates" : "Select template"}
              </option>
              {templates.map((template) => (
                <option key={template.name} value={template.name}>
                  {template.name}
                </option>
              ))}
            </NativeSelect>
          )}
        </FormField>
        <FormField label="Scope">
          {({ id, describedBy }) => (
            <NativeSelect
              id={id}
              aria-label="Scope"
              aria-describedby={describedBy}
              value={selection.scope}
              onChange={(event) => {
                const scope = event.target.value as McpServerDraft["scope"];
                onChange({
                  scope,
                  project_id: scope === "project" ? currentProjectId : "",
                });
              }}
            >
              <option value="project" disabled={!currentProjectId}>
                Current project
              </option>
              <option value="global">Global</option>
            </NativeSelect>
          )}
        </FormField>
      </div>

      {error ? (
        <div className="flex items-center justify-between gap-3" role="alert">
          <p className="text-[length:var(--text-sm)] text-destructive">
            {error}
          </p>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => void fetchTemplates(currentProjectId || undefined)}
          >
            Retry
          </Button>
        </div>
      ) : null}

      {!loading && !error && templates.length === 0 ? (
        <p className="text-[length:var(--text-sm)] text-muted-foreground">
          No templates are available for this project.
        </p>
      ) : null}

      {selectedTemplate?.description ? (
        <p className="max-w-[70ch] text-[length:var(--text-sm)] text-muted-foreground">
          {selectedTemplate.description}
        </p>
      ) : null}

      {selectedTemplate ? (
        <div className="grid gap-4 md:grid-cols-2">
          {selectedTemplate.params.map((param) => {
            const label = paramLabel(param);
            const fieldError = validationErrors[param.name];
            if (param.choices.length > 0) {
              return (
                <FormField
                  key={param.name}
                  label={requiredLabel(label, param.required)}
                  error={fieldError}
                >
                  {({ id, describedBy, invalid }) => (
                    <NativeSelect
                      id={id}
                      aria-required={param.required}
                      aria-describedby={describedBy}
                      error={invalid}
                      value={selection.values[param.name] ?? ""}
                      onChange={(event) =>
                        updateValue(param.name, event.target.value)
                      }
                    >
                      <option value="">Select {label.toLowerCase()}</option>
                      {param.choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </FormField>
              );
            }

            if (param.secret) {
              const hasReference = Boolean(selection.values[param.name]);
              return (
                <FormField
                  key={param.name}
                  label={requiredLabel(label, param.required)}
                  hint={
                    hasReference
                      ? "Reference set. Type to replace it or clear it."
                      : "Enter a $secret:NAME reference. The value is write-only."
                  }
                  error={fieldError}
                >
                  {({ id, describedBy, invalid }) => (
                    <div className="flex items-center gap-2">
                      <Input
                        id={id}
                        type="password"
                        aria-required={param.required}
                        aria-describedby={describedBy}
                        aria-invalid={invalid}
                        autoComplete="off"
                        spellCheck={false}
                        value=""
                        onFocus={() => {
                          secretBuffers.current[param.name] = "";
                        }}
                        onChange={(event) => {
                          const next = `${secretBuffers.current[param.name] ?? ""}${event.target.value}`;
                          secretBuffers.current[param.name] = next;
                          updateValue(param.name, next);
                        }}
                        onKeyDown={(event) => {
                          if (event.key !== "Backspace") return;
                          event.preventDefault();
                          const next = (
                            secretBuffers.current[param.name] ?? ""
                          ).slice(0, -1);
                          secretBuffers.current[param.name] = next;
                          updateValue(param.name, next);
                        }}
                      />
                      {hasReference ? (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          aria-label={`Clear ${label}`}
                          onClick={() => clearValue(param.name)}
                        >
                          Clear
                        </Button>
                      ) : null}
                    </div>
                  )}
                </FormField>
              );
            }

            return (
              <FormField
                key={param.name}
                label={requiredLabel(label, param.required)}
                error={fieldError}
              >
                {({ id, describedBy, invalid }) => (
                  <Input
                    id={id}
                    aria-required={param.required}
                    aria-describedby={describedBy}
                    aria-invalid={invalid}
                    value={selection.values[param.name] ?? ""}
                    onChange={(event) =>
                      updateValue(param.name, event.target.value)
                    }
                  />
                )}
              </FormField>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
