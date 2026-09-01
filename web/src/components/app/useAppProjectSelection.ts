import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { configurationClient } from "../../api/config";

import type { ProjectWithStats } from "../../hooks/useProjects";
import type {
  SendProjectChangeAction,
  StartNewChatAction,
} from "../../hooks/useChat/actionTypes";
import type { ProjectOption } from "../../types/chat";
import type { ProjectSelectionContextValue } from "../settings/sections/SettingsSectionContext";

const HIDDEN_PROJECTS = new Set(["_orphaned", "_migrated"]);

interface UseAppProjectSelectionArgs {
  allProjects: ProjectWithStats[];
  onProjectSelect: () => void;
  selectedProvider: string | null;
  setSelectedProvider: (provider: string | null) => void;
  startNewChat: StartNewChatAction;
  setProjectIdRef: (projectId: string | null) => void;
  sendProjectChange: SendProjectChangeAction;
}

export function useAppProjectSelection({
  allProjects,
  onProjectSelect,
  selectedProvider,
  setSelectedProvider,
  startNewChat,
  setProjectIdRef,
  sendProjectChange,
}: UseAppProjectSelectionArgs) {
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    null,
  );
  const [uiSettingsLoaded, setUiSettingsLoaded] = useState(false);
  const initialReconciliationDoneRef = useRef(false);
  const projectTouchedRef = useRef(false);
  const providerTouchedRef = useRef(false);

  const selectProject = useCallback(
    (projectId: string | null) => {
      onProjectSelect();
      projectTouchedRef.current = true;
      setSelectedProjectId(projectId);
    },
    [onProjectSelect],
  );

  const selectProvider = useCallback(
    (provider: string | null) => {
      providerTouchedRef.current = true;
      setSelectedProvider(provider);
    },
    [setSelectedProvider],
  );

  const projectOptions: ProjectOption[] = useMemo(
    () =>
      allProjects
        .filter((p) => !HIDDEN_PROJECTS.has(p.name))
        .map((p) => ({
          id: p.id,
          name: p.name === "_personal" ? "Personal" : p.display_name || p.name,
          // Personal is checkout-free by design; every other project needs a
          // checkout on this machine before chat or files can run against it.
          hasCheckout: p.name === "_personal" || p.checkout !== null,
        })),
    [allProjects],
  );

  const defaultProjectId = useMemo(() => {
    const repoProject = projectOptions.find((p) => p.name !== "Personal");
    return (
      repoProject?.id ??
      projectOptions.find((p) => p.name === "Personal")?.id ??
      projectOptions[0]?.id ??
      null
    );
  }, [projectOptions]);

  const projectReady = uiSettingsLoaded && projectOptions.length > 0;
  const resolvedSelectedProjectId =
    selectedProjectId &&
    projectOptions.some((project) => project.id === selectedProjectId)
      ? selectedProjectId
      : null;
  const effectiveProjectId = resolvedSelectedProjectId ?? defaultProjectId;
  const isPersonalProject =
    projectOptions.find((p) => p.id === effectiveProjectId)?.name ===
    "Personal";

  const projectSelection: ProjectSelectionContextValue = useMemo(
    () => ({
      selectedProjectId: effectiveProjectId,
      onSelectProject: selectProject,
    }),
    [effectiveProjectId, selectProject],
  );

  // On mount: fetch persisted project/provider from API (DB is source of truth).
  useEffect(() => {
    let cancelled = false;
    configurationClient
      .fetchValues()
      .then((snapshot) => {
        if (cancelled) return;
        const data = snapshot?.desired.ui_settings;
        const settings =
          data && typeof data === "object" && !Array.isArray(data)
            ? (data as Record<string, unknown>)
            : null;
        if (
          !projectTouchedRef.current &&
          typeof settings?.selectedProjectId === "string"
        ) {
          setSelectedProjectId(settings.selectedProjectId);
        }
        if (
          !providerTouchedRef.current &&
          typeof settings?.selectedProvider === "string"
        ) {
          setSelectedProvider(settings.selectedProvider);
        }
        setUiSettingsLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setUiSettingsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [setSelectedProvider]);

  const isFirstProviderRender = useRef(true);
  useEffect(() => {
    if (!uiSettingsLoaded) return;
    if (isFirstProviderRender.current) {
      isFirstProviderRender.current = false;
      if (!providerTouchedRef.current) return;
    }
    configurationClient
      .patchLastWriteWins({ ui_settings: { selectedProvider } })
      .catch((error) => {
        console.warn("Failed to persist selected provider", error);
      });
  }, [selectedProvider, uiSettingsLoaded]);

  const isFirstProjectRender = useRef(true);
  useEffect(() => {
    if (!projectReady) return;
    if (isFirstProjectRender.current) {
      isFirstProjectRender.current = false;
      if (!projectTouchedRef.current) return;
    }
    void configurationClient.patchLastWriteWins({
      ui_settings: { selectedProjectId },
    });
  }, [selectedProjectId, projectReady]);

  const prevProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (!projectReady) return;
    if (
      effectiveProjectId &&
      prevProjectRef.current !== null &&
      effectiveProjectId !== prevProjectRef.current
    ) {
      startNewChat();
      initialReconciliationDoneRef.current = false;
    }
    prevProjectRef.current = effectiveProjectId ?? null;
  }, [effectiveProjectId, startNewChat, projectReady]);

  useEffect(() => {
    if (!projectReady) return;
    setProjectIdRef(effectiveProjectId);
    if (effectiveProjectId) {
      sendProjectChange(effectiveProjectId);
    }
  }, [effectiveProjectId, setProjectIdRef, sendProjectChange, projectReady]);

  return {
    effectiveProjectId,
    initialReconciliationDoneRef,
    isPersonalProject,
    projectOptions,
    projectReady,
    projectSelection,
    selectProject,
    selectProvider,
  };
}
