import {
  isProviderModelEntry,
  mapProviderModelEntry,
} from "./providerModelCatalog";
import type {
  ProviderModelEntry,
  ProviderModelOption,
  ReasoningOption,
} from "./providerModelTypes";

export type {
  ProviderModelEntry,
  ProviderModelOption,
  ProviderModelReasoning,
  ProviderModelRefresh,
  ProviderModelRefreshSource,
  ProviderModelRoute,
  ProviderModelRouteActivation,
  ProviderModelRoutes,
  ReasoningOption,
} from "./providerModelTypes";

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
  grok: "Grok",
  agy: "AGY",
  qwen: "Qwen",
  openai: "OpenAI",
};

// Self-hosted endpoint providers surface as "endpoint:<id>"; the scheme is an
// implementation detail, so labels show only the humanized endpoint name
// (#20047 — no "Endpoint:Lm-Studio" raw casing).
const ENDPOINT_LABELS: Record<string, string> = {
  "lm-studio": "LM Studio",
  ollama: "Ollama",
  vllm: "vLLM",
};

// Providers that must never be offered as a choice anywhere in the web UI
// (filters, pickers, spawn forms). Existing sessions from these providers
// still render with their labels/badges. AGY is hidden until fully supported
// (#20049).
const HIDDEN_PROVIDERS = new Set(["agy"]);

export function isHiddenProvider(value?: string | null): boolean {
  return value ? HIDDEN_PROVIDERS.has(value.toLowerCase()) : false;
}

const CODEX_FLAVORS = [
  { token: "codex", label: "Codex", tierScore: 300 },
  { token: "mini", label: "Mini", tierScore: 200 },
  { token: "spark", label: "Spark", tierScore: 100 },
  { token: "sol", label: "Sol", tierScore: 400 },
  { token: "terra", label: "Terra", tierScore: 400 },
  { token: "luna", label: "Luna", tierScore: 400 },
] as const;

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
        const validModels = data.providers
          .filter(isProviderModelEntry)
          .map(mapProviderModelEntry)
          .filter(
            (entry: ProviderModelEntry) => !isHiddenProvider(entry.provider),
          );
        cachedModels = validModels;
        cachedModelsTimestamp = now;
        return validModels;
      }
    }
  } catch (err) {
    console.debug("Failed to load provider catalog", err);
  }

  return cachedModels ?? [];
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
  if (normalized.startsWith("endpoint:")) {
    const endpoint = normalized.slice("endpoint:".length);
    return ENDPOINT_LABELS[endpoint] ?? titleCase(endpoint.replace(/-/g, " "));
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
    (candidate) =>
      normalizeProvider(candidate.provider) === normalizeProvider(provider),
  );
  const rawModels = Array.isArray(entry?.models) ? entry.models : [];
  return resolveVisibleModels(provider, rawModels);
}

export function getModelsForSelection(
  catalog: ProviderModelEntry[],
  executionProvider: string | null | undefined,
  model: string | null | undefined,
): ProviderModelOption[] {
  const normalizedExecutionProvider = normalizeProvider(executionProvider);
  const selectedModel = model?.trim();
  if (normalizedExecutionProvider && selectedModel) {
    const owner = catalog.find((entry) => {
      const entryExecutionProvider = normalizeProvider(
        entry.execution_provider?.trim() || entry.provider,
      );
      return (
        entryExecutionProvider === normalizedExecutionProvider &&
        entry.models.some((candidate) => candidate.value === selectedModel)
      );
    });
    if (owner) {
      return resolveVisibleModels(owner.provider, owner.models);
    }
  }

  return normalizedExecutionProvider
    ? getModelsForProvider(catalog, normalizedExecutionProvider)
    : [];
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
    getModelsForSelection(catalog, normalizedProvider, normalizedModel),
    normalizedModel,
  );
  return matched?.label ?? humanizeFallbackModelLabel(model);
}

export function resolveModelValueForProvider(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): string | null {
  if (model?.trim().startsWith("local:")) {
    return null;
  }
  const normalizedProvider = normalizeProvider(provider);
  if (!normalizedProvider) {
    return normalizeModelIdentifier(model);
  }

  return (
    findMatchingModelOption(
      getModelsForSelection(catalog, normalizedProvider, model),
      model,
    )?.value ?? null
  );
}

export function inputModalityChips(
  inputModalities?: readonly string[] | null,
): Array<"Text" | "Image"> {
  if (!inputModalities) return [];
  const chips: Array<"Text" | "Image"> = [];
  if (inputModalities.includes("text")) chips.push("Text");
  if (inputModalities.includes("image")) chips.push("Image");
  return chips;
}

function isEndpointBackedOption(
  option: ProviderModelOption | null,
  ownerProvider: string | null | undefined,
  requestedModel: string | null | undefined,
): boolean {
  return (
    Boolean(option?.value.startsWith("endpoint:")) ||
    Boolean(ownerProvider?.startsWith("endpoint:")) ||
    Boolean(requestedModel?.trim().startsWith("endpoint:"))
  );
}

export function modelSupportsImageInput(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): boolean {
  const normalizedProvider = normalizeProvider(provider);
  if (!normalizedProvider) return true;
  const option = findMatchingModelOption(
    getModelsForSelection(catalog, normalizedProvider, model),
    model,
  );
  const owner = option
    ? catalog.find((entry) =>
        entry.models.some((candidate) => candidate.value === option.value),
      )
    : catalog.find(
        (entry) => normalizeProvider(entry.provider) === normalizedProvider,
      );
  if (isEndpointBackedOption(option, owner?.provider, model)) {
    return Boolean(option?.input_modalities?.includes("image"));
  }
  return option?.input_modalities
    ? option.input_modalities.includes("image")
    : true;
}

export function getReasoningOptionsForModel(
  catalog: ProviderModelEntry[],
  provider: string | null | undefined,
  model: string | null | undefined,
): ReasoningOption[] {
  const matchedModel = findMatchingModelOption(
    getModelsForSelection(catalog, provider, model),
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
  if (
    normalizedPreferred &&
    options.some((option) => option.value === normalizedPreferred)
  ) {
    return normalizedPreferred;
  }

  const matchedModel = findMatchingModelOption(
    getModelsForProvider(catalog, provider?.trim() || ""),
    model,
  );
  const defaultEffort = normalizeModelIdentifier(
    matchedModel?.reasoning?.default_effort,
  );
  if (
    defaultEffort &&
    options.some((option) => option.value === defaultEffort)
  ) {
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
  const safePreferredModel = preferredModel?.trim().startsWith("local:")
    ? null
    : preferredModel;
  const normalizedProvider = provider?.trim();
  if (!normalizedProvider) {
    return safePreferredModel?.trim() || null;
  }

  const models = getModelsForSelection(
    catalog,
    normalizedProvider,
    safePreferredModel,
  );
  if (models.length === 0) {
    return safePreferredModel?.trim() || null;
  }

  const matchedModel = findMatchingModelOption(models, safePreferredModel);
  if (matchedModel) {
    return matchedModel.value;
  }

  return (
    models.find((model) => model.is_default)?.value ?? models[0]?.value ?? null
  );
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
    fallback?.provider?.trim() === provider
      ? fallback.model?.trim() || null
      : null;
  const preferredModel =
    sameProviderFallbackModel || primary.model?.trim() || null;

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
    const identity =
      normalizeModelIdentifier(resolved.canonical_id ?? resolved.value) ??
      resolved.value;
    const existing = deduped.get(identity);
    if (!existing) {
      deduped.set(identity, resolved);
      continue;
    }

    const mergedMatchIdentifiers = Array.from(
      new Set([
        ...(existing.match_identifiers ?? []),
        ...(resolved.match_identifiers ?? []),
      ]),
    );

    if (compareResolvedModels(resolved, existing) < 0) {
      deduped.set(identity, resolved);
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
  if (normalizedProvider === "qwen") {
    return parseQwenModelInfo(model);
  }
  return parseGenericModelInfo(model);
}

function parseClaudeModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const tokens = tokenizeModel(
    model.canonical_id || model.value || model.label,
  );
  const family = tokens.find((token) =>
    ["fable", "opus", "sonnet", "haiku"].includes(token),
  );
  const versionParts = extractVersionParts(tokens, family);
  const releaseDate = extractReleaseDate(tokens);
  const familyScore =
    family === "fable"
      ? 400
      : family === "opus"
        ? 300
        : family === "sonnet"
          ? 200
          : 100;

  return {
    displayLabel: family
      ? `${titleCase(family)}${versionParts.length > 0 ? ` ${versionParts.join(".")}` : ""}`
      : humanizeFallbackModelLabel(model.label || model.value),
    strengthRank: familyScore * 10_000 + versionScore(versionParts),
    versionParts,
    releaseDate,
  };
}

function parseCodexModelInfo(model: ProviderModelOption): ParsedModelInfo {
  const normalized = normalizeModelIdentifier(model.value) ?? "";
  if (normalized.startsWith("endpoint:")) {
    return parseGenericModelInfo(model);
  }
  const tokens = tokenizeModel(normalized);
  const versionParts = extractVersionParts(tokens, "gpt");
  const flavors = CODEX_FLAVORS.filter((flavor) =>
    tokens.includes(flavor.token),
  );
  const tierScore = flavors.reduce(
    (score, flavor) => Math.min(score, flavor.tierScore),
    400,
  );
  const parts = [`GPT ${versionParts.join(".") || "?"}`];
  parts.push(...flavors.map((flavor) => flavor.label));

  return {
    displayLabel: parts.join(" ").trim(),
    strengthRank: tierScore * 10_000 + versionScore(versionParts),
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
  const backendLabel = (model.label ?? "").trim();
  // The backend curates qwen labels (configured display names, relabeled CLI
  // aliases); only fall back to humanizing the raw id when the label is just
  // the id itself.
  const displayLabel =
    backendLabel && backendLabel !== rawValue && backendLabel !== modelId
      ? backendLabel
      : humanizeFallbackModelLabel(modelId);
  const normalized = normalizeModelIdentifier(modelId) ?? "";
  const tokens = tokenizeModel(modelId);

  let strengthRank = 0;
  if (/claude-opus|opus/.test(normalized)) {
    strengthRank = 500_000;
  } else if (/claude-sonnet|sonnet/.test(normalized)) {
    strengthRank = 400_000;
  } else if (/gpt-5/.test(normalized)) {
    strengthRank = 350_000;
  } else if (tokens.includes("gemini") && tokens.includes("pro")) {
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
  const leftIsDefault = left.is_default === true;
  const rightIsDefault = right.is_default === true;

  if (leftIsDefault !== rightIsDefault) {
    return leftIsDefault ? -1 : 1;
  }

  if (left.is_default !== right.is_default) {
    return left.is_default ? -1 : 1;
  }

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
    return (rightParsed.releaseDate ?? "").localeCompare(
      leftParsed.releaseDate ?? "",
    );
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

function extractVersionParts(
  tokens: string[],
  anchor: string | null | undefined,
): number[] {
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

export function formatModelDisplayLabel(
  label: string | null | undefined,
): string {
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
