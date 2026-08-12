import { useCallback } from "react";

import {
  getModelLabel,
  getModelsForSelection,
  getPreferredModelForProvider,
  getPreferredReasoningEffort,
  getReasoningOptionsForModel,
  type ProviderModelEntry,
  type ProviderModelOption,
  type ReasoningOption,
} from "../../lib/providerModels";

interface UseChatInputProviderSelectionOptions {
  currentModel: string;
  currentReasoning: string;
  disabled: boolean;
  onModelChange?: (model: string) => void;
  onProviderChange?: (provider: string | null) => void;
  onProviderSelectionChange?: (
    provider: string,
    model: string,
    reasoningEffort: string | null,
  ) => void;
  onReasoningChange?: (effort: string) => void;
  onSwitchProvider?: (
    provider: string,
    options?: { model?: string | null; reasoningEffort?: string | null },
  ) => void;
  provider: string | null | undefined;
  providerModelCatalog: ProviderModelEntry[];
  providerPickerDisabledReason: string | null;
}

interface ChatInputProviderSelection {
  canSelectModel: boolean;
  effectiveProvider: string;
  handleCatalogSelect: (nextProvider: string, nextModel: string) => void;
  handleModelSelect: (nextModel: string) => void;
  handleProviderSelect: (nextProvider: string) => void;
  handleReasoningSelect: (nextReasoning: string) => void;
  modelOptions: ProviderModelOption[];
  reasoningOptions: ReasoningOption[];
  resolvedModelLabel: string;
  resolvedModelValue: string;
  resolvedReasoning: string;
  selectionDisabled: boolean;
}

export function useChatInputProviderSelection({
  currentModel,
  currentReasoning,
  disabled,
  onModelChange,
  onProviderChange,
  onProviderSelectionChange,
  onReasoningChange,
  onSwitchProvider,
  provider,
  providerModelCatalog,
  providerPickerDisabledReason,
}: UseChatInputProviderSelectionOptions): ChatInputProviderSelection {
  const effectiveProvider = provider ?? "claude";
  const visibleModels = getModelsForSelection(
    providerModelCatalog,
    effectiveProvider,
    currentModel,
  );
  const modelOptions =
    visibleModels.length > 0
      ? visibleModels
      : [
          {
            value: currentModel || "default",
            label: getModelLabel(
              providerModelCatalog,
              effectiveProvider,
              currentModel,
            ),
          },
        ];
  const resolvedModelValue =
    currentModel || modelOptions[0]?.value || "default";
  const resolvedModelLabel = getModelLabel(
    providerModelCatalog,
    effectiveProvider,
    resolvedModelValue,
  );
  const reasoningOptions = getReasoningOptionsForModel(
    providerModelCatalog,
    effectiveProvider,
    resolvedModelValue,
  );
  const resolvedReasoning =
    currentReasoning ||
    getPreferredReasoningEffort(
      providerModelCatalog,
      effectiveProvider,
      resolvedModelValue,
      currentReasoning,
    );
  const canSelectModel = Boolean(onModelChange);
  const selectionDisabled = disabled || Boolean(providerPickerDisabledReason);

  const applySelection = useCallback(
    (nextProvider: string, nextModel: string, nextReasoning: string) => {
      if (onProviderSelectionChange) {
        onProviderSelectionChange(nextProvider, nextModel, nextReasoning);
        return;
      }

      const providerChanged = nextProvider !== effectiveProvider;
      if (providerChanged) {
        onProviderChange?.(nextProvider);
      }
      onModelChange?.(nextModel);
      onReasoningChange?.(nextReasoning);
      if (providerChanged) {
        onSwitchProvider?.(nextProvider, {
          model: nextModel,
          reasoningEffort: nextReasoning,
        });
      }
    },
    [
      effectiveProvider,
      onModelChange,
      onProviderChange,
      onProviderSelectionChange,
      onReasoningChange,
      onSwitchProvider,
    ],
  );

  const handleProviderSelect = useCallback(
    (nextProvider: string) => {
      const nextModel =
        getPreferredModelForProvider(
          providerModelCatalog,
          nextProvider,
          null,
        ) ??
        resolvedModelValue ??
        "default";
      const nextReasoning = getPreferredReasoningEffort(
        providerModelCatalog,
        nextProvider,
        nextModel,
        null,
      );
      applySelection(nextProvider, nextModel, nextReasoning);
    },
    [applySelection, providerModelCatalog, resolvedModelValue],
  );

  const handleCatalogSelect = useCallback(
    (nextProvider: string, nextModel: string) => {
      const nextReasoning = getPreferredReasoningEffort(
        providerModelCatalog,
        nextProvider,
        nextModel,
        null,
      );
      applySelection(nextProvider, nextModel, nextReasoning);
    },
    [applySelection, providerModelCatalog],
  );

  const handleModelSelect = useCallback(
    (nextModel: string) => {
      const nextReasoning = getPreferredReasoningEffort(
        providerModelCatalog,
        effectiveProvider,
        nextModel,
        resolvedReasoning,
      );
      applySelection(effectiveProvider, nextModel, nextReasoning);
    },
    [
      applySelection,
      effectiveProvider,
      providerModelCatalog,
      resolvedReasoning,
    ],
  );

  const handleReasoningSelect = useCallback(
    (nextReasoning: string) => {
      applySelection(effectiveProvider, resolvedModelValue, nextReasoning);
    },
    [applySelection, effectiveProvider, resolvedModelValue],
  );

  return {
    canSelectModel,
    effectiveProvider,
    handleCatalogSelect,
    handleModelSelect,
    handleProviderSelect,
    handleReasoningSelect,
    modelOptions,
    reasoningOptions,
    resolvedModelLabel,
    resolvedModelValue,
    resolvedReasoning,
    selectionDisabled,
  };
}
