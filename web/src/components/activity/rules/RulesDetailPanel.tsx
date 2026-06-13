import { useCallback, useEffect, useMemo, useState } from "react";

import type { RuleDetail } from "../../../hooks/useRules";
import { cn } from "../../../lib/utils";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import {
  DetailPaneHeader,
  SelectField,
  SwitchField,
  TagsField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import {
  RULE_AUDIENCE_OPTIONS,
  RULE_EVENT_OPTIONS,
  detailToDraft,
  draftToYaml,
  formatRuleSummaryValue,
  isBundledRule,
  type RuleDraft,
  yamlToDraft,
} from "./RulesTabData";
import { RulesYamlView } from "./RulesYamlView";

interface RulesDetailPanelProps {
  detail: RuleDetail | null;
  isLoading: boolean;
  error: string | null;
  onSave: (originalName: string, draft: RuleDraft) => Promise<string | null>;
  onError: (message: string) => void;
  onConfirmLeaveChange: (confirmIfDirty: (next: () => void) => void) => void;
}

function optionsWithCurrent(
  options: Array<{ value: string; label: string }>,
  value: string,
) {
  if (!value || options.some((option) => option.value === value)) return options;
  return [...options, { value, label: value }];
}

function ReadOnlySummary({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="rules-readonly">
      <span className="rules-readonly__label">{label}</span>
      <pre className="rules-readonly__value">{formatRuleSummaryValue(value)}</pre>
    </div>
  );
}

type DetailViewMode = "form" | "yaml";

const RULE_DRAFT_FIELDS: Array<keyof RuleDraft> = [
  "name",
  "description",
  "event",
  "group",
  "priority",
  "tags",
  "audience",
  "agent_scope",
  "enabled",
  "when",
  "match",
  "effects",
  "extra",
];

export function RulesDetailPanel({
  detail,
  isLoading,
  error,
  onSave,
  onError,
  onConfirmLeaveChange,
}: RulesDetailPanelProps) {
  const sourceDraft = useMemo(() => (detail ? detailToDraft(detail) : null), [detail]);
  const sourceKey = sourceDraft?.name ?? "";
  const sourceYamlText = sourceDraft ? draftToYaml(sourceDraft) : "";
  const [detailViewState, setDetailViewState] = useState<{
    sourceKey: string;
    view: DetailViewMode;
  }>({ sourceKey, view: "form" });
  const [yamlState, setYamlState] = useState<{
    sourceKey: string;
    text: string;
    error: string | null;
  }>({ sourceKey, text: sourceYamlText, error: null });

  const handleSave = useCallback(
    async (draft: RuleDraft) => {
      if (!detail) return false;
      try {
        await onSave(detail.name, draft);
        return true;
      } catch (saveError) {
        onError(saveError instanceof Error ? saveError.message : String(saveError));
        return false;
      }
    },
    [detail, onError, onSave],
  );

  const draftState = useDetailDraft<RuleDraft>({
    source: sourceDraft,
    onSave: handleSave,
  });

  if (detailViewState.sourceKey !== sourceKey) {
    setDetailViewState({ sourceKey, view: "form" });
  }
  if (yamlState.sourceKey !== sourceKey) {
    setYamlState({ sourceKey, text: sourceYamlText, error: null });
  }

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  if (isLoading) {
    return <ActivityPanelEmpty body="Loading rule detail..." />;
  }

  if (error) {
    return <ActivityPanelEmpty heading="Rule detail" body={error} />;
  }

  if (!detail || !draftState.draft) {
    return <ActivityPanelEmpty heading="Rules" body="Select a rule to inspect and edit it." />;
  }

  const draft = draftState.draft;
  const detailView = detailViewState.view;
  const yamlText = yamlState.text;
  const yamlError = yamlState.error;
  const bundled = isBundledRule(detail);
  const eventOptions = optionsWithCurrent(
    [{ value: "", label: "Select event" }, ...RULE_EVENT_OPTIONS],
    draft.event,
  );
  const audienceOptions = optionsWithCurrent([...RULE_AUDIENCE_OPTIONS], draft.audience);
  const parseYamlDraft = () => {
    const nextDraft = yamlToDraft(yamlText, draft);
    if (bundled) nextDraft.name = detail.name;
    return nextDraft;
  };
  const applyYamlDraft = (nextDraft: RuleDraft) => {
    for (const field of RULE_DRAFT_FIELDS) {
      draftState.setField(field, nextDraft[field]);
    }
  };
  const handleYamlChange = (value: string) => {
    try {
      const nextDraft = yamlToDraft(value, draft);
      if (bundled) nextDraft.name = detail.name;
      setYamlState({ sourceKey, text: value, error: null });
      applyYamlDraft(nextDraft);
    } catch (yamlParseError) {
      setYamlState({
        sourceKey,
        text: value,
        error: yamlParseError instanceof Error ? yamlParseError.message : "Invalid YAML",
      });
      draftState.setField("name", draft.name);
    }
  };
  const handleViewChange = (view: DetailViewMode) => {
    if (view === "yaml") {
      setYamlState({ sourceKey, text: draftToYaml(draft), error: null });
    }
    setDetailViewState({ sourceKey, view });
  };
  const handleHeaderSave = async () => {
    if (detailView !== "yaml") {
      await draftState.save();
      return;
    }
    try {
      const nextDraft = parseYamlDraft();
      const saved = await draftState.save(nextDraft);
      if (saved) {
        setYamlState({ sourceKey, text: draftToYaml(nextDraft), error: null });
      }
    } catch (yamlParseError) {
      setYamlState({
        sourceKey,
        text: yamlText,
        error: yamlParseError instanceof Error ? yamlParseError.message : "Invalid YAML",
      });
    }
  };
  const handleDiscard = () => {
    draftState.discard();
    setYamlState({ sourceKey, text: sourceYamlText, error: null });
  };

  return (
    <div className="rules-detail">
      <DetailPaneHeader
        title={draft.name || detail.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void handleHeaderSave()}
        onDiscard={handleDiscard}
        actions={
          <div className="flex items-center gap-2">
            <span className="rules-detail__source">{detail.source}</span>
            <div
              className="inline-flex min-h-11 rounded-md border border-border bg-[var(--bg-primary)] p-0.5"
              aria-label="Rule detail view"
            >
              {(["form", "yaml"] as const).map((view) => (
                <button
                  key={view}
                  type="button"
                  className={cn(
                    "min-h-11 min-w-11 rounded px-3 py-2 text-xs font-medium text-muted-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                    detailView === view && "text-[var(--accent)]",
                  )}
                  style={
                    detailView === view
                      ? {
                          backgroundColor: "color-mix(in srgb, var(--accent) 15%, transparent)",
                          boxShadow:
                            "inset 0 0 0 999px color-mix(in srgb, var(--accent) 15%, transparent)",
                          color: "var(--accent)",
                        }
                      : undefined
                  }
                  onClick={() => handleViewChange(view)}
                >
                  {view === "form" ? "Form" : "YAML"}
                </button>
              ))}
            </div>
          </div>
        }
      />
      <div className="rules-detail__body">
        {detailView === "yaml" ? (
          <RulesYamlView
            detail={detail}
            bundled={bundled}
            yamlText={yamlText}
            yamlError={yamlError}
            onYamlChange={handleYamlChange}
            onYamlSave={() => void handleHeaderSave()}
          />
        ) : (
          <>
            {bundled ? (
              <div className="rules-locked-name">
                <span className="rules-locked-name__label">Name</span>
                <span className="rules-locked-name__value">{detail.name}</span>
                <span className="rules-locked-name__hint">
                  Bundled template rule names are read-only
                </span>
              </div>
            ) : (
              <TextField
                label="Name"
                ariaLabel="Rule name"
                value={draft.name}
                onChange={(value) => draftState.setField("name", value)}
              />
            )}
            <TextAreaField
              label="Description"
              ariaLabel="Description"
              value={draft.description}
              onChange={(value) => draftState.setField("description", value)}
            />
            <div className="rules-detail__grid">
              <SelectField
                label="Event"
                ariaLabel="Event"
                value={draft.event}
                options={eventOptions}
                onChange={(value) => draftState.setField("event", value)}
              />
              <TextField
                label="Group"
                ariaLabel="Group"
                value={draft.group}
                onChange={(value) => draftState.setField("group", value)}
              />
              <TextField
                label="Priority"
                ariaLabel="Priority"
                value={String(draft.priority)}
                onChange={(value) => {
                  const next = Number.parseInt(value, 10);
                  draftState.setField("priority", Number.isFinite(next) ? next : 0);
                }}
              />
              <SelectField
                label="Audience"
                ariaLabel="Audience"
                value={draft.audience}
                options={audienceOptions}
                onChange={(value) => draftState.setField("audience", value)}
              />
            </div>
            <TagsField
              label="Tags"
              ariaLabel="Tags"
              value={draft.tags}
              placeholder="Add tag"
              onChange={(value) => draftState.setField("tags", value)}
            />
            <TagsField
              label="Agent scope"
              ariaLabel="Agent scope"
              value={draft.agent_scope}
              placeholder="Add agent"
              onChange={(value) => draftState.setField("agent_scope", value)}
            />
            <SwitchField
              label="Enabled"
              value={draft.enabled}
              ariaLabel="Rule enabled"
              onChange={(value) => draftState.setField("enabled", value)}
            />
            <div className="rules-detail__readonly-grid">
              <ReadOnlySummary label="When" value={draft.when} />
              <ReadOnlySummary label="Match" value={draft.match} />
              <ReadOnlySummary label="Effects" value={draft.effects} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
