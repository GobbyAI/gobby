import { useCallback, useEffect } from "react";

import { ActivityPanelEmpty, PipelinesEmptyIcon } from "../ActivityPanelEmpty";
import { Chip } from "../../ui/Chip";
import {
  DetailActionButton,
  DetailPaneHeader,
  SwitchField,
  TagsField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import { PipelineEditor } from "./PipelineEditor";
import type {
  PipelineDefinition,
  PipelineDefinitionUpdate,
} from "./PipelinesDefsActions";

export type PipelineDefinitionViewMode = "detail" | "editor";

interface PipelinesDefsDetailProps {
  definition: PipelineDefinition | null;
  viewMode: PipelineDefinitionViewMode;
  onViewModeChange: (mode: PipelineDefinitionViewMode) => void;
  onSave: (definition: PipelineDefinition) => Promise<boolean>;
  onUpdateDefinition: (
    id: string,
    updates: PipelineDefinitionUpdate,
  ) => Promise<PipelineDefinition | null>;
  onExport: (definition: PipelineDefinition) => void;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
  onError: (message: string | null) => void;
}

export function PipelinesDefsDetail({
  definition,
  viewMode,
  onViewModeChange,
  onSave,
  onUpdateDefinition,
  onExport,
  onConfirmLeaveChange,
  onError,
}: PipelinesDefsDetailProps) {
  const handleSave = useCallback(
    async (draft: PipelineDefinition) => {
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
  const draftState = useDetailDraft<PipelineDefinition>({
    source: definition,
    onSave: handleSave,
  });

  useEffect(() => {
    if (viewMode === "detail") {
      onConfirmLeaveChange(draftState.confirmIfDirty);
      return () => onConfirmLeaveChange((next) => next());
    }
    onConfirmLeaveChange((next) => next());
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange, viewMode]);

  if (!definition || !draftState.draft) {
    return (
      <ActivityPanelEmpty
        icon={<PipelinesEmptyIcon />}
        heading="Pipeline definitions"
        body="Select a pipeline definition to inspect and edit it."
      />
    );
  }

  if (viewMode === "editor") {
    return (
      <PipelineEditor
        pipeline={definition}
        updateWorkflow={onUpdateDefinition}
        onBack={() => onViewModeChange("detail")}
        onExport={() => onExport(definition)}
      />
    );
  }

  const draft = draftState.draft;
  const setField = <K extends keyof PipelineDefinition>(
    key: K,
    value: PipelineDefinition[K],
  ) => {
    draftState.setField(key, value);
  };

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
            <Chip tone="accent" uppercase>
              PIPELINE
            </Chip>
            <DetailActionButton
              label="Edit"
              variant="accent"
              onClick={() => onViewModeChange("editor")}
            />
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="grid gap-3 md:grid-cols-2">
          <TextField
            label="Name"
            ariaLabel="Pipeline definition name"
            value={draft.name}
            disabled
            onChange={(value) => setField("name", value)}
          />
          <TextField
            label="Source"
            ariaLabel="Pipeline definition source"
            value={draft.source}
            disabled
            onChange={(value) => setField("source", value)}
          />
          <TextField
            label="Version"
            ariaLabel="Pipeline definition version"
            value={draft.version}
            disabled
            onChange={(value) => setField("version", value)}
          />
          <SwitchField
            label="Enabled"
            ariaLabel="Pipeline definition enabled"
            value={draft.enabled}
            onChange={(value) => setField("enabled", value)}
          />
        </div>
        <div className="mt-4 grid gap-3">
          <TextAreaField
            label="Description"
            ariaLabel="Pipeline definition description"
            value={draft.description ?? ""}
            onChange={(value) => setField("description", value)}
          />
          <TagsField
            label="Tags"
            ariaLabel="Pipeline definition tags"
            value={draft.tags ?? []}
            placeholder="Add tag"
            onChange={(value) => setField("tags", value)}
          />
        </div>
      </div>
    </div>
  );
}
