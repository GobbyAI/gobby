import { useEffect, useRef, useState } from "react";

const ACTIVE_AGENT_KEY = "gobby-active-agent";
const SELECTED_PROVIDER_KEY = "gobby-selected-provider";
const DEFAULT_AGENT = "default";

function isExpectedStorageError(error: unknown): boolean {
  return error instanceof DOMException || error instanceof TypeError;
}

function logUnexpectedStorageError(action: string, key: string, error: unknown) {
  if (!isExpectedStorageError(error) && import.meta.env.DEV) {
    console.warn("Unexpected localStorage error", { action, key, error });
  }
}

function storageGet(key: string): string | null {
  try {
    if (!globalThis.localStorage) return null;
    return globalThis.localStorage.getItem(key);
  } catch (error) {
    logUnexpectedStorageError("get", key, error);
    return null;
  }
}

function storageSet(key: string, value: string): "stored" | "unavailable" | "failed" {
  try {
    if (!globalThis.localStorage) return "unavailable";
    globalThis.localStorage.setItem(key, value);
    return "stored";
  } catch (error) {
    logUnexpectedStorageError("set", key, error);
    return "failed";
  }
}

function storageRemove(key: string): "removed" | "unavailable" | "failed" {
  try {
    if (!globalThis.localStorage) return "unavailable";
    globalThis.localStorage.removeItem(key);
    return "removed";
  } catch (error) {
    logUnexpectedStorageError("remove", key, error);
    return "failed";
  }
}

export function useProviderAgentState() {
  const [activeAgent, setActiveAgent] = useState<string>(
    () => storageGet(ACTIVE_AGENT_KEY) || DEFAULT_AGENT,
  );
  const activeAgentRef = useRef(activeAgent);

  useEffect(() => {
    activeAgentRef.current = activeAgent;
    const status = activeAgent === DEFAULT_AGENT
      ? storageRemove(ACTIVE_AGENT_KEY)
      : storageSet(ACTIVE_AGENT_KEY, activeAgent);
    if (status === "failed") {
      console.warn("Failed to persist active agent selection");
    }
  }, [activeAgent]);

  const [selectedProvider, setSelectedProviderRaw] = useState<string | null>(
    () => storageGet(SELECTED_PROVIDER_KEY) || null,
  );
  const selectedProviderRef = useRef<string | null>(selectedProvider);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (event: StorageEvent) => {
      if (
        event.key !== null &&
        event.key !== ACTIVE_AGENT_KEY &&
        event.key !== SELECTED_PROVIDER_KEY
      ) {
        return;
      }
      try {
        if (event.storageArea && event.storageArea !== window.localStorage) return;
      } catch {
        return;
      }
      if (event.key === null || event.key === ACTIVE_AGENT_KEY) {
        setActiveAgent(
          event.key === null
            ? storageGet(ACTIVE_AGENT_KEY) || DEFAULT_AGENT
            : event.newValue || DEFAULT_AGENT,
        );
      }
      if (event.key === null || event.key === SELECTED_PROVIDER_KEY) {
        setSelectedProviderRaw(
          event.key === null
            ? storageGet(SELECTED_PROVIDER_KEY) || null
            : event.newValue || null,
        );
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    selectedProviderRef.current = selectedProvider;
    const status = selectedProvider
      ? storageSet(SELECTED_PROVIDER_KEY, selectedProvider)
      : storageRemove(SELECTED_PROVIDER_KEY);
    if (status === "failed") {
      console.warn("Failed to persist selected provider");
    }
  }, [selectedProvider]);

  return {
    activeAgent,
    activeAgentRef,
    selectedProvider,
    selectedProviderRef,
    setActiveAgent,
    setSelectedProvider: setSelectedProviderRaw,
  };
}
