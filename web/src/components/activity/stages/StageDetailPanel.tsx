import { useCallback, useEffect } from "react";

import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import {
  DetailPaneHeader,
  SelectField,
  SwitchField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import type { StageEntry } from "./StagesTabData";
import { STAGE_CATEGORY_OPTIONS } from "./StagesTabData";

interface StageDetailPanelProps {
  stage: StageEntry | null;
  onSave: (stage: StageEntry) => Promise<boolean>;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  onError: (message: string | null) => void;
}

function numericValue(value: string, fallback: number): number {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

export function StageDetailPanel({
  stage,
  onSave,
  onConfirmLeaveChange,
  onError,
}: StageDetailPanelProps) {
  const handleSave = useCallback(
    async (draft: StageEntry) => {
      try {
        onError(null);
        return await onSave(draft);
      } catch (error) {
        onError(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    [onError, onSave],
  );
  const draftState = useDetailDraft<StageEntry>({ source: stage, onSave: handleSave });

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  if (!draftState.draft) {
    return (
      <ActivityPanelEmpty heading="Stages" body="Select a stage to inspect and edit it." />
    );
  }

  const draft = draftState.draft;
  const setField = <K extends keyof StageEntry>(key: K, value: StageEntry[K]) => {
    draftState.setField(key, value);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailPaneHeader
        title={draft.display_label || draft.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={draftState.discard}
        actions={
          <span className="activity-chip">{draft.name}</span>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="grid gap-3 md:grid-cols-2">
          <TextField
            label="Label"
            ariaLabel="Stage label"
            value={draft.display_label}
            onChange={(value) => setField("display_label", value)}
          />
          <SelectField
            label="Category"
            ariaLabel="Stage category"
            value={draft.category}
            options={STAGE_CATEGORY_OPTIONS}
            onChange={(value) => setField("category", value)}
          />
          <TextField
            label="Default agent"
            ariaLabel="Default agent"
            value={draft.default_agent ?? ""}
            onChange={(value) => setField("default_agent", value || null)}
          />
          <TextField
            label="Reviewer agent"
            ariaLabel="Reviewer agent"
            value={draft.reviewer_agent ?? ""}
            onChange={(value) => setField("reviewer_agent", value || null)}
          />
          <TextField
            label="Review policy"
            ariaLabel="Review policy"
            value={draft.review_policy}
            onChange={(value) => setField("review_policy", value)}
          />
          <TextField
            label="Dispatch type"
            ariaLabel="Dispatch type"
            value={draft.dispatch_type ?? ""}
            onChange={(value) => setField("dispatch_type", value || null)}
          />
          <TextField
            label="Dispatch target"
            ariaLabel="Dispatch target"
            value={draft.dispatch_target ?? ""}
            onChange={(value) => setField("dispatch_target", value || null)}
          />
          <TextField
            label="Position"
            ariaLabel="Position"
            value={String(draft.position_hint)}
            onChange={(value) => setField("position_hint", numericValue(value, 0))}
          />
          <TextField
            label="Max work attempts"
            ariaLabel="Max work attempts"
            value={String(draft.default_max_work_attempts)}
            onChange={(value) =>
              setField("default_max_work_attempts", numericValue(value, 0))
            }
          />
          <TextField
            label="Max review rounds"
            ariaLabel="Max review rounds"
            value={String(draft.default_max_review_rounds)}
            onChange={(value) =>
              setField("default_max_review_rounds", numericValue(value, 0))
            }
          />
          <SwitchField
            label="Requires human"
            ariaLabel="Requires human"
            value={draft.requires_human}
            onChange={(value) => setField("requires_human", value)}
          />
          <SwitchField
            label="Terminal stage"
            ariaLabel="Terminal stage"
            value={draft.is_terminal}
            onChange={(value) => setField("is_terminal", value)}
          />
        </div>
        <div className="mt-4 grid gap-3">
          <TextAreaField
            label="Description"
            ariaLabel="Stage description"
            value={draft.description}
            onChange={(value) => setField("description", value)}
          />
          <TextAreaField
            label="Reviewer selector JSON"
            ariaLabel="Reviewer selector JSON"
            value={draft.reviewer_agent_selector_json ?? ""}
            onChange={(value) => setField("reviewer_agent_selector_json", value || null)}
          />
          <TextAreaField
            label="Dispatch inputs JSON"
            ariaLabel="Dispatch inputs JSON"
            value={draft.dispatch_inputs_json ?? ""}
            onChange={(value) => setField("dispatch_inputs_json", value || null)}
          />
        </div>
      </div>
    </div>
  );
}
