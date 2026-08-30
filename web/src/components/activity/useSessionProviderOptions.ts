import { useEffect, useState } from "react";

import { isHiddenProvider } from "../../lib/providerModels";

const NON_PROVIDER_SOURCES = new Set(["cron", "pipeline", "system"]);

type ProviderRegistryResponse = {
  providers?: unknown;
};

export function providerNamesFromRegistry(data: unknown): string[] {
  const providers = (data as ProviderRegistryResponse | null)?.providers;
  if (!Array.isArray(providers)) return [];

  return Array.from(
    new Set(
      providers
        .map((provider) => {
          if (typeof provider !== "object" || provider === null) return "";
          const entry = provider as { name?: unknown; available?: unknown };
          const name = typeof entry.name === "string" ? entry.name.trim() : "";
          if (name.toLowerCase() === "agy" && entry.available !== true)
            return "";
          return name;
        })
        .filter(
          (name) =>
            name.length > 0 &&
            !NON_PROVIDER_SOURCES.has(name) &&
            !isHiddenProvider(name),
        ),
    ),
  );
}

export function useSessionProviderOptions(): {
  providerOptions: string[];
  registryLoaded: boolean;
} {
  const [providerOptions, setProviderOptions] = useState<string[]>([]);
  const [registryLoaded, setRegistryLoaded] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/providers", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Provider fetch failed with ${response.status}`);
        }
        return response.json();
      })
      .then((data: unknown) => {
        setProviderOptions(providerNamesFromRegistry(data));
        setRegistryLoaded(true);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError")
          return;
        console.error("Failed to load session provider registry", { error });
        setRegistryLoaded(false);
      });

    return () => controller.abort();
  }, []);

  return { providerOptions, registryLoaded };
}
