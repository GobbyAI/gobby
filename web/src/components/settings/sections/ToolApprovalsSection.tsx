import { useId, useState } from "react";
import { BoundedSelectField, StringListField, TypedListField } from "../fields";
import { DetailActionButton, TextField } from "../../activity/fields";
import type { FieldOption } from "../../activity/fields";
import {
  SchemaSelectField,
  Subsection,
  SwitchConfigField,
} from "./configFields";
import { asString, asTypedList } from "./configAccessors";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import { useSettingsSectionContext } from "./SettingsSectionContext";
import { Input } from "../../ui/Input";

/**
 * Tool Approvals settings section (audit IA section 13). Two surfaces live here:
 * the draft-backed `tool_approval.*` DaemonConfig rows (enablement, default
 * policy, and the per-tool `policies` "fix" row promoted from a text fallback to
 * a structured editor), saved through the #17108 section footer; and the global
 * auto-allow rules — a standalone `/api/config/tool-approvals/global` surface
 * threaded through context like the rules-enforcement toggle, saved immediately
 * by its own control rather than the config draft.
 */

interface ToolApprovalPolicy {
  server_pattern: string;
  tool_pattern: string;
  policy: string;
}

const TOOL_APPROVAL_PATHS = [
  "tool_approval.enabled",
  "tool_approval.default_policy",
  "tool_approval.policies",
] as const;

const OWNED_PATHS: readonly string[] = [...TOOL_APPROVAL_PATHS];

// The three `ToolApprovalPolicy.policy` literals. Rendered through
// BoundedSelectField (not SchemaSelectField) because list-item fields have no
// dotted schema path; an out-of-set stored value is surfaced as "(unsupported)".
const POLICY_OPTIONS: FieldOption[] = [
  { value: "auto", label: "Auto-allow" },
  { value: "approve_once", label: "Approve once" },
  { value: "always_ask", label: "Always ask" },
];

function arraysEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function policyLabel(policy: ToolApprovalPolicy, index: number): string {
  const server = asString(policy.server_pattern) || "*";
  const tool = asString(policy.tool_pattern) || "*";
  const pair = `${server} / ${tool}`;
  return pair === "* / *" ? `Per-tool policy ${index + 1}` : pair;
}

function EnablementGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Approval prompts"
      hint="Whether interactive web chat pauses for tool-call approval, and the policy applied when no per-tool rule matches."
    >
      <SwitchConfigField
        fields={fields}
        path="tool_approval.enabled"
        label="Enable tool approval prompts"
        ariaLabel="Enable tool approval prompts"
      />
      <SchemaSelectField
        fields={fields}
        path="tool_approval.default_policy"
        label="Default policy"
        ariaLabel="Default approval policy"
      />
    </Subsection>
  );
}

function PoliciesGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Per-tool policies"
      hint="Server/tool glob patterns matched in order; the first match wins, otherwise the default policy applies."
    >
      <TypedListField<ToolApprovalPolicy>
        label="Per-tool policies"
        ariaLabel="Per-tool policy"
        value={asTypedList<ToolApprovalPolicy>(
          fields.getValue("tool_approval.policies"),
        )}
        addLabel="Add policy"
        createItem={() => ({
          server_pattern: "*",
          tool_pattern: "*",
          policy: "always_ask",
        })}
        itemLabel={policyLabel}
        onChange={(value) => fields.setValue("tool_approval.policies", value)}
        renderItem={(policy, onItemChange, index) => (
          <>
            <TextField
              label="Server pattern"
              ariaLabel={`Per-tool policy ${index + 1} server pattern`}
              value={asString(policy.server_pattern)}
              placeholder="*"
              onChange={(value) =>
                onItemChange({ ...policy, server_pattern: value })
              }
            />
            <TextField
              label="Tool pattern"
              ariaLabel={`Per-tool policy ${index + 1} tool pattern`}
              value={asString(policy.tool_pattern)}
              placeholder="*"
              onChange={(value) =>
                onItemChange({ ...policy, tool_pattern: value })
              }
            />
            <BoundedSelectField
              label="Policy"
              ariaLabel={`Per-tool policy ${index + 1} approval`}
              value={asString(policy.policy)}
              options={POLICY_OPTIONS}
              onChange={(value) => onItemChange({ ...policy, policy: value })}
            />
          </>
        )}
      />
    </Subsection>
  );
}

function BuiltInExemptions({ exemptions }: { exemptions: string[] }) {
  const labelId = useId();
  return (
    <div className="flex flex-col gap-2" role="group" aria-labelledby={labelId}>
      <span
        id={labelId}
        className="text-base leading-[1.3] font-medium text-foreground"
      >
        Built-in exemptions
      </span>
      {exemptions.length > 0 ? (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {exemptions.map((rule, index) => (
            <li key={rule} className="flex items-center gap-2">
              <Input
                type="text"
                className="flex-1 [font-family:inherit] text-foreground pointer-coarse:min-h-11"
                value={rule}
                disabled
                aria-label={`Built-in exemption ${index + 1}`}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm leading-[1.4] text-foreground-muted">None.</p>
      )}
      <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
        Always auto-allowed and read-only.
      </p>
    </div>
  );
}

interface GlobalRulesGroupProps {
  rules: string[];
  defaultRules: string[];
  builtInExemptions: string[];
  onSave?: (rules: string[]) => Promise<boolean>;
}

function GlobalRulesGroup({
  rules,
  defaultRules,
  builtInExemptions,
  onSave,
}: GlobalRulesGroupProps) {
  const [draft, setDraft] = useState<string[]>(rules);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Re-seed from the live surface whenever the provider refetches it, using the
  // render-time "adjust state on prop change" pattern (no effect): `rules` is a
  // stable reference until a save or external refetch replaces it.
  const [seededRules, setSeededRules] = useState<string[]>(rules);
  if (rules !== seededRules) {
    setSeededRules(rules);
    setDraft(rules);
    setError(null);
  }

  if (!onSave) {
    return (
      <Subsection
        title="Global auto-allow rules"
        hint="Tool patterns auto-allowed across providers for interactive web chat."
      >
        <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
          Global approval rules are unavailable.
        </p>
      </Subsection>
    );
  }

  const dirty = !arraysEqual(draft, rules);
  const canReset = !arraysEqual(draft, defaultRules);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const cleaned = draft.map((rule) => rule.trim()).filter(Boolean);
      const ok = await onSave(cleaned);
      if (!ok) setError("Could not save approval rules.");
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not save approval rules.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Subsection
      title="Global auto-allow rules"
      hint="Tool patterns auto-allowed across providers for interactive web chat. Saved immediately, separate from the config draft above."
    >
      <StringListField
        label="Rules"
        ariaLabel="Auto-allow rule"
        value={draft}
        onChange={(value) => {
          setError(null);
          setDraft(value);
        }}
        addLabel="Add rule"
        placeholder="tool:Write or mcp:gobby-tasks:*"
        disabled={saving}
      />
      <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
        Recommended defaults: {defaultRules.join(", ") || "none"}
      </p>
      <BuiltInExemptions exemptions={builtInExemptions} />
      <div className="flex flex-wrap gap-2">
        <DetailActionButton
          label="Reset to defaults"
          variant="ghost"
          onClick={() => {
            setError(null);
            setDraft(defaultRules);
          }}
          disabled={saving || !canReset}
        />
        <DetailActionButton
          label={saving ? "Saving…" : "Save rules"}
          variant="accent"
          onClick={() => {
            void handleSave();
          }}
          disabled={saving || !dirty}
        />
      </div>
      {error ? (
        <p
          className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground"
          role="alert"
        >
          {error}
        </p>
      ) : null}
    </Subsection>
  );
}

export function ToolApprovalsSection() {
  const {
    globalApprovalRules,
    defaultApprovalRules,
    builtInApprovalExemptions,
    saveGlobalApprovalRules,
  } = useSettingsSectionContext();
  return (
    <SettingsSection sectionId="tool-approvals" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <EnablementGroup fields={fields} />
          <PoliciesGroup fields={fields} />
          <GlobalRulesGroup
            rules={globalApprovalRules ?? []}
            defaultRules={defaultApprovalRules ?? []}
            builtInExemptions={builtInApprovalExemptions ?? []}
            onSave={saveGlobalApprovalRules}
          />
        </>
      )}
    </SettingsSection>
  );
}
