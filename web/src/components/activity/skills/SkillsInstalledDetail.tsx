import { useCallback, useEffect, useState } from "react";

import { ActivityPanelEmpty, TasksEmptyIcon } from "../ActivityPanelEmpty";
import {
  DetailActionButton,
  DetailPaneHeader,
  ProjectSelectField,
  SelectField,
  SwitchField,
  TagsField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import { SkillContentView } from "./SkillContentView";
import {
  skillCategory,
  skillSourceKey,
  skillSourceLabel,
  type ActivitySkill,
} from "./SkillsTabData";

type DetailViewMode = "detail" | "content";

interface SkillsInstalledDetailProps {
  skill: ActivitySkill | null;
  onSave: (draft: ActivitySkill) => Promise<boolean>;
  onError: (message: string | null) => void;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  confirmDiscardChanges: () => Promise<boolean>;
}

const INJECTION_FORMAT_OPTIONS = [
  { value: "summary", label: "Summary" },
  { value: "full", label: "Full" },
  { value: "content", label: "Content" },
];

export function SkillsInstalledDetail({
  skill,
  onSave,
  onError,
  onConfirmLeaveChange,
  confirmDiscardChanges,
}: SkillsInstalledDetailProps) {
  const [viewMode, setViewMode] = useState<DetailViewMode>("detail");
  const handleSave = useCallback(
    async (draft: ActivitySkill) => {
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
  const draftState = useDetailDraft<ActivitySkill>({
    source: skill,
    onSave: handleSave,
  });

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  if (!skill || !draftState.draft) {
    return (
      <ActivityPanelEmpty
        icon={<TasksEmptyIcon />}
        heading="Installed skills"
        body="Select a skill to inspect its configuration and edit its content."
      />
    );
  }

  const draft = draftState.draft;
  const disabled = Boolean(draft.deleted_at);
  const setField = <K extends keyof ActivitySkill>(key: K, value: ActivitySkill[K]) => {
    draftState.setField(key, value);
  };
  const showProjectField = Boolean(draft.project_id || draft.source === "project");

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailPaneHeader
        title={draft.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={draftState.discard}
        actions={
          <>
            {skillSourceKey(draft) !== "installed" && (
              <span className="activity-chip">{skillSourceLabel(draft)}</span>
            )}
            {viewMode === "content" ? (
              <DetailActionButton label="Close" onClick={() => setViewMode("detail")} />
            ) : (
              <DetailActionButton
                label="Content"
                variant="accent"
                onClick={() => setViewMode("content")}
              />
            )}
          </>
        }
      />
      {viewMode === "content" ? (
        <SkillContentView
          skill={draft}
          disabled={disabled}
          onError={onError}
          onSaveContent={(next) => draftState.save({ ...draft, content: next })}
          confirmDiscardChanges={confirmDiscardChanges}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div className="grid gap-3 md:grid-cols-2">
            <TextField
              label="Name"
              ariaLabel="Skill name"
              value={draft.name}
              disabled
              onChange={(value) => setField("name", value)}
            />
            <TextField
              label="Category"
              ariaLabel="Skill category value"
              value={skillCategory(draft)}
              disabled
              onChange={() => undefined}
            />
            <TextField
              label="Version"
              ariaLabel="Skill version"
              value={draft.version ?? ""}
              disabled={disabled}
              onChange={(value) => setField("version", value)}
            />
            <TextField
              label="License"
              ariaLabel="Skill license"
              value={draft.license ?? ""}
              disabled={disabled}
              onChange={(value) => setField("license", value)}
            />
            <TextField
              label="Compatibility"
              ariaLabel="Skill compatibility"
              value={draft.compatibility ?? ""}
              disabled={disabled}
              onChange={(value) => setField("compatibility", value)}
            />
            <SelectField
              label="Injection format"
              ariaLabel="Skill injection format"
              value={draft.injection_format || "summary"}
              disabled={disabled}
              options={INJECTION_FORMAT_OPTIONS}
              onChange={(value) => setField("injection_format", value)}
            />
          </div>
          <div className="mt-4 grid gap-3">
            <TextAreaField
              label="Description"
              ariaLabel="Skill description"
              value={draft.description ?? ""}
              disabled={disabled}
              rows={4}
              onChange={(value) => setField("description", value)}
            />
            <TagsField
              label="Allowed tools"
              ariaLabel="Skill allowed tools"
              value={draft.allowed_tools ?? []}
              disabled={disabled}
              placeholder="Add tool"
              onChange={(value) => setField("allowed_tools", value)}
            />
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <SwitchField
              label="Enabled"
              ariaLabel="Skill enabled"
              value={draft.enabled}
              disabled={disabled}
              onChange={(value) => setField("enabled", value)}
            />
            <SwitchField
              label="Always apply"
              ariaLabel="Skill always apply"
              value={draft.always_apply}
              disabled={disabled}
              onChange={(value) => setField("always_apply", value)}
            />
            {showProjectField && (
              <ProjectSelectField
                label="Project"
                ariaLabel="Skill project"
                value={draft.project_id ?? ""}
                disabled={disabled}
                placeholder="Project scope"
                onChange={(value) => setField("project_id", value || null)}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
