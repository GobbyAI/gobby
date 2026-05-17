import { useCallback, useEffect, useRef, useState } from "react";

const ACTIVE_AGENT_KEY = "gobby-active-agent";
const SELECTED_PROVIDER_KEY = "gobby-selected-provider";

export function useProviderAgentState() {
  const [activeAgent, setActiveAgent] = useState<string>(
    () => localStorage.getItem(ACTIVE_AGENT_KEY) || "default",
  );
  const activeAgentRef = useRef(activeAgent);

  useEffect(() => {
    activeAgentRef.current = activeAgent;
    localStorage.setItem(ACTIVE_AGENT_KEY, activeAgent);
  }, [activeAgent]);

  const [selectedProvider, setSelectedProviderRaw] = useState<string | null>(
    () => {
      try {
        return localStorage.getItem(SELECTED_PROVIDER_KEY) || null;
      } catch {
        return null;
      }
    },
  );

  const setSelectedProvider = useCallback((provider: string | null) => {
    setSelectedProviderRaw(provider);
    try {
      if (provider) {
        localStorage.setItem(SELECTED_PROVIDER_KEY, provider);
      } else {
        localStorage.removeItem(SELECTED_PROVIDER_KEY);
      }
    } catch {
      /* ignore */
    }
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
