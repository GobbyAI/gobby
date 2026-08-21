import type {
  ProviderModelEntry,
  ProviderModelOption,
  ProviderModelReasoning,
  ProviderModelRefresh,
  ProviderModelRoutes,
} from "./providerModelTypes";

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isProviderModelReasoning(
  value: unknown,
): value is ProviderModelReasoning {
  return (
    isRecord(value) &&
    Array.isArray(value.supported_efforts) &&
    value.supported_efforts.every((effort) => typeof effort === "string") &&
    (value.default_effort === undefined ||
      typeof value.default_effort === "string")
  );
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
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
      (source.source_url === undefined ||
        isNullableString(source.source_url)) &&
      (source.required === undefined || typeof source.required === "boolean") &&
      (source.attempts === undefined || Number.isInteger(source.attempts)) &&
      (source.last_attempt_at === undefined ||
        isNullableString(source.last_attempt_at)) &&
      (source.last_success_at === undefined ||
        isNullableString(source.last_success_at)) &&
      (source.last_error === undefined || isNullableString(source.last_error)),
  );
}

function isLegacyProviderModelOption(
  value: unknown,
): value is ProviderModelOption {
  return (
    isRecord(value) &&
    typeof value.value === "string" &&
    typeof value.label === "string" &&
    (value.hidden === undefined || typeof value.hidden === "boolean") &&
    (value.is_default === undefined || typeof value.is_default === "boolean") &&
    (value.canonical_id === undefined ||
      typeof value.canonical_id === "string") &&
    (value.input_modalities === undefined ||
      value.input_modalities === null ||
      isStringArray(value.input_modalities)) &&
    (value.supports_tools === undefined ||
      typeof value.supports_tools === "boolean") &&
    (value.execution_provider === undefined ||
      typeof value.execution_provider === "string") &&
    (value.context_length === undefined ||
      value.context_length === null ||
      typeof value.context_length === "number") &&
    (value.context_length_source === undefined ||
      [
        "override",
        "provider_reported",
        "provider_catalog",
        "registry",
        "unknown",
      ].includes(value.context_length_source as string)) &&
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
    ["known", "unsupported", "unknown"].includes(
      value.reasoning.status as string,
    ) &&
    (value.reasoning.supported_efforts === null ||
      isStringArray(value.reasoning.supported_efforts)) &&
    isNullableString(value.reasoning.default_effort) &&
    (value.input_modalities === null ||
      isStringArray(value.input_modalities)) &&
    (value.supports_tools === null ||
      typeof value.supports_tools === "boolean") &&
    isProviderModelRoutes(value.routes) &&
    isRecord(value.provenance)
  );
}

function isProviderModelOption(value: unknown): value is ProviderModelPayload {
  return isLegacyProviderModelOption(value) || isProviderMatrixModel(value);
}

export function isProviderModelEntry(
  value: unknown,
): value is ProviderModelEntryPayload {
  return (
    isRecord(value) &&
    typeof value.provider === "string" &&
    (value.execution_provider === undefined ||
      (typeof value.execution_provider === "string" &&
        value.execution_provider.trim().length > 0)) &&
    typeof value.available === "boolean" &&
    Array.isArray(value.models) &&
    value.models.every(isProviderModelOption) &&
    (value.source === undefined ||
      ["static", "live", "cache", "config", "failed", "unsupported"].includes(
        value.source as string,
      )) &&
    (value.display_name === undefined ||
      typeof value.display_name === "string") &&
    (value.provider_type === undefined ||
      typeof value.provider_type === "string") &&
    (value.installed === undefined || typeof value.installed === "boolean") &&
    (value.deprecated === undefined || typeof value.deprecated === "boolean") &&
    (value.deprecation_message === undefined ||
      value.deprecation_message === null ||
      typeof value.deprecation_message === "string") &&
    (value.supports_web_chat === undefined ||
      typeof value.supports_web_chat === "boolean") &&
    (value.supports_agent_spawn === undefined ||
      typeof value.supports_agent_spawn === "boolean") &&
    (value.unavailable_reason === undefined ||
      value.unavailable_reason === null ||
      typeof value.unavailable_reason === "string") &&
    (value.refresh === undefined || isProviderModelRefresh(value.refresh))
  );
}

function mapProviderModel(model: ProviderModelPayload): ProviderModelOption {
  if ("value" in model) {
    // /api/providers/models emits `input_modalities: null` for "unknown";
    // normalize to absent so legacy and matrix options agree.
    if (model.input_modalities === null) {
      const { input_modalities: _unknown, ...rest } = model;
      return rest;
    }
    return model;
  }
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
    ...(model.supports_tools === null
      ? {}
      : { supports_tools: model.supports_tools }),
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

export function mapProviderModelEntry(
  entry: ProviderModelEntryPayload,
): ProviderModelEntry {
  return { ...entry, models: entry.models.map(mapProviderModel) };
}
