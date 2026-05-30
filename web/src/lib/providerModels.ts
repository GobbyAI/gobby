export interface ProviderModelReasoning {
  supported_efforts: string[];
  default_effort?: string;
}

export interface ProviderModelOption {
  value: string;
  label: string;
  hidden?: boolean;
  is_default?: boolean;
  canonical_id?: string;
  context_length?: number | null;
  context_length_source?:
    | "provider_reported"
    | "provider_catalog"
    | "registry"
    | "static_default";
  reasoning?: ProviderModelReasoning;
}

export interface ProviderModelEntry {
  provider: string;
  available: boolean;
  models: ProviderModelOption[];
  source: "static" | "live" | "cache" | "failed" | "unsupported";
  display_name?: string;
  installed?: boolean;
  deprecated?: boolean;
  deprecation_message?: string | null;
  supports_web_chat?: boolean;
  supports_agent_spawn?: boolean;
  unavailable_reason?: string | null;
}

export interface ReasoningOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface ParsedModelInfo {
  displayLabel: string;
  strengthRank: number;
  versionParts: number[];
  releaseDate: string | null;
}

interface ResolvedModelOption extends ProviderModelOption {
  match_identifiers?: string[];
  _parsed?: ParsedModelInfo;
}

const PROVIDER_LABELS: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  droid: "Droid",
  gemini: "Gemini",
  grok: "Grok",
  agy: "AGY",
  qwen: "Qwen",
  openai: "OpenAI",
};

const MODELS_CACHE_TTL_MS = 5 * 60 * 1000;
export const AUTO_REASONING_EFFORT = "auto";

let cachedModels: ProviderModelEntry[] | null = null;
let cachedModelsTimestamp = 0;

export async function fetchProviderModelCatalog(): Promise<
  ProviderModelEntry[]
> {
  const now = Date.now();
  const cacheFresh =
    cachedModels !== null && now - cachedModelsTimestamp < MODELS_CACHE_TTL_MS;
  if (cacheFresh) {
    return cachedModels ?? [];
  }

  try {
    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    const res = await fetch(`${baseUrl}/api/providers/models`);
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data?.providers)) {
        cachedModels = data.providers as ProviderModelEntry[];
        cachedModelsTimestamp = now;
        return cachedModels;
      }
    }
  } catch (err) {
    console.debug("Failed to load provider catalog", err);
  }

  return [];
}

export function clearProviderModelCache(): void {
  cachedModels = null;
  cachedModelsTimestamp = 0;
}

export function getProviderDisplayName(
  provider: string | null | undefined,
): string {
  const normalized = normalizeProvider(provider);
  if (!normalized) {
    return "";
  }
  return PROVIDER_LABELS[normalized] ?? titleCase(normalized);
}

export function getProviderDisplayNameFromEntry(
  entry: ProviderModelEntry | null | undefined,
  fallbackProvider?: string | null,
): string {
  const displayName = entry?.display_name?.trim();
  if (displayName) return displayName;
  return getProviderDisplayName(entry?.provider ?? fallbackProvider);
}

export function buildReasoningPreferenceKey(
  provider: string | null | undefined,
  model: string | null | undefined,
): string | null {
  const normalizedProvider = normalizeProvider(provider);
  const normalizedModel = normalizeModelIdentifier(model);
  if (!normalizedProvider || !normalizedModel) {
    return null;
  }
  return `${normalizedProvider}:${normalizedModel}`;
}

export function getOrderedProviders(providers: string[]): string[] {
  const uniqueProviders = Array.from(
    new Set(
      providers
        .map((provider) => normalizeProvider(provider))
        .filter((provider): provider is string => Boolean(provider)),
    ),
  );

  return uniqueProviders.sort((left, right) => {
    const labelOrder = getProviderDisplayName(left).localeCompare(
      getProviderDisplayName(right),
      undefined,
      { sensitivity: "base" },
    );
    return labelOrder || left.localeCompare(right);
  });
}

export function getModelsForProvider(
  catalog: ProviderModelEntry[],
  provider: string,
): ProviderModelOption[] {
  const entry = catalog.find(
    (candidate) => normalizeProvider(candidate.provider) === normalizeProvider(provider),
  );
  const rawModels = Array.isArray(entry?.models) ? entry.models : [];
  return resolveVisibleModels(provider, rawModels);
}

export function getModelLabel(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): string {
  const normalizedProvider = normalizeProvider(provider);
  const normalizedModel = normalizeModelIdentifier(model);
  if (!normalizedProvider) {
    return model?.trim() || "Default";
  }

  const matched = findMatchingModelOption(
    getModelsForProvider(catalog, normalizedProvider),
    normalizedModel,
  );
  return matched?.label ?? humanizeFallbackModelLabel(model);
}

export function resolveModelValueForProvider(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): string | null {
  const normalizedProvider = normalizeProvider(provider);
  if (!normalizedProvider) {
    return normalizeModelIdentifier(model);
  }

  return (
    findMatchingModelOption(
      getModelsForProvider(catalog, normalizedProvider),
      model,
    )?.value ?? null
  );
}

export function getReasoningOptionsForModel(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): ReasoningOption[] {
  const matchedModel = findMatchingModelOption(
    getModelsForProvider(catalog, provider?.trim() || ""),
    model,
  );
  const supported = Array.from(
    new Set(matchedModel?.reasoning?.supported_efforts ?? []),
  ).filter((value) => value.trim().length > 0);

  if (supported.length === 0) {
    return [{ value: AUTO_REASONING_EFFORT, label: "Auto", disabled: true }];
  }

  return [
    { value: AUTO_REASONING_EFFORT, label: "Auto" },
    ...supported.map((effort) => ({
      value: effort,
      label: formatReasoningLabel(effort),
    })),
  ];
}

export function getPreferredReasoningEffort(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
  preferredEffort?: string | null,
): string {
  const options = getReasoningOptionsForModel(catalog, provider, model);
  const normalizedPreferred = normalizeModelIdentifier(preferredEffort);
  if (normalizedPreferred && options.some((option) => option.value === normalizedPreferred)) {
    return normalizedPreferred;
  }

  const matchedModel = findMatchingModelOption(
    getModelsForProvider(catalog, provider?.trim() || ""),
    model,
  );
  const defaultEffort = normalizeModelIdentifier(
    matchedModel?.reasoning?.default_effort,
  );
  if (defaultEffort && options.some((option) => option.value === defaultEffort)) {
    return defaultEffort;
  }

  return AUTO_REASONING_EFFORT;
}

function normalizeModelIdentifier(value?: string | null): string | null {
  const normalized = value?.trim().toLowerCase() ?? "";
  return normalized.length > 0 ? normalized : null;
}

function normalizeProvider(value?: string | null): string | null {
  return normalizeModelIdentifier(value);
}

function findMatchingModelOption(
  models: ProviderModelOption[],
  requestedModel?: string | null,
): ProviderModelOption | null {
  const normalizedRequested = normalizeModelIdentifier(requestedModel);
  if (!normalizedRequested) {
    return null;
  }

  return (
    models.find((model) => {
      const candidates = [
        normalizeModelIdentifier(model.value),
        normalizeModelIdentifier(model.canonical_id),
        ...(((model as ResolvedModelOption).match_identifiers ?? []).map(
          (value) => normalizeModelIdentifier(value),
        ) as Array<string | null>),
      ].filter((candidate): candidate is string => Boolean(candidate));
      return candidates.includes(normalizedRequested);
    }) ?? null
  );
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

  const matchedModel = findMatchingModelOption(models, preferredModel);
  if (matchedModel) {
    return matchedModel.value;
  }

  return models[0]?.value ?? null;
}

export function resolveProviderModelPair(
  catalog: ProviderModelEntry[],
  primary: { provider?: string | null; model?: string | null },
  fallback?: { provider?: string | null; model?: string | null },
): { provider: string | null; model: string | null } {
  const provider =
    primary.provider?.trim() || fallback?.provider?.trim() || null;
  if (!provider) {
    return {
      provider: null,
      model: primary.model?.trim() || fallback?.model?.trim() || null,
    };
  }

  const sameProviderFallbackModel =
    fallback?.provider?.trim() === provider ? fallback.model?.trim() || null : null;
  const preferredModel = sameProviderFallbackModel || primary.model?.trim() || null;

  return {
    provider,
    model: getPreferredModelForProvider(catalog, provider, preferredModel),
  };
}

function resolveVisibleModels(
  provider: string,
  models: ProviderModelOption[],
): ProviderModelOption[] {
  const visibleModels = models.filter((model) => !model.hidden);
  const deduped = new Map<string, ResolvedModelOption>();

  for (const model of visibleModels) {
    const resolved = resolveModelOption(provider, model);
    const existing = deduped.get(resolved.label);
    if (!existing) {
      deduped.set(resolved.label, resolved);
      continue;
    }

    const mergedMatchIdentifiers = Array.from(
      new Set([
        ...(existing.match_identifiers ?? []),
        ...(resolved.match_identifiers ?? []),
      ]),
    );

    if (compareResolvedModels(resolved, existing) < 0) {
      deduped.set(resolved.label, resolved);
      resolved.match_identifiers = mergedMatchIdentifiers;
    } else {
      existing.match_identifiers = mergedMatchIdentifiers;
    }
  }

  return Array.from(deduped.values()).sort(compareResolvedModels);
}

function resolveModelOption(
  provider: string,
  model: ProviderModelOption,
): ResolvedModelOption {
  const parsed = parseModelInfo(provider, model);
  const matchIdentifiers = [
    model.value,
    model.canonical_id,
    parsed.releaseDate ? `${model.value}:${parsed.releaseDate}` : null,
  ].filter((value): value is string => Boolean(value));

  return {
    ...model,
    label: parsed.displayLabel,
    match_identifiers: matchIdentifiers,
    _parsed: parsed,
  };
}

function parseModelInfo(
  provider: string,
  model: ProviderModelOption,
): ParsedModelInfo {
  const normalizedProvider = normalizeProvider(provider) ?? "claude";
  if (normalizedProvider === "claude") {
    return parseClaudeModelInfo(model);
  }
  if (normalizedProvider === "codex") {
    return parseCodexModelInfo(model);
  }
  if (normalizedProvider === "gemini") {
    return parseGeminiModelInfo(model);
  }
  if (normalizedProvider === "qwen") {
    return parseQwenModelInfo(model);
  }
  return parseGenericModelInfo(model);
}

function parseClaudeModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const tokens = tokenizeModel(model.canonical_id || model.value || model.label);
  const family = tokens.find((token) => ["opus", "sonnet", "haiku"].includes(token));
  const versionParts = extractVersionParts(tokens, family);
  const releaseDate = extractReleaseDate(tokens);
  const familyScore = family === "opus" ? 300 : family === "sonnet" ? 200 : 100;

  return {
    displayLabel: family
      ? `${titleCase(family)}${versionParts.length > 0 ? ` ${versionParts.join(".")}` : ""}`
      : humanizeFallbackModelLabel(model.label || model.value),
    strengthRank: familyScore + versionScore(versionParts),
    versionParts,
    releaseDate,
  };
}

function parseCodexModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const normalized = normalizeModelIdentifier(model.value) ?? "";
  const tokens = tokenizeModel(normalized);
  const versionParts = extractVersionParts(tokens, "gpt");
  const isMini = tokens.includes("mini");
  const isSpark = tokens.includes("spark");
  const isCodex = tokens.includes("codex");
  const tierScore = isSpark ? 100 : isMini ? 200 : isCodex ? 300 : 400;
  const parts = [`GPT ${versionParts.join(".") || "?"}`];
  if (isCodex) {
    parts.push("Codex");
  }
  if (isMini) {
    parts.push("Mini");
  }
  if (isSpark) {
    parts.push("Spark");
  }

  return {
    displayLabel: parts.join(" ").trim(),
    strengthRank: tierScore * 10_000 + versionScore(versionParts),
    versionParts,
    releaseDate: null,
  };
}

function parseGeminiModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const tokens = tokenizeModel(model.value || model.label);
  const versionParts = extractVersionParts(tokens, "gemini");
  const isPro = tokens.includes("pro");
  const isFlash = tokens.includes("flash");
  const isPreview = tokens.includes("preview");
  const tierScore = isPro ? 300 : isFlash ? 200 : 100;
  const stabilityScore = isPreview ? 0 : 50;
  const variant = isPro ? "Pro" : isFlash ? "Flash" : "";

  return {
    displayLabel: [
      "Gemini",
      versionParts.join("."),
      variant,
    ]
      .filter(Boolean)
      .join(" "),
    strengthRank: tierScore * 10_000 + stabilityScore * 100 + versionScore(versionParts),
    versionParts,
    releaseDate: null,
  };
}

function stripTrailingParentheticalGroups(value: string): string {
  // Iterate instead of regex so nested trailing groups like "(foo (bar))" are stripped correctly.
  let cleaned = value.trim();

  while (cleaned.endsWith(")")) {
    let depth = 0;
    let matchingOpenIndex = -1;

    for (let index = cleaned.length - 1; index >= 0; index -= 1) {
      const char = cleaned[index];
      if (char === ")") {
        depth += 1;
      } else if (char === "(") {
        depth -= 1;
        if (depth === 0) {
          matchingOpenIndex = index;
          break;
        }
      }
    }

    if (matchingOpenIndex < 0) {
      break;
    }

    cleaned = cleaned.slice(0, matchingOpenIndex).trimEnd();
  }

  return cleaned;
}

function parseQwenModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const rawValue = model.value || model.label;
  const modelId = stripTrailingParentheticalGroups(rawValue);
  const displayLabel = humanizeFallbackModelLabel(modelId);
  const normalized = normalizeModelIdentifier(modelId) ?? "";

  let strengthRank = 0;
  if (/claude-opus|opus/.test(normalized)) {
    strengthRank = 500_000;
  } else if (/claude-sonnet|sonnet/.test(normalized)) {
    strengthRank = 400_000;
  } else if (/gpt-5/.test(normalized)) {
    strengthRank = 350_000;
  } else if (/gemini.*pro|pro/.test(normalized)) {
    strengthRank = 300_000;
  } else if (/flash/.test(normalized)) {
    strengthRank = 200_000;
  }

  return {
    displayLabel,
    strengthRank,
    versionParts: extractVersionParts(tokenizeModel(modelId), null),
    releaseDate: null,
  };
}

function parseGenericModelInfo(model: ProviderModelOption): ParsedModelInfo {
  return {
    displayLabel: humanizeFallbackModelLabel(model.label || model.value),
    strengthRank: 0,
    versionParts: [],
    releaseDate: null,
  };
}

function compareResolvedModels(
  left: ResolvedModelOption,
  right: ResolvedModelOption,
): number {
  const leftParsed = left._parsed ?? parseGenericModelInfo(left);
  const rightParsed = right._parsed ?? parseGenericModelInfo(right);

  if (leftParsed.strengthRank !== rightParsed.strengthRank) {
    return rightParsed.strengthRank - leftParsed.strengthRank;
  }

  const versionDiff = compareNumberArrays(
    rightParsed.versionParts,
    leftParsed.versionParts,
  );
  if (versionDiff !== 0) {
    return versionDiff;
  }

  if (leftParsed.releaseDate !== rightParsed.releaseDate) {
    return (rightParsed.releaseDate ?? "").localeCompare(leftParsed.releaseDate ?? "");
  }

  if (left.is_default !== right.is_default) {
    return left.is_default ? -1 : 1;
  }

  return left.label.localeCompare(right.label);
}

function compareNumberArrays(left: number[], right: number[]): number {
  const maxLength = Math.max(left.length, right.length);
  for (let index = 0; index < maxLength; index += 1) {
    const leftValue = left[index] ?? 0;
    const rightValue = right[index] ?? 0;
    if (leftValue !== rightValue) {
      return leftValue - rightValue;
    }
  }
  return 0;
}

function tokenizeModel(value: string): string[] {
  return value
    .toLowerCase()
    .replace(/[()]/g, " ")
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

function extractVersionParts(tokens: string[], anchor: string | null | undefined): number[] {
  const anchorIndex = anchor ? tokens.indexOf(anchor) : -1;
  const startIndex = anchorIndex >= 0 ? anchorIndex + 1 : 0;
  const versionParts: number[] = [];

  for (const token of tokens.slice(startIndex)) {
    if (!/^\d+$/.test(token)) {
      if (versionParts.length > 0) {
        break;
      }
      continue;
    }
    if (token.length >= 8) {
      break;
    }
    versionParts.push(Number(token));
  }

  return versionParts;
}

function extractReleaseDate(tokens: string[]): string | null {
  const dateToken = tokens.find((token) => /^\d{8}$/.test(token));
  return dateToken ?? null;
}

function versionScore(versionParts: number[]): number {
  return versionParts.reduce((total, part, index) => {
    const multiplier = 100 ** Math.max(0, 2 - index);
    return total + part * multiplier;
  }, 0);
}

function formatReasoningLabel(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (normalized === "xhigh") {
    return "XHigh";
  }
  if (normalized === "medium") {
    return "Med";
  }
  return titleCase(normalized);
}

export function formatModelDisplayLabel(label: string | null | undefined): string {
  const raw = label?.trim() ?? "";
  if (!raw) return "";
  return raw
    .replace(/\bClaude\s+/g, "")
    .replace(/\bDroid Core\s+/g, "")
    .replace(/\s+Mode\b/g, "")
    .replace(/\[Deprecated\]/g, "[D]")
    .replace(/[()]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function humanizeFallbackModelLabel(value?: string | null): string {
  const raw = value?.trim() ?? "";
  if (!raw) {
    return "Default";
  }

  const compact = raw.replace(/\(([^)]+)\)$/g, (_match, suffix: string) => {
    return ` (${titleCase(suffix.replace(/-/g, " "))})`;
  });

  if (/^gpt-\d/i.test(compact)) {
    return compact
      .replace(/^gpt-/i, "GPT ")
      .replace(/-mini/gi, " Mini")
      .replace(/-spark/gi, " Spark")
      .replace(/-codex/gi, " Codex");
  }

  return compact
    .replace(/[-_]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

function titleCase(value: string): string {
  return value
    .replace(/\b\w/g, (match) => match.toUpperCase())
    .replace(/\bOpenai\b/g, "OpenAI")
    .replace(/\bVertex Ai\b/g, "Vertex AI");
}
