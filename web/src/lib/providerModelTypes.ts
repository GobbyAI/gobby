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
  input_modalities?: string[] | null;
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
  source?: "static" | "live" | "cache" | "config" | "failed" | "unsupported";
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

export interface ReasoningOption {
  value: string;
  label: string;
  disabled?: boolean;
}
