import { useState, useEffect, useMemo, useId, useRef } from "react";
import { SidebarPanel } from "../shared/SidebarPanel";
import { CodeMirrorEditor } from "../shared/CodeMirrorEditor";
import { ExpressionBuilder } from "./ExpressionBuilder";
import { useMcp, type McpToolSchema } from "../../hooks/useMcp";
import "./RuleEditForm.css";

const RULE_EVENTS = [
  "before_tool",
  "after_tool",
  "before_agent",
  "session_start",
  "session_end",
  "stop",
  "pre_compact",
];

const EFFECT_TYPES = [
  "block",
  "set_variable",
  "inject_context",
  "mcp_call",
  "observe",
];

export interface RuleEditFormProps {
  isOpen: boolean;
  readOnly?: boolean;
  form: RuleFormData;
  onChange: (form: RuleFormData) => void;
  onSave: () => void;
  onCancel: () => void;
  isEditing: boolean;
  saveDisabled?: boolean;
  sidebarView: "form" | "yaml";
  onViewChange: (view: "form" | "yaml") => void;
  yamlContent: string;
  onYamlChange: (content: string) => void;
  onYamlSave: () => void;
  conflictWarning?: string | null;
}

export interface RuleFormData {
  name: string;
  event: string;
  description: string;
  priority: number;
  enabled: boolean;
  group: string;
  tags: string[];
  when: string;
  effect: { type: string; [key: string]: unknown };
}

export const DEFAULT_RULE_FORM: RuleFormData = {
  name: "",
  event: "before_tool",
  description: "",
  priority: 100,
  enabled: true,
  group: "",
  tags: [],
  when: "",
  effect: { type: "block", reason: "" },
};

function MetaRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rule-edit-meta-row">
      <span className="rule-edit-meta-label">{label}</span>
      <div className="rule-edit-meta-value">{children}</div>
    </div>
  );
}

export function RuleEditForm({
  isOpen,
  readOnly,
  form,
  onChange,
  onSave,
  onCancel,
  isEditing,
  saveDisabled,
  sidebarView,
  onViewChange,
  yamlContent,
  onYamlChange,
  onYamlSave,
  conflictWarning,
}: RuleEditFormProps) {
  const [tagInput, setTagInput] = useState("");
  const [knownTags, setKnownTags] = useState<string[]>([]);
  const [tagsError, setTagsError] = useState(false);
  const tagListId = useId();

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/rules/tags", { signal: controller.signal })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((data) => { setKnownTags(data.tags || []); setTagsError(false); })
      .catch((err) => { if (!controller.signal.aborted) { console.error("Failed to fetch rule tags:", err); setKnownTags([]); setTagsError(true); } });
    return () => controller.abort();
  }, []);

  const tagSuggestions = useMemo(
    () => knownTags.filter((t) => !form.tags.includes(t)),
    [knownTags, form.tags],
  );

  const set = <K extends keyof RuleFormData>(key: K, value: RuleFormData[K]) =>
    onChange({ ...form, [key]: value });

  const setEffect = (updates: Record<string, unknown>) =>
    onChange({ ...form, effect: { ...form.effect, ...updates } });

  const changeEffectType = (type: string) => {
    const defaults: Record<string, Record<string, unknown>> = {
      block: { type: "block", reason: "" },
      set_variable: { type: "set_variable", variable: "", value: "" },
      inject_context: { type: "inject_context", template: "" },
      mcp_call: {
        type: "mcp_call",
        server: "",
        tool: "",
        arguments: {},
        background: false,
      },
      observe: { type: "observe", category: "", message: "" },
    };
    onChange({
      ...form,
      effect: (defaults[type] || { type }) as RuleFormData["effect"],
    });
  };

  const addTag = () => {
    const tag = tagInput.trim();
    if (tag && !form.tags.includes(tag)) {
      set("tags", [...form.tags, tag]);
    }
    setTagInput("");
  };

  const title = readOnly
    ? form.name || "Rule"
    : isEditing
      ? "Edit Rule"
      : "Create Rule";

  const headerContent = (
    <>
      <div className="sidebar-tab-bar">
        <button
          type="button"
          className={`sidebar-tab ${sidebarView !== "yaml" ? "sidebar-tab--active" : ""}`}
          onClick={() => onViewChange("form")}
        >
          Form
        </button>
        <button
          type="button"
          className={`sidebar-tab ${sidebarView === "yaml" ? "sidebar-tab--active" : ""}`}
          onClick={() => onViewChange("yaml")}
        >
          YAML
        </button>
      </div>
    </>
  );

  const footer = !readOnly ? (
    <>
      <button className="rule-edit-btn" onClick={onCancel} type="button">
        Cancel
      </button>
      <button
        className="rule-edit-btn rule-edit-btn--primary"
        onClick={sidebarView === "yaml" ? onYamlSave : onSave}
        disabled={saveDisabled}
        type="button"
      >
        {isEditing ? "Save" : "Create"}
      </button>
    </>
  ) : undefined;

  return (
    <SidebarPanel
      isOpen={isOpen}
      onClose={onCancel}
      title={title}
      headerContent={headerContent}
      footer={footer}
    >
      {sidebarView === "yaml" ? (
        <div className="rule-edit-yaml-view">
          <CodeMirrorEditor
            content={yamlContent}
            language="yaml"
            readOnly={readOnly}
            onChange={onYamlChange}
            onSave={!readOnly ? onYamlSave : undefined}
          />
        </div>
      ) : readOnly ? (
        <ReadOnlyView form={form} />
      ) : (
        <>
          {/* Name */}
          <div className="rule-edit-section">
            <label className="rule-edit-field">
              <span className="rule-edit-label">Name *</span>
              <input
                className="rule-edit-input"
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="my-rule"
              />
            </label>
          </div>

          {/* Meta */}
          <div className="rule-edit-meta">
            <MetaRow label="Event">
              <select
                className="rule-edit-input"
                value={form.event}
                onChange={(e) => set("event", e.target.value)}
              >
                {RULE_EVENTS.map((ev) => (
                  <option key={ev} value={ev}>
                    {ev}
                  </option>
                ))}
              </select>
            </MetaRow>
            <MetaRow label="Priority">
              <input
                className="rule-edit-input"
                type="number"
                value={form.priority}
                onChange={(e) => set("priority", Number(e.target.value))}
                min={0}
              />
            </MetaRow>
            {conflictWarning && (
              <div className="rule-edit-conflict-warning">{conflictWarning}</div>
            )}
            <MetaRow label="Group">
              <input
                className="rule-edit-input"
                value={form.group}
                onChange={(e) => set("group", e.target.value)}
                placeholder="(none)"
              />
            </MetaRow>
          </div>

          {/* Condition */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Condition</h4>
            <div className="rule-edit-field">
              <span className="rule-edit-label">When (expression)</span>
              <ExpressionBuilder
                value={form.when}
                onChange={(v) => set("when", v)}
              />
            </div>
          </div>

          {/* Effect */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Effect</h4>
            <label className="rule-edit-field">
              <span className="rule-edit-label">Type</span>
              <select
                className="rule-edit-input"
                value={form.effect.type}
                onChange={(e) => changeEffectType(e.target.value)}
              >
                {EFFECT_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <EffectFields effect={form.effect} onChange={setEffect} />
          </div>

          {/* Tags */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Tags</h4>
            <div className="rule-edit-chips">
              {form.tags.map((tag) => (
                <span key={tag} className="rule-edit-chip">
                  {tag}
                  <button
                    type="button"
                    className="rule-edit-chip-remove"
                    onClick={() =>
                      set(
                        "tags",
                        form.tags.filter((t) => t !== tag),
                      )
                    }
                  >
                    &times;
                  </button>
                </span>
              ))}
              <div className="rule-edit-chip-add">
                <input
                  className="rule-edit-input"
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      addTag();
                    }
                  }}
                  placeholder="Add tag..."
                  list={tagListId}
                />
                {tagsError && (
                  <span style={{ color: "var(--color-text-tertiary)", fontSize: 11 }}>
                    Could not load tag suggestions
                  </span>
                )}
                {tagSuggestions.length > 0 && (
                  <datalist id={tagListId}>
                    {tagSuggestions.map((t) => (
                      <option key={t} value={t} />
                    ))}
                  </datalist>
                )}
              </div>
            </div>
          </div>

          {/* Description */}
          <div className="rule-edit-section">
            <h4 className="rule-edit-section-title">Description</h4>
            <textarea
              className="rule-edit-textarea"
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="What this rule does..."
              rows={3}
            />
          </div>
        </>
      )}
    </SidebarPanel>
  );
}

function ReadOnlyView({ form }: { form: RuleFormData }) {
  return (
    <>
      <div className="rule-edit-meta">
        <MetaRow label="Event">
          <span>{form.event}</span>
        </MetaRow>
        <MetaRow label="Priority">
          <span>{form.priority}</span>
        </MetaRow>
        <MetaRow label="Enabled">
          <span>{form.enabled ? "Yes" : "No"}</span>
        </MetaRow>
        {form.group && (
          <MetaRow label="Group">
            <span>{form.group}</span>
          </MetaRow>
        )}
      </div>
      {form.description && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Description</h4>
          <span className="rule-edit-readonly-value">{form.description}</span>
        </div>
      )}
      {form.when && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Condition</h4>
          <code className="rule-edit-readonly-value rule-edit-mono">
            {form.when}
          </code>
        </div>
      )}
      <div className="rule-edit-section">
        <h4 className="rule-edit-section-title">Effect</h4>
        <pre className="rule-edit-readonly-pre">
          {JSON.stringify(form.effect, null, 2)}
        </pre>
      </div>
      {form.tags.length > 0 && (
        <div className="rule-edit-section">
          <h4 className="rule-edit-section-title">Tags</h4>
          <div className="rule-edit-chips">
            {form.tags.map((tag) => (
              <span key={tag} className="rule-edit-chip">
                {tag}
              </span>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

function EffectFields({
  effect,
  onChange,
}: {
  effect: Record<string, unknown>;
  onChange: (u: Record<string, unknown>) => void;
}) {
  const type = effect.type as string;

  if (type === "block") {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Reason</span>
          <textarea
            className="rule-edit-textarea"
            value={(effect.reason as string) ?? ""}
            onChange={(e) => onChange({ reason: e.target.value })}
            placeholder="Why this is blocked..."
            rows={2}
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Tools (comma-separated)</span>
          <input
            className="rule-edit-input"
            value={
              Array.isArray(effect.tools)
                ? (effect.tools as string[]).join(", ")
                : ""
            }
            onChange={(e) =>
              onChange({
                tools: e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            placeholder="Edit, Write"
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">MCP Tools (comma-separated)</span>
          <input
            className="rule-edit-input"
            value={
              Array.isArray(effect.mcp_tools)
                ? (effect.mcp_tools as string[]).join(", ")
                : ""
            }
            onChange={(e) =>
              onChange({
                mcp_tools: e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
            placeholder="gobby-tasks.create_task"
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Command pattern</span>
          <input
            className="rule-edit-input rule-edit-mono"
            value={(effect.command_pattern as string) ?? ""}
            onChange={(e) =>
              onChange({ command_pattern: e.target.value || undefined })
            }
            placeholder="regex pattern"
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Command NOT pattern</span>
          <input
            className="rule-edit-input rule-edit-mono"
            value={(effect.command_not_pattern as string) ?? ""}
            onChange={(e) =>
              onChange({ command_not_pattern: e.target.value || undefined })
            }
            placeholder="regex exclusion"
          />
        </label>
      </>
    );
  }

  if (type === "set_variable") {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Variable name</span>
          <input
            className="rule-edit-input rule-edit-mono"
            value={(effect.variable as string) ?? ""}
            onChange={(e) => onChange({ variable: e.target.value })}
            placeholder="my_var"
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Value</span>
          <textarea
            className="rule-edit-textarea rule-edit-mono"
            value={(effect.value as string) ?? ""}
            onChange={(e) => onChange({ value: e.target.value })}
            placeholder="value or expression"
            rows={2}
          />
        </label>
      </>
    );
  }

  if (type === "inject_context") {
    return (
      <div className="rule-edit-field">
        <span className="rule-edit-label">Template</span>
        <div className="rule-edit-codemirror">
          <CodeMirrorEditor
            content={(effect.template as string) ?? ""}
            language="markdown"
            onChange={(v) => onChange({ template: v })}
          />
        </div>
      </div>
    );
  }

  if (type === "mcp_call") {
    return <McpCallFields effect={effect} onChange={onChange} />;
  }

  if (type === "observe") {
    return (
      <>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Category</span>
          <input
            className="rule-edit-input"
            value={(effect.category as string) ?? ""}
            onChange={(e) => onChange({ category: e.target.value })}
            placeholder="audit"
          />
        </label>
        <label className="rule-edit-field">
          <span className="rule-edit-label">Message</span>
          <textarea
            className="rule-edit-textarea"
            value={(effect.message as string) ?? ""}
            onChange={(e) => onChange({ message: e.target.value })}
            placeholder="Log message..."
            rows={2}
          />
        </label>
      </>
    );
  }

  return null;
}

function McpCallFields({
  effect,
  onChange,
}: {
  effect: Record<string, unknown>;
  onChange: (u: Record<string, unknown>) => void;
}) {
  const { servers, toolsByServer, fetchToolSchema } = useMcp();
  const [schema, setSchema] = useState<McpToolSchema | null>(null);
  const [loadingSchema, setLoadingSchema] = useState(false);

  const selectedServer = (effect.server as string) ?? "";
  const selectedTool = (effect.tool as string) ?? "";
  const args = (effect.arguments as Record<string, unknown>) ?? {};

  useEffect(() => {
    if (!selectedServer || !selectedTool) {
      setSchema(null);
      return;
    }
    let cancelled = false;
    setLoadingSchema(true);
    fetchToolSchema(selectedServer, selectedTool)
      .then((s) => {
        if (!cancelled) setSchema(s);
      })
      .finally(() => {
        if (!cancelled) setLoadingSchema(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedServer, selectedTool, fetchToolSchema]);

  const connectedServers = servers.filter((s) => s.connected);
  const serverNames = connectedServers.map((s) => s.name);
  const availableTools = toolsByServer[selectedServer] ?? [];
  const toolNames = availableTools.map((t) => t.name);

  return (
    <>
      <label className="rule-edit-field">
        <span className="rule-edit-label">Server</span>
        <select
          className="rule-edit-input"
          value={selectedServer}
          onChange={(e) => {
            onChange({ server: e.target.value, tool: "", arguments: {} });
          }}
        >
          <option value="">Select server...</option>
          {selectedServer && !serverNames.includes(selectedServer) && (
            <option value={selectedServer}>
              {selectedServer} (disconnected)
            </option>
          )}
          {connectedServers.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
      </label>
      <label className="rule-edit-field">
        <span className="rule-edit-label">Tool</span>
        <select
          className="rule-edit-input"
          value={selectedTool}
          disabled={!selectedServer}
          onChange={(e) => {
            onChange({ tool: e.target.value, arguments: {} });
          }}
        >
          <option value="">
            {selectedServer ? "Select tool..." : "Select a server first"}
          </option>
          {selectedTool && !toolNames.includes(selectedTool) && (
            <option value={selectedTool}>{selectedTool}</option>
          )}
          {availableTools.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      {schema?.description && (
        <div className="rule-edit-tool-desc">{schema.description}</div>
      )}
      <div className="rule-edit-field">
        <span className="rule-edit-label">
          Arguments
          {loadingSchema && (
            <span className="rule-edit-hint"> (loading schema...)</span>
          )}
        </span>
        <SchemaArgEditor
          args={args}
          inputSchema={schema?.inputSchema ?? null}
          onChange={(newArgs) => onChange({ arguments: newArgs })}
        />
      </div>
      <label
        className="rule-edit-field"
        style={{ flexDirection: "row", alignItems: "center", gap: 8 }}
      >
        <input
          type="checkbox"
          checked={!!effect.background}
          onChange={(e) => onChange({ background: e.target.checked })}
        />
        <span className="rule-edit-label" style={{ textTransform: "none" }}>
          Run in background
        </span>
      </label>
    </>
  );
}

function SchemaArgEditor({
  args,
  inputSchema,
  onChange,
}: {
  args: Record<string, unknown>;
  inputSchema: Record<string, unknown> | null;
  onChange: (args: Record<string, unknown>) => void;
}) {
  const [addingArg, setAddingArg] = useState(false);
  const addArgRef = useRef<HTMLSelectElement>(null);
  const blurTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => { if (addingArg) addArgRef.current?.focus(); }, [addingArg]);
  useEffect(() => () => { if (blurTimerRef.current) clearTimeout(blurTimerRef.current); }, []);

  const properties =
    (inputSchema?.properties as Record<
      string,
      Record<string, unknown>
    >) ?? {};
  const requiredKeys = (inputSchema?.required as string[]) ?? [];
  const requiredSet = new Set(requiredKeys);
  const schemaKeys = Object.keys(properties);

  // Required keys first, then keys already in args, deduped
  const visibleKeys = [
    ...requiredKeys,
    ...Object.keys(args).filter((k) => !requiredSet.has(k)),
  ].filter((k, i, arr) => arr.indexOf(k) === i);

  // Optional schema keys not yet in args
  const availableOptional = schemaKeys.filter(
    (k) => !requiredSet.has(k) && !(k in args),
  );

  const updateArg = (key: string, value: unknown) => {
    onChange({ ...args, [key]: value });
  };

  const removeArg = (key: string) => {
    const next = { ...args };
    delete next[key];
    onChange(next);
  };

  const addOptionalArg = (key: string) => {
    const prop = properties[key];
    const propType = (prop?.type as string) ?? "string";
    const defaultVal =
      propType === "boolean"
        ? false
        : propType === "number" || propType === "integer"
          ? 0
          : "";
    onChange({ ...args, [key]: defaultVal });
    setAddingArg(false);
  };

  return (
    <div className="rule-edit-kv">
      {visibleKeys.map((key) => {
        const prop = properties[key];
        const isRequired = requiredSet.has(key);
        const isSchema = key in properties;
        const propType = (prop?.type as string) ?? "string";
        const description = prop?.description as string | undefined;

        return (
          <div key={key} className="rule-edit-kv-row">
            {isSchema ? (
              <span className="rule-edit-kv-label" title={description}>
                {key}
                {isRequired && (
                  <span className="rule-edit-required">*</span>
                )}
              </span>
            ) : (
              <input
                className="rule-edit-input"
                value={key}
                onChange={(e) => {
                  const newKey = e.target.value;
                  const next: Record<string, unknown> = {};
                  for (const [k, v] of Object.entries(args)) {
                    next[k === key ? newKey : k] = v;
                  }
                  onChange(next);
                }}
                placeholder="key"
              />
            )}
            <ArgValueInput
              type={propType}
              value={args[key]}
              onChange={(v) => updateArg(key, v)}
            />
            {!isRequired && (
              <button
                type="button"
                className="rule-edit-kv-remove"
                onClick={() => removeArg(key)}
              >
                &times;
              </button>
            )}
          </div>
        );
      })}
      {addingArg ? (
        <div className="rule-edit-kv-row">
          <select
            ref={addArgRef}
            className="rule-edit-input"
            style={{ flex: 1 }}
            value=""
            onChange={(e) => {
              if (e.target.value === "__custom__") {
                onChange({ ...args, "": "" });
                setAddingArg(false);
              } else if (e.target.value) {
                addOptionalArg(e.target.value);
              }
            }}
            aria-label="Choose argument"
            onBlur={() => { blurTimerRef.current = setTimeout(() => setAddingArg(false), 150); }}
          >
            <option value="">Choose argument...</option>
            {availableOptional.map((k) => {
              const desc = properties[k]?.description as string | undefined;
              return (
                <option key={k} value={k}>
                  {k}{desc ? ` — ${desc}` : ""}
                </option>
              );
            })}
            <option value="__custom__">Custom argument...</option>
          </select>
        </div>
      ) : (
        <button
          type="button"
          className="rule-edit-kv-add"
          onClick={() => setAddingArg(true)}
        >
          + Add argument
        </button>
      )}
    </div>
  );
}

function ArgValueInput({
  type,
  value,
  onChange,
}: {
  type: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (type === "boolean") {
    return (
      <input
        type="checkbox"
        checked={!!value}
        onChange={(e) => onChange(e.target.checked)}
        style={{ margin: "0 auto" }}
      />
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <input
        className="rule-edit-input"
        type="number"
        value={value == null ? "" : String(value)}
        onChange={(e) =>
          onChange(e.target.value === "" ? null : Number(e.target.value))
        }
        placeholder="0"
      />
    );
  }
  return (
    <input
      className="rule-edit-input"
      value={value == null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
      placeholder="value"
    />
  );
}
