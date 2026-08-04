export interface ProviderModelReasoning {
  supported_efforts: string[];
  default_effort?: string;
}

export interface ProviderModelRouteActivation {
  kind: string;
  surface: string;
  params: Record<string, unknown>;
}

export interface ProviderModelRoute {
  selector: string;
  available: boolean;
  usage_multiplier: string | null;
  throughput_multiplier: string | null;
  latency_class: string | null;
  activations: ProviderModelRouteActivation[];
}

export type ProviderModelRoutes = Partial<
  Record<"standard" | "fast", ProviderModelRoute>
>;

export interface ProviderModelRefreshSource {
  source_key: string;
  source_url?: string | null;
  required?: boolean;
  state: "pending" | "ok" | "stale" | "error";
  attempts?: number;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
}

export interface ProviderModelRefresh {
  generation: number;
  sources: ProviderModelRefreshSource[];
}

export interface ProviderModelOption {
  value: string;
  label: string;
  hidden?: boolean;
  is_default?: boolean;
  canonical_id?: string;
  context_length?: number | null;
  context_length_source?: string;
  input_modalities?: string[];
  supports_tools?: boolean;
  execution_provider?: string;
  reasoning?: ProviderModelReasoning;
  routes?: ProviderModelRoutes;
}

export interface ProviderModelEntry {
  provider: string;
  execution_provider?: string;
  available: boolean;
  models: ProviderModelOption[];
  source: "static" | "live" | "cache" | "config" | "failed" | "unsupported";
  display_name?: string;
  /** Local endpoint protocol ("lmstudio", "ollama") used for icon resolution. */
  provider_type?: string;
  installed?: boolean;
  deprecated?: boolean;
  deprecation_message?: string | null;
  supports_web_chat?: boolean;
  supports_agent_spawn?: boolean;
  unavailable_reason?: string | null;
  refresh?: ProviderModelRefresh;
}

interface ProviderMatrixFact {
  value: number | null;
  source: string;
}

interface ProviderMatrixReasoning {
  status: "known" | "unsupported" | "unknown";
  supported_efforts: string[] | null;
  default_effort: string | null;
}

interface ProviderMatrixModel {
  canonical_model: string;
  display_name: string;
  aliases: string[];
  available: boolean;
  hidden: boolean;
  is_default: boolean;
  context_length: ProviderMatrixFact;
  max_output_tokens: ProviderMatrixFact;
  latency_class: string | null;
  reasoning: ProviderMatrixReasoning;
  input_modalities: string[] | null;
  supports_tools: boolean | null;
  routes: ProviderModelRoutes;
  provenance: Record<string, unknown>;
}

type ProviderModelPayload = ProviderModelOption | ProviderMatrixModel;
type ProviderModelEntryPayload = Omit<ProviderModelEntry, "models"> & {
  models: ProviderModelPayload[];
};

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
  grok: "Grok",
  agy: "AGY",
  qwen: "Qwen",
  openai: "OpenAI",
};

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isProviderModelReasoning(value: unknown): value is ProviderModelReasoning {
  return (
    isRecord(value) &&
    Array.isArray(value.supported_efforts) &&
    value.supported_efforts.every((effort) => typeof effort === "string") &&
    (value.default_effort === undefined || typeof value.default_effort === "string")
  );
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isProviderModelRoutes(value: unknown): value is ProviderModelRoutes {
  if (!isRecord(value)) return false;
  return Object.entries(value).every(
    ([speed, route]) =>
      ["standard", "fast"].includes(speed) &&
      isRecord(route) &&
      typeof route.selector === "string" &&
      typeof route.available === "boolean" &&
      isNullableString(route.usage_multiplier) &&
      isNullableString(route.throughput_multiplier) &&
      isNullableString(route.latency_class) &&
      Array.isArray(route.activations) &&
      route.activations.every(
        (activation) =>
          isRecord(activation) &&
          typeof activation.kind === "string" &&
          typeof activation.surface === "string" &&
          isRecord(activation.params),
      ),
  );
}

function isProviderModelRefresh(value: unknown): value is ProviderModelRefresh {
  if (
    !isRecord(value) ||
    !Number.isInteger(value.generation) ||
    !Array.isArray(value.sources)
  ) {
    return false;
  }
  return value.sources.every(
    (source) =>
      isRecord(source) &&
      typeof source.source_key === "string" &&
      ["pending", "ok", "stale", "error"].includes(source.state as string) &&
      (source.source_url === undefined || isNullableString(source.source_url)) &&
      (source.required === undefined || typeof source.required === "boolean") &&
      (source.attempts === undefined || Number.isInteger(source.attempts)) &&
      (source.last_attempt_at === undefined || isNullableString(source.last_attempt_at)) &&
      (source.last_success_at === undefined || isNullableString(source.last_success_at)) &&
      (source.last_error === undefined || isNullableString(source.last_error)),
  );
}

function isLegacyProviderModelOption(value: unknown): value is ProviderModelOption {
  return (
    isRecord(value) &&
    typeof value.value === "string" &&
    typeof value.label === "string" &&
    (value.hidden === undefined || typeof value.hidden === "boolean") &&
    (value.is_default === undefined || typeof value.is_default === "boolean") &&
    (value.canonical_id === undefined || typeof value.canonical_id === "string") &&
    (value.input_modalities === undefined ||
      (Array.isArray(value.input_modalities) &&
        value.input_modalities.every((modality) => typeof modality === "string"))) &&
    (value.supports_tools === undefined || typeof value.supports_tools === "boolean") &&
    (value.execution_provider === undefined ||
      typeof value.execution_provider === "string") &&
    (value.context_length === undefined ||
      value.context_length === null ||
      typeof value.context_length === "number") &&
    (value.context_length_source === undefined ||
      ["override", "provider_reported", "provider_catalog", "registry", "unknown"].includes(
        value.context_length_source as string,
      )) &&
    (value.reasoning === undefined || isProviderModelReasoning(value.reasoning))
  );
}

function isProviderMatrixFact(value: unknown): value is ProviderMatrixFact {
  return (
    isRecord(value) &&
    (value.value === null || typeof value.value === "number") &&
    typeof value.source === "string"
  );
}

function isProviderMatrixModel(value: unknown): value is ProviderMatrixModel {
  return (
    isRecord(value) &&
    typeof value.canonical_model === "string" &&
    typeof value.display_name === "string" &&
    isStringArray(value.aliases) &&
    typeof value.available === "boolean" &&
    typeof value.hidden === "boolean" &&
    typeof value.is_default === "boolean" &&
    isProviderMatrixFact(value.context_length) &&
    isProviderMatrixFact(value.max_output_tokens) &&
    isNullableString(value.latency_class) &&
    isRecord(value.reasoning) &&
    ["known", "unsupported", "unknown"].includes(value.reasoning.status as string) &&
    (value.reasoning.supported_efforts === null ||
      isStringArray(value.reasoning.supported_efforts)) &&
    isNullableString(value.reasoning.default_effort) &&
    (value.input_modalities === null || isStringArray(value.input_modalities)) &&
    (value.supports_tools === null || typeof value.supports_tools === "boolean") &&
    isProviderModelRoutes(value.routes) &&
    isRecord(value.provenance)
  );
}

function isProviderModelOption(value: unknown): value is ProviderModelPayload {
  return isLegacyProviderModelOption(value) || isProviderMatrixModel(value);
}

function isProviderModelEntry(value: unknown): value is ProviderModelEntryPayload {
  return (
    isRecord(value) &&
    typeof value.provider === "string" &&
    (value.execution_provider === undefined ||
      (typeof value.execution_provider === "string" &&
        value.execution_provider.trim().length > 0)) &&
    typeof value.available === "boolean" &&
    Array.isArray(value.models) &&
    value.models.every(isProviderModelOption) &&
    ["static", "live", "cache", "config", "failed", "unsupported"].includes(
      value.source as string,
    ) &&
    (value.display_name === undefined || typeof value.display_name === "string") &&
    (value.provider_type === undefined || typeof value.provider_type === "string") &&
    (value.installed === undefined || typeof value.installed === "boolean") &&
    (value.deprecated === undefined || typeof value.deprecated === "boolean") &&
    (value.deprecation_message === undefined ||
      value.deprecation_message === null ||
      typeof value.deprecation_message === "string") &&
    (value.supports_web_chat === undefined || typeof value.supports_web_chat === "boolean") &&
    (value.supports_agent_spawn === undefined ||
      typeof value.supports_agent_spawn === "boolean") &&
    (value.unavailable_reason === undefined ||
      value.unavailable_reason === null ||
      typeof value.unavailable_reason === "string") &&
    (value.refresh === undefined || isProviderModelRefresh(value.refresh))
  );
}

function mapProviderModel(model: ProviderModelPayload): ProviderModelOption {
  if ("value" in model) return model;
  return {
    value: model.canonical_model,
    label: model.display_name,
    canonical_id: model.canonical_model,
    hidden: model.hidden,
    is_default: model.is_default,
    context_length: model.context_length.value,
    context_length_source: model.context_length.source,
    ...(model.input_modalities === null
      ? {}
      : { input_modalities: model.input_modalities }),
    ...(model.supports_tools === null ? {} : { supports_tools: model.supports_tools }),
    ...(model.reasoning.supported_efforts === null
      ? {}
      : {
          reasoning: {
            supported_efforts: model.reasoning.supported_efforts,
            ...(model.reasoning.default_effort === null
              ? {}
              : { default_effort: model.reasoning.default_effort }),
          },
        }),
    routes: model.routes,
  };
}

function mapProviderModelEntry(entry: ProviderModelEntryPayload): ProviderModelEntry {
  return { ...entry, models: entry.models.map(mapProviderModel) };
}

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
          .map(mapProviderModelEntry);
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
  const safePreferredModel = preferredModel?.trim().startsWith("local:")
    ? null
    : preferredModel;
  const normalizedProvider = provider?.trim();
  if (!normalizedProvider) {
    return safePreferredModel?.trim() || null;
  }

  const models = getModelsForSelection(catalog, normalizedProvider, safePreferredModel);
  if (models.length === 0) {
    return safePreferredModel?.trim() || null;
  }

  const matchedModel = findMatchingModelOption(models, safePreferredModel);
  if (matchedModel) {
    return matchedModel.value;
  }

  return models.find((model) => model.is_default)?.value ?? models[0]?.value ?? null;
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
    const identity = normalizeModelIdentifier(
      resolved.canonical_id ?? resolved.value,
    ) ?? resolved.value;
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
  const tokens = tokenizeModel(model.canonical_id || model.value || model.label);
  const family = tokens.find((token) => ["fable", "opus", "sonnet", "haiku"].includes(token));
  const versionParts = extractVersionParts(tokens, family);
  const releaseDate = extractReleaseDate(tokens);
  const familyScore =
    family === "fable" ? 400 : family === "opus" ? 300 : family === "sonnet" ? 200 : 100;

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
  const flavors = CODEX_FLAVORS.filter((flavor) => tokens.includes(flavor.token));
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
    return (rightParsed.releaseDate ?? "").localeCompare(leftParsed.releaseDate ?? "");
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
