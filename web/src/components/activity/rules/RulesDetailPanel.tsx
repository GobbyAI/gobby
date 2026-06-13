import { useCallback, useEffect, useMemo } from "react";

import type { RuleDetail } from "../../../hooks/useRules";
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
  formatRuleSummaryValue,
  isBundledRule,
  type RuleDraft,
} from "./RulesTabData";

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

export function RulesDetailPanel({
  detail,
  isLoading,
  error,
  onSave,
  onError,
  onConfirmLeaveChange,
}: RulesDetailPanelProps) {
  const sourceDraft = useMemo(() => (detail ? detailToDraft(detail) : null), [detail]);

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
  const bundled = isBundledRule(detail);
  const eventOptions = optionsWithCurrent(
    [{ value: "", label: "Select event" }, ...RULE_EVENT_OPTIONS],
    draft.event,
  );
  const audienceOptions = optionsWithCurrent([...RULE_AUDIENCE_OPTIONS], draft.audience);

  return (
    <div className="rules-detail">
      <DetailPaneHeader
        title={draft.name || detail.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={draftState.discard}
        actions={<span className="rules-detail__source">{detail.source}</span>}
      />
      <div className="rules-detail__body">
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
      </div>
    </div>
  );
}
