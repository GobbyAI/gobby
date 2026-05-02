import { useState, useEffect, useMemo, useId, useRef } from "react";
import { SidebarPanel } from "../shared/SidebarPanel";
import { CodeMirrorEditor } from "../shared/CodeMirrorEditor";
import { ExpressionBuilder } from "./ExpressionBuilder";
import { useMcp, type McpToolSchema } from "../../hooks/useMcp";
import type { RuleFormData } from "./ruleFormData";
import { cn } from "../../lib/utils";

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

const META_WRAP_CLS = 'border-b border-[var(--border)] px-5 py-3'
const META_ROW_CLS = 'flex items-center justify-between py-1 text-[length:var(--text-sm)]'
const META_LABEL_CLS = 'mr-3 shrink-0 text-[var(--text-muted)]'
const META_VALUE_CLS = 'max-w-[220px] flex-1 text-right [&>select]:w-full [&>input[type="number"]]:w-full'

const SECTION_CLS = 'flex flex-col gap-1.5 border-b border-[var(--border)] px-5 py-3'
const SECTION_TITLE_CLS = 'm-0 mb-1 text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]'

const FIELD_CLS = 'flex flex-col gap-1'
const FIELD_INLINE_CLS = 'flex flex-row items-center gap-2'
const LABEL_CLS = 'text-[length:var(--text-xs)] uppercase tracking-[0.3px] text-[var(--text-muted)]'
const LABEL_INLINE_CLS = 'text-[length:var(--text-xs)] normal-case tracking-normal text-[var(--text-muted)]'

const INPUT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none transition-colors duration-150 focus:border-[var(--accent,var(--color-agent))] focus:[box-shadow:0_0_0_2px_color-mix(in_srgb,var(--accent,var(--color-agent))_20%,transparent)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const TEXTAREA_CLS =
  'min-h-[60px] resize-y rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none transition-colors duration-150 focus:border-[var(--accent,var(--color-agent))] focus:[box-shadow:0_0_0_2px_color-mix(in_srgb,var(--accent,var(--color-agent))_20%,transparent)]'
const MONO_CLS = 'font-mono text-[length:var(--text-sm)]'

const CHIPS_WRAP_CLS = 'flex flex-wrap items-center gap-1.5'
const CHIP_CLS =
  'inline-flex items-center gap-1 rounded-xl border border-[var(--border)] bg-[var(--bg-tertiary)] py-0.5 pl-2.5 pr-2 text-[length:var(--text-sm)] text-[var(--text-primary)]'
const CHIP_REMOVE_CLS =
  'cursor-pointer border-0 bg-transparent px-0.5 text-[length:var(--text-base)] leading-none text-[var(--text-muted)] transition-colors duration-150 hover:text-[var(--color-error)]'
const CHIP_ADD_CLS = 'flex items-center gap-1'
const CHIP_INPUT_CLS = 'w-[120px] px-2 py-0.5 text-[length:var(--text-sm)]'

const CONFLICT_CLS =
  'my-1 rounded-md border border-[color-mix(in_srgb,var(--color-warning,var(--color-warning-foreground))_40%,transparent)] bg-[color-mix(in_srgb,var(--color-warning,var(--color-warning-foreground))_15%,transparent)] px-2.5 py-1.5 text-[length:var(--text-sm)] leading-snug text-[var(--color-warning-foreground)]'

const KV_WRAP_CLS = 'flex flex-col gap-1.5'
const KV_ROW_CLS = 'flex items-center gap-1.5'
const KV_INPUT_CLS = 'flex-1 px-2 py-1 text-[length:var(--text-sm)]'
const KV_LABEL_CLS =
  'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap px-2 py-1 text-[length:var(--text-sm)] text-[var(--text-primary)]'
const KV_REMOVE_CLS =
  'cursor-pointer border-0 bg-transparent px-1 text-[length:var(--text-base)] text-[var(--text-muted)] transition-colors duration-150 hover:text-[var(--color-error)] pointer-coarse:min-h-11 pointer-coarse:px-2'
const KV_ADD_CLS =
  'cursor-pointer self-start rounded-md border border-dashed border-[var(--border)] bg-transparent px-2.5 py-1 text-[length:var(--text-xs)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:px-3'

const REQUIRED_CLS = 'ml-0.5 text-[var(--color-error)]'
const TOOL_DESC_CLS = 'py-0.5 text-[length:var(--text-xs)] leading-snug text-[var(--text-muted)]'
const HINT_CLS = 'font-normal normal-case tracking-normal opacity-70'
const TAGS_ERROR_CLS = 'text-[length:var(--text-xs)] text-[var(--text-muted)]'

const READONLY_VALUE_CLS = 'break-words text-[length:var(--text-md)] text-[var(--text-primary)]'
const READONLY_PRE_CLS =
  'm-0 whitespace-pre-wrap rounded border border-[var(--border)] bg-[var(--bg-primary)] p-2 text-[length:var(--text-sm)] leading-normal text-[var(--text-secondary)]'

const CODEMIRROR_WRAP_CLS =
  'min-h-[150px] max-h-[300px] overflow-hidden rounded-md border border-[var(--border)] [&_.codemirror-container]:h-[150px]'
const YAML_VIEW_CLS = 'h-full [&_.codemirror-container]:h-full'

const BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1 text-[length:var(--text-sm)] font-medium text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:bg-[var(--bg-secondary)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:border-[var(--accent-hover)] hover:bg-[var(--accent-hover)]'

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

function MetaRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className={META_ROW_CLS}>
      <span className={META_LABEL_CLS}>{label}</span>
      <div className={META_VALUE_CLS}>{children}</div>
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
      <button className={BTN_CLS} onClick={onCancel} type="button">
        Cancel
      </button>
      <button
        className={cn(BTN_CLS, BTN_PRIMARY_CLS)}
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
        <div className={YAML_VIEW_CLS}>
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
          <div className={SECTION_CLS}>
            <label className={FIELD_CLS}>
              <span className={LABEL_CLS}>Name *</span>
              <input
                className={INPUT_CLS}
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="my-rule"
              />
            </label>
          </div>

          {/* Meta */}
          <div className={META_WRAP_CLS}>
            <MetaRow label="Event">
              <select
                className={INPUT_CLS}
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
                className={INPUT_CLS}
                type="number"
                value={form.priority}
                onChange={(e) => set("priority", Number(e.target.value))}
                min={0}
              />
            </MetaRow>
            {conflictWarning && (
              <div className={CONFLICT_CLS}>{conflictWarning}</div>
            )}
            <MetaRow label="Group">
              <input
                className={INPUT_CLS}
                value={form.group}
                onChange={(e) => set("group", e.target.value)}
                placeholder="(none)"
              />
            </MetaRow>
          </div>

          {/* Condition */}
          <div className={SECTION_CLS}>
            <h4 className={SECTION_TITLE_CLS}>Condition</h4>
            <div className={FIELD_CLS}>
              <span className={LABEL_CLS}>When (expression)</span>
              <ExpressionBuilder
                value={form.when}
                onChange={(v) => set("when", v)}
              />
            </div>
          </div>

          {/* Effect */}
          <div className={SECTION_CLS}>
            <h4 className={SECTION_TITLE_CLS}>Effect</h4>
            <label className={FIELD_CLS}>
              <span className={LABEL_CLS}>Type</span>
              <select
                className={INPUT_CLS}
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
          <div className={SECTION_CLS}>
            <h4 className={SECTION_TITLE_CLS}>Tags</h4>
            <div className={CHIPS_WRAP_CLS}>
              {form.tags.map((tag) => (
                <span key={tag} className={CHIP_CLS}>
                  {tag}
                  <button
                    type="button"
                    className={CHIP_REMOVE_CLS}
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
              <div className={CHIP_ADD_CLS}>
                <input
                  className={cn(INPUT_CLS, CHIP_INPUT_CLS)}
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
                  <span className={TAGS_ERROR_CLS}>
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
          <div className={SECTION_CLS}>
            <h4 className={SECTION_TITLE_CLS}>Description</h4>
            <textarea
              className={TEXTAREA_CLS}
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
      <div className={META_WRAP_CLS}>
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
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Description</h4>
          <span className={READONLY_VALUE_CLS}>{form.description}</span>
        </div>
      )}
      {form.when && (
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Condition</h4>
          <code className={cn(READONLY_VALUE_CLS, MONO_CLS)}>
            {form.when}
          </code>
        </div>
      )}
      <div className={SECTION_CLS}>
        <h4 className={SECTION_TITLE_CLS}>Effect</h4>
        <pre className={READONLY_PRE_CLS}>
          {JSON.stringify(form.effect, null, 2)}
        </pre>
      </div>
      {form.tags.length > 0 && (
        <div className={SECTION_CLS}>
          <h4 className={SECTION_TITLE_CLS}>Tags</h4>
          <div className={CHIPS_WRAP_CLS}>
            {form.tags.map((tag) => (
              <span key={tag} className={CHIP_CLS}>
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
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Reason</span>
          <textarea
            className={TEXTAREA_CLS}
            value={(effect.reason as string) ?? ""}
            onChange={(e) => onChange({ reason: e.target.value })}
            placeholder="Why this is blocked..."
            rows={2}
          />
        </label>
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Tools (comma-separated)</span>
          <input
            className={INPUT_CLS}
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
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>MCP Tools (comma-separated)</span>
          <input
            className={INPUT_CLS}
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
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Command pattern</span>
          <input
            className={cn(INPUT_CLS, MONO_CLS)}
            value={(effect.command_pattern as string) ?? ""}
            onChange={(e) =>
              onChange({ command_pattern: e.target.value || undefined })
            }
            placeholder="regex pattern"
          />
        </label>
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Command NOT pattern</span>
          <input
            className={cn(INPUT_CLS, MONO_CLS)}
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
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Variable name</span>
          <input
            className={cn(INPUT_CLS, MONO_CLS)}
            value={(effect.variable as string) ?? ""}
            onChange={(e) => onChange({ variable: e.target.value })}
            placeholder="my_var"
          />
        </label>
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Value</span>
          <textarea
            className={cn(TEXTAREA_CLS, MONO_CLS)}
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
      <div className={FIELD_CLS}>
        <span className={LABEL_CLS}>Template</span>
        <div className={CODEMIRROR_WRAP_CLS}>
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
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Category</span>
          <input
            className={INPUT_CLS}
            value={(effect.category as string) ?? ""}
            onChange={(e) => onChange({ category: e.target.value })}
            placeholder="audit"
          />
        </label>
        <label className={FIELD_CLS}>
          <span className={LABEL_CLS}>Message</span>
          <textarea
            className={TEXTAREA_CLS}
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

  const connectedServers = servers.filter((s) => s.state === "connected");
  const serverNames = connectedServers.map((s) => s.name);
  const availableTools = toolsByServer[selectedServer] ?? [];
  const toolNames = availableTools.map((t) => t.name);

  return (
    <>
      <label className={FIELD_CLS}>
        <span className={LABEL_CLS}>Server</span>
        <select
          className={INPUT_CLS}
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
      <label className={FIELD_CLS}>
        <span className={LABEL_CLS}>Tool</span>
        <select
          className={INPUT_CLS}
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
        <div className={TOOL_DESC_CLS}>{schema.description}</div>
      )}
      <div className={FIELD_CLS}>
        <span className={LABEL_CLS}>
          Arguments
          {loadingSchema && (
            <span className={HINT_CLS}> (loading schema...)</span>
          )}
        </span>
        <SchemaArgEditor
          args={args}
          inputSchema={schema?.inputSchema ?? null}
          onChange={(newArgs) => onChange({ arguments: newArgs })}
        />
      </div>
      <label className={FIELD_INLINE_CLS}>
        <input
          type="checkbox"
          checked={!!effect.background}
          onChange={(e) => onChange({ background: e.target.checked })}
        />
        <span className={LABEL_INLINE_CLS}>
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
    <div className={KV_WRAP_CLS}>
      {visibleKeys.map((key) => {
        const prop = properties[key];
        const isRequired = requiredSet.has(key);
        const isSchema = key in properties;
        const propType = (prop?.type as string) ?? "string";
        const description = prop?.description as string | undefined;

        return (
          <div key={key} className={KV_ROW_CLS}>
            {isSchema ? (
              <span className={KV_LABEL_CLS} title={description}>
                {key}
                {isRequired && (
                  <span className={REQUIRED_CLS}>*</span>
                )}
              </span>
            ) : (
              <input
                className={cn(INPUT_CLS, KV_INPUT_CLS)}
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
                className={KV_REMOVE_CLS}
                onClick={() => removeArg(key)}
              >
                &times;
              </button>
            )}
          </div>
        );
      })}
      {addingArg ? (
        <div className={KV_ROW_CLS}>
          <select
            ref={addArgRef}
            className={cn(INPUT_CLS, KV_INPUT_CLS)}
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
          className={KV_ADD_CLS}
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
        className="mx-auto"
        checked={!!value}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <input
        className={cn(INPUT_CLS, KV_INPUT_CLS)}
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
      className={cn(INPUT_CLS, KV_INPUT_CLS)}
      value={value == null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
      placeholder="value"
    />
  );
}
