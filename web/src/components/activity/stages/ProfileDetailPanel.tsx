import { useCallback, useEffect, useMemo, useState } from "react";

import { Chip } from "../../ui/Chip";
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
import type { BuildProfile, ProfileSource } from "./StagesTabData";
import {
  DELIVERY_MODE_OPTIONS,
  ISOLATION_OPTIONS,
  PROFILE_SOURCE_OPTIONS,
  createProfileDraft,
} from "./StagesTabData";

interface ProfileDetailPanelProps {
  profile: BuildProfile | null;
  creating: boolean;
  projectId?: string | null;
  stageNames: Set<string>;
  onSave: (profile: BuildProfile, creating: boolean) => Promise<boolean>;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  onError: (message: string | null) => void;
}

export function ProfileDetailPanel({
  profile,
  creating,
  projectId,
  stageNames,
  onSave,
  onConfirmLeaveChange,
  onError,
}: ProfileDetailPanelProps) {
  const [validationError, setValidationError] = useState<string | null>(null);
  const sourceDraft = useMemo(
    () => (creating ? createProfileDraft(projectId) : profile),
    [creating, profile, projectId],
  );
  const handleSave = useCallback(
    async (draft: BuildProfile) => {
      const unknownSkipStages = draft.skip_stages.filter(
        (stage) => !stageNames.has(stage),
      );
      if (unknownSkipStages.length > 0) {
        setValidationError(
          `Unknown skip stage: ${unknownSkipStages.join(", ")}`,
        );
        return false;
      }
      try {
        setValidationError(null);
        onError(null);
        return await onSave(draft, creating);
      } catch (error) {
        onError(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    [creating, onError, onSave, stageNames],
  );
  const draftState = useDetailDraft<BuildProfile>({
    source: sourceDraft,
    onSave: handleSave,
  });

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  if (!draftState.draft) {
    return (
      <ActivityPanelEmpty
        heading="Profiles"
        body="Select a profile to inspect and edit it."
      />
    );
  }

  const draft = draftState.draft;
  const unknownSkipStages = draft.skip_stages.filter(
    (stage) => !stageNames.has(stage),
  );
  const setField = <K extends keyof BuildProfile>(
    key: K,
    value: BuildProfile[K],
  ) => {
    setValidationError(null);
    draftState.setField(key, value);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailPaneHeader
        title={creating ? "New profile" : draft.display_label || draft.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={draftState.discard}
        actions={!creating ? <Chip>{draft.source}</Chip> : null}
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {(validationError || unknownSkipStages.length > 0) && (
          <div className="mb-3 rounded-md bg-error-soft px-3 py-2 text-sm text-error">
            {validationError ??
              `Unknown skip stage: ${unknownSkipStages.join(", ")}`}
          </div>
        )}
        <div className="grid gap-3 md:grid-cols-2">
          <TextField
            label="Name"
            ariaLabel="Profile name"
            value={draft.name}
            disabled={!creating}
            onChange={(value) => setField("name", value)}
          />
          <TextField
            label="Label"
            ariaLabel="Profile label"
            value={draft.display_label}
            onChange={(value) => setField("display_label", value)}
          />
          <SelectField
            label="Source"
            ariaLabel="Profile source"
            value={draft.source}
            disabled={!creating}
            options={[...PROFILE_SOURCE_OPTIONS]}
            onChange={(value) => setField("source", value as ProfileSource)}
          />
          <SelectField
            label="Isolation"
            ariaLabel="Isolation"
            value={draft.isolation}
            options={[...ISOLATION_OPTIONS]}
            onChange={(value) =>
              setField("isolation", value as BuildProfile["isolation"])
            }
          />
          <SelectField
            label="Delivery mode"
            ariaLabel="Delivery mode"
            value={draft.delivery_mode}
            options={[...DELIVERY_MODE_OPTIONS]}
            onChange={(value) =>
              setField("delivery_mode", value as BuildProfile["delivery_mode"])
            }
          />
          <TextField
            label="Delivery target repo"
            ariaLabel="Delivery target repo"
            value={draft.delivery_target_repo ?? ""}
            onChange={(value) =>
              setField("delivery_target_repo", value || null)
            }
          />
          <SwitchField
            label="Enabled"
            ariaLabel="Profile enabled"
            value={draft.enabled}
            onChange={(value) => setField("enabled", value)}
          />
          <SwitchField
            label="Unattended"
            ariaLabel="Unattended"
            value={draft.unattended}
            onChange={(value) => setField("unattended", value)}
          />
        </div>
        <div className="mt-4 grid gap-3">
          <TextAreaField
            label="Description"
            ariaLabel="Profile description"
            value={draft.description}
            onChange={(value) => setField("description", value)}
          />
          <TagsField
            label="Skip stages"
            ariaLabel="Skip stages"
            value={draft.skip_stages}
            placeholder="Add stage"
            onChange={(value) => setField("skip_stages", value)}
          />
          <TagsField
            label="Tags"
            ariaLabel="Tags"
            value={draft.tags ?? []}
            placeholder="Add tag"
            onChange={(value) => setField("tags", value)}
          />
        </div>
      </div>
    </div>
  );
}
