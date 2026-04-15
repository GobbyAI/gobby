export interface ProviderModelOption {
  value: string;
  label: string;
  hidden?: boolean;
  is_default?: boolean;
}

export interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: ProviderModelOption[];
  source: "static" | "live" | "cache" | "failed";
}

let cachedModels: ProviderModelEntry[] | null = null;
let cachedModelsTimestamp = 0;

const MODELS_CACHE_TTL_MS = 5 * 60 * 1000;

export async function fetchProviderModelCatalog(): Promise<ProviderModelEntry[]> {
  const now = Date.now();
  const cacheFresh =
    cachedModels !== null && now - cachedModelsTimestamp < MODELS_CACHE_TTL_MS;
  if (cacheFresh) {
    return cachedModels ?? [];
  }

  try {
    const res = await fetch("/api/providers/models");
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data?.providers)) {
        cachedModels = data.providers as ProviderModelEntry[];
        cachedModelsTimestamp = now;
        return cachedModels;
      }
    }
  } catch {
    // Use empty catalog when the daemon is unavailable.
  }

  return [];
}

export function getModelsForProvider(
  catalog: ProviderModelEntry[],
  provider: string,
): ProviderModelOption[] {
  const entry = catalog.find((candidate) => candidate.provider === provider);
  return Array.isArray(entry?.models) ? entry.models : [];
}

export function getPreferredModelForProvider(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  preferredModel?: string | null,
): string | null {
  const normalizedProvider = provider?.trim();
  if (!normalizedProvider) {
    return preferredModel?.trim() || null;
  }

  const models = getModelsForProvider(catalog, normalizedProvider);
  if (models.length === 0) {
    return preferredModel?.trim() || null;
  }

  const normalizedPreferred = preferredModel?.trim() || null;
  if (normalizedPreferred && models.some((model) => model.value === normalizedPreferred)) {
    return normalizedPreferred;
  }

  return (
    models.find((model) => model.is_default && !model.hidden)?.value ??
    models.find((model) => !model.hidden)?.value ??
    models[0]?.value ??
    null
  );
}

export function resolveProviderModelPair(
  catalog: ProviderModelEntry[],
  primary: { provider?: string | null; model?: string | null },
  fallback?: { provider?: string | null; model?: string | null },
): { provider: string | null; model: string | null } {
  const provider = primary.provider?.trim() || fallback?.provider?.trim() || null;
  if (!provider) {
    return {
      provider: null,
      model: primary.model?.trim() || fallback?.model?.trim() || null,
    };
  }

  const sameProviderFallbackModel =
    fallback?.provider?.trim() === provider ? fallback.model?.trim() || null : null;

  return {
    provider,
    model: getPreferredModelForProvider(
      catalog,
      provider,
      primary.model?.trim() || sameProviderFallbackModel,
    ),
  };
}
