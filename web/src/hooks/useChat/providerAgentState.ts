import { useCallback, useEffect, useRef, useState } from "react";

const ACTIVE_AGENT_KEY = "gobby-active-agent";
const SELECTED_PROVIDER_KEY = "gobby-selected-provider";

function storageGet(key: string): string | null {
  try {
    if (typeof globalThis.localStorage === "undefined") return null;
    return globalThis.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string): "stored" | "unavailable" | "failed" {
  try {
    if (typeof globalThis.localStorage === "undefined") return "unavailable";
    globalThis.localStorage.setItem(key, value);
    return "stored";
  } catch {
    return "failed";
  }
}

function storageRemove(key: string): void {
  try {
    if (typeof globalThis.localStorage !== "undefined") {
      globalThis.localStorage.removeItem(key);
    }
  } catch {
    /* ignore */
  }
}

export function useProviderAgentState() {
  const [activeAgent, setActiveAgent] = useState<string>(
    () => storageGet(ACTIVE_AGENT_KEY) || "default",
  );
  const activeAgentRef = useRef(activeAgent);

  useEffect(() => {
    activeAgentRef.current = activeAgent;
    if (storageSet(ACTIVE_AGENT_KEY, activeAgent) === "failed") {
      console.warn("Failed to persist active agent selection");
    }
  }, [activeAgent]);

  const [selectedProvider, setSelectedProviderRaw] = useState<string | null>(
    () => storageGet(SELECTED_PROVIDER_KEY) || null,
  );

  const setSelectedProvider = useCallback((provider: string | null) => {
    setSelectedProviderRaw(provider);
    if (provider) storageSet(SELECTED_PROVIDER_KEY, provider);
    else storageRemove(SELECTED_PROVIDER_KEY);
  }, []);

  const selectedProviderRef = useRef<string | null>(selectedProvider);
  useEffect(() => {
    selectedProviderRef.current = selectedProvider;
  }, [selectedProvider]);

  return {
    activeAgent,
    activeAgentRef,
    selectedProvider,
    selectedProviderRef,
    setActiveAgent,
    setSelectedProvider,
  };
}
