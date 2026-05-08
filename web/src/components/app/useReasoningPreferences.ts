import { useCallback, useEffect, useMemo, useState } from "react";
import {
  buildReasoningPreferenceKey,
  fetchProviderModelCatalog,
  getPreferredModelForProvider,
  getPreferredReasoningEffort,
  resolveModelValueForProvider,
  type ProviderModelEntry,
} from "../../lib/providerModels";
import {
  loadReasoningPreferences,
  REASONING_PREFERENCES_STORAGE_KEY,
} from "../../lib/sessionPersistence";

interface UseReasoningPreferencesArgs {
  mainSessionSource: string | null | undefined;
  selectedProvider: string | null | undefined;
  currentModel: string | null | undefined;
  persistedSessionModel: string | null | undefined;
  updateModel: (model: string) => void;
}

export function useReasoningPreferences({
  mainSessionSource,
  selectedProvider,
  currentModel,
  persistedSessionModel,
  updateModel,
}: UseReasoningPreferencesArgs) {
  const [providerModelCatalog, setProviderModelCatalog] = useState<
    ProviderModelEntry[]
  >([]);
  const [reasoningPreferences, setReasoningPreferences] = useState<
    Record<string, string>
  >(() => loadReasoningPreferences());

  useEffect(() => {
    let cancelled = false;
    fetchProviderModelCatalog()
      .then((catalog) => {
        if (!cancelled) setProviderModelCatalog(catalog);
      })
      .catch(() => {
        if (!cancelled) setProviderModelCatalog([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(
        REASONING_PREFERENCES_STORAGE_KEY,
        JSON.stringify(reasoningPreferences),
      );
    } catch {
      // Best-effort local preference cache
    }
  }, [reasoningPreferences]);

  useEffect(() => {
    const activeProvider = mainSessionSource ?? selectedProvider ?? "claude";
    const selectedModelForProvider = resolveModelValueForProvider(
      providerModelCatalog,
      activeProvider,
      currentModel,
    );
    const persistedModelForProvider = resolveModelValueForProvider(
      providerModelCatalog,
      activeProvider,
      persistedSessionModel ?? null,
    );
    const nextModel =
      selectedModelForProvider ??
      persistedModelForProvider ??
      getPreferredModelForProvider(providerModelCatalog, activeProvider, null);
    if (nextModel && nextModel !== currentModel) {
      updateModel(nextModel);
    }
  }, [
    mainSessionSource,
    persistedSessionModel,
    providerModelCatalog,
    selectedProvider,
    currentModel,
    updateModel,
  ]);

  const updateReasoningPreference = useCallback(
    (
      provider: string | null | undefined,
      model: string | null | undefined,
      reasoningEffort: string | null | undefined,
    ) => {
      const key = buildReasoningPreferenceKey(provider, model);
      if (!key || !reasoningEffort) return;
      setReasoningPreferences((prev) => {
        if (prev[key] === reasoningEffort) return prev;
        return { ...prev, [key]: reasoningEffort };
      });
    },
    [],
  );

  const currentMainReasoning = useMemo(() => {
    const provider = mainSessionSource ?? selectedProvider ?? "claude";
    const preferenceKey = buildReasoningPreferenceKey(provider, currentModel);
    return getPreferredReasoningEffort(
      providerModelCatalog,
      provider,
      currentModel,
      preferenceKey ? reasoningPreferences[preferenceKey] : null,
    );
  }, [
    mainSessionSource,
    providerModelCatalog,
    reasoningPreferences,
    selectedProvider,
    currentModel,
  ]);

  return {
    providerModelCatalog,
    reasoningPreferences,
    updateReasoningPreference,
    currentMainReasoning,
  };
}
