import { useEffect, useState } from "react";
import {
  NumberField,
  SwitchField,
  TextField,
  type FieldOption,
} from "../../activity/fields";
import {
  BoundedSelectField,
  KeyValueMapField,
  KeyValueRowsField,
  StringListField,
} from "../fields";
import { Chip } from "../../ui/Chip";
import {
  fetchProviderModelCatalog,
  getModelsForProvider,
  getOrderedProviders,
  getProviderDisplayName,
  inputModalityChips,
  type ProviderModelEntry,
  type ProviderModelOption,
} from "../../../lib/providerModels";
import type { UseSettingsReturn } from "../../../hooks/useSettings";
import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import {
  useSettingsSectionContext,
  type ProviderSelectionContextValue,
} from "./SettingsSectionContext";
import {
  asMap,
  decodeDynamicMapRows,
  encodeDynamicMapRows,
} from "./configAccessors";
import { enumOptionsAt } from "../configSchema";
import {
  NumberConfigField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  TextConfigField,
} from "./configFields";

// One LLM feature's owned config rows. Each feature exposes a capability
// profile (`FeatureProfile` enum) and an ordered candidate list; some also
// carry an `enabled` toggle, prompt-path overrides, or a confidence threshold.
interface FeatureSpec {
  key: string;
  label: string;
  hasEnabled: boolean;
  hasConfidence?: boolean;
  promptPaths: { suffix: string; label: string }[];
}

const FEATURE_SPECS: readonly FeatureSpec[] = [
  {
    key: "recommend_tools",
    label: "Tool recommendation",
    hasEnabled: true,
    promptPaths: [
      { suffix: "prompt_path", label: "System prompt path" },
      {
        suffix: "hybrid_rerank_prompt_path",
        label: "Hybrid re-rank prompt path",
      },
      { suffix: "llm_prompt_path", label: "LLM prompt path" },
    ],
  },
  {
    key: "tool_summarizer",
    label: "Tool summarizer",
    hasEnabled: true,
    promptPaths: [
      { suffix: "prompt_path", label: "Prompt path" },
      { suffix: "system_prompt_path", label: "System prompt path" },
      {
        suffix: "server_description_prompt_path",
        label: "Server description prompt path",
      },
      {
        suffix: "server_description_system_prompt_path",
        label: "Server description system prompt path",
      },
    ],
  },
  {
    key: "import_mcp_server",
    label: "MCP server import",
    hasEnabled: true,
    promptPaths: [
      { suffix: "prompt_path", label: "Prompt path" },
      { suffix: "github_fetch_prompt_path", label: "GitHub fetch prompt path" },
      { suffix: "search_fetch_prompt_path", label: "Search fetch prompt path" },
    ],
  },
  {
    key: "project_verification_synthesis",
    label: "Verification synthesis",
    hasEnabled: false,
    hasConfidence: true,
    promptPaths: [],
  },
  {
    key: "merge_resolution",
    label: "Merge resolution",
    hasEnabled: false,
    promptPaths: [],
  },
  {
    key: "skill_description",
    label: "Skill description",
    hasEnabled: false,
    promptPaths: [],
  },
];

const GENERATION_NUMBER_FIELDS: readonly { suffix: string; label: string }[] = [
  { suffix: "timeout_seconds", label: "Timeout (seconds)" },
  { suffix: "candidate_timeout_seconds", label: "Candidate timeout (seconds)" },
  {
    suffix: "cli_candidate_timeout_seconds",
    label: "CLI candidate timeout (seconds)",
  },
];

const GENERATION_PREFIX = "ai.generation";
const GENERATION_ENDPOINTS_PATH = `${GENERATION_PREFIX}.endpoints`;
const PROFILE_DEFAULTS_PATH = `${GENERATION_PREFIX}.profile_defaults`;
const CONTEXT_WINDOW_PATH = "context_window_overrides";
const OPENAI_COMPATIBLE_PROTOCOL = "openai-compatible";
const CHAT_COMPLETIONS_WIRE_API = "chat-completions";

interface GenerationEndpoint {
  protocol?: string;
  wire_api?: string;
  api_base?: string;
  model?: string;
  api_key?: string | null;
  tool_chat?: boolean;
}

function omitVisionExtract(
  endpoint: GenerationEndpoint & { vision_extract?: boolean },
): GenerationEndpoint {
  const { vision_extract: _unused, ...rest } = endpoint;
  return rest;
}

function sanitizeEndpointMap(
  endpoints: Record<string, GenerationEndpoint>,
): Record<string, GenerationEndpoint> {
  return Object.fromEntries(
    Object.entries(endpoints).map(([slug, endpoint]) => [
      slug,
      omitVisionExtract(endpoint),
    ]),
  );
}

function endpointEnumOptions(
  schema: Record<string, unknown> | null,
  slug: string,
  field: "protocol" | "wire_api",
): FieldOption[] {
  return enumOptionsAt(
    schema,
    `${GENERATION_ENDPOINTS_PATH}.${slug || "_"}.${field}`,
  );
}

function protocolForcesChatCompletions(protocol: string): boolean {
  return protocol !== OPENAI_COMPATIBLE_PROTOCOL;
}

function inputModalitiesForEndpoint(
  catalog: ProviderModelEntry[],
  slug: string,
  configuredModel?: string,
): string[] | null | undefined {
  if (!slug) return undefined;
  const groupName = `endpoint:${slug}`.toLowerCase();
  const group = catalog.find(
    (entry) => entry.provider.toLowerCase() === groupName,
  );
  const fromGroup = group?.models ?? [];
  const fromScattered = catalog.flatMap((entry) =>
    entry.models.filter((model) => {
      const value = model.value.toLowerCase();
      return value === groupName || value.startsWith(`${groupName}/`);
    }),
  );
  const models: ProviderModelOption[] =
    fromGroup.length > 0 ? fromGroup : fromScattered;
  if (models.length === 0) return undefined;

  const trimmed = configuredModel?.trim() ?? "";
  const isAuto = trimmed === "" || trimmed.toLowerCase() === "auto";
  if (isAuto) {
    return (
      models.find((model) => model.is_default) ??
      models.find((model) => model.value.toLowerCase() === groupName)
    )?.input_modalities;
  }

  const needle = trimmed.toLowerCase();
  return models.find((model) => {
    const value = model.value.toLowerCase();
    const canonical = model.canonical_id?.toLowerCase();
    return (
      value === needle ||
      canonical === needle ||
      value === `${groupName}/${needle}` ||
      value.endsWith(`/${needle}`)
    );
  })?.input_modalities;
}

function featurePromptPaths(spec: FeatureSpec): string[] {
  return spec.promptPaths.map((prompt) => `${spec.key}.${prompt.suffix}`);
}

function featureOwnedPaths(spec: FeatureSpec): string[] {
  return [
    `${spec.key}.profile`,
    `${spec.key}.candidates`,
    ...(spec.hasEnabled ? [`${spec.key}.enabled`] : []),
    ...(spec.hasConfidence ? [`${spec.key}.confidence_threshold`] : []),
    ...featurePromptPaths(spec),
  ];
}

// The exact set of dotted config paths this section owns (and the draft is
// scoped to). Derived from the specs so the draft can never drift from what the
// section renders.
const OWNED_PATHS: readonly string[] = [
  ...FEATURE_SPECS.flatMap(featureOwnedPaths),
  ...GENERATION_NUMBER_FIELDS.map(
    (field) => `${GENERATION_PREFIX}.${field.suffix}`,
  ),
  GENERATION_ENDPOINTS_PATH,
  PROFILE_DEFAULTS_PATH,
  CONTEXT_WINDOW_PATH,
];

function ModelProviderControls({
  clientSettings,
  providerSelection,
}: {
  clientSettings?: UseSettingsReturn;
  providerSelection?: ProviderSelectionContextValue;
}) {
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([]);

  useEffect(() => {
    if (!clientSettings || !providerSelection) return undefined;
    let cancelled = false;
    void fetchProviderModelCatalog().then((entries) => {
      if (!cancelled) setCatalog(entries);
    });
    return () => {
      cancelled = true;
    };
  }, [clientSettings, providerSelection]);

  if (!clientSettings || !providerSelection) {
    return (
      <Subsection title="Model & provider">
        <p className="rounded-lg border border-border bg-muted px-5 py-4 text-sm leading-[1.5] text-foreground-muted">
          Model and provider selection is unavailable — the settings provider is
          not mounted.
        </p>
      </Subsection>
    );
  }

  const provider = providerSelection.selectedProvider ?? "";
  const providerOptions = getOrderedProviders(
    catalog.map((entry) => entry.provider),
  ).map((id) => ({ value: id, label: getProviderDisplayName(id) || id }));
  const modelOptions = provider
    ? getModelsForProvider(catalog, provider)
        .filter((model) => !model.hidden)
        .map((model) => ({ value: model.value, label: model.label }))
    : [];

  return (
    <Subsection
      title="Model & provider"
      hint="Applies immediately — the default provider and model for new chats."
    >
      <BoundedSelectField
        label="Default provider"
        ariaLabel="Default provider"
        value={provider}
        options={providerOptions}
        onChange={providerSelection.onSelectProvider}
      />
      <BoundedSelectField
        label="Default model"
        ariaLabel="Default model"
        value={clientSettings.settings.model}
        options={modelOptions}
        onChange={clientSettings.updateModel}
      />
    </Subsection>
  );
}

function FeatureGroup({
  fields,
  spec,
}: {
  fields: SettingsSectionFields;
  spec: FeatureSpec;
}) {
  return (
    <Subsection title={spec.label}>
      <SchemaSelectField
        fields={fields}
        path={`${spec.key}.profile`}
        label="Capability profile"
        ariaLabel={`${spec.label} profile`}
      />
      {spec.hasEnabled ? (
        <SwitchField
          label="Enabled"
          ariaLabel={`${spec.label} enabled`}
          value={fields.getValue(`${spec.key}.enabled`) === true}
          onChange={(value) => fields.setValue(`${spec.key}.enabled`, value)}
        />
      ) : null}
      <StringListConfigField
        fields={fields}
        path={`${spec.key}.candidates`}
        label="Provider/model candidates"
        ariaLabel={`${spec.label} candidates`}
        addLabel="Add candidate"
        placeholder="provider/model (e.g. claude/sonnet)"
      />
      {spec.hasConfidence ? (
        <NumberConfigField
          fields={fields}
          path={`${spec.key}.confidence_threshold`}
          label="Confidence threshold"
          ariaLabel={`${spec.label} confidence threshold`}
          step={0.05}
        />
      ) : null}
      {spec.promptPaths.map((prompt) => (
        <TextConfigField
          key={prompt.suffix}
          fields={fields}
          path={`${spec.key}.${prompt.suffix}`}
          label={prompt.label}
          ariaLabel={`${spec.label} ${prompt.label}`}
          placeholder="features/… (leave blank for the default)"
          nullable
        />
      ))}
    </Subsection>
  );
}

function GenerationEndpointEditor({
  slug,
  value,
  onChange,
  schema,
  catalog,
}: {
  slug: string;
  value: GenerationEndpoint;
  onChange: (next: GenerationEndpoint) => void;
  schema: Record<string, unknown> | null;
  catalog: ProviderModelEntry[];
}) {
  const name = slug || "new endpoint";
  const protocol = value.protocol ?? OPENAI_COMPATIBLE_PROTOCOL;
  const patch = (partial: Partial<GenerationEndpoint>) =>
    onChange(omitVisionExtract({ ...value, ...partial }));
  const wireApiVisible = !protocolForcesChatCompletions(protocol);
  const chips = inputModalityChips(
    inputModalitiesForEndpoint(catalog, slug, value.model),
  );

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-3">
      {chips.length > 0 ? (
        <div className="capability-chips" aria-label={`Capabilities (${name})`}>
          {chips.map((label) => (
            <Chip key={label} className="capability-chip">
              {label}
            </Chip>
          ))}
        </div>
      ) : null}
      <BoundedSelectField
        label="Protocol"
        ariaLabel={`Protocol (${name})`}
        value={protocol}
        options={endpointEnumOptions(schema, slug, "protocol")}
        onChange={(nextProtocol) =>
          patch({
            protocol: nextProtocol,
            wire_api: protocolForcesChatCompletions(nextProtocol)
              ? CHAT_COMPLETIONS_WIRE_API
              : (value.wire_api ?? CHAT_COMPLETIONS_WIRE_API),
          })
        }
      />
      {wireApiVisible ? (
        <BoundedSelectField
          label="Wire API"
          ariaLabel={`Wire API (${name})`}
          value={value.wire_api ?? CHAT_COMPLETIONS_WIRE_API}
          options={endpointEnumOptions(schema, slug, "wire_api")}
          onChange={(wire_api) => patch({ wire_api })}
        />
      ) : null}
      <TextField
        label="API base"
        ariaLabel={`API base (${name})`}
        value={value.api_base ?? ""}
        placeholder="http://localhost:1234"
        onChange={(api_base) => patch({ api_base })}
      />
      <TextField
        label="Model"
        ariaLabel={`Model (${name})`}
        value={value.model ?? ""}
        onChange={(model) => patch({ model })}
      />
      <TextField
        label="API key"
        ariaLabel={`API key (${name})`}
        value={value.api_key ?? ""}
        placeholder="$secret:NAME for encrypted storage"
        onChange={(apiKey) => patch({ api_key: apiKey === "" ? null : apiKey })}
      />
      <SwitchField
        label="Tool chat"
        ariaLabel={`Tool chat (${name})`}
        value={Boolean(value.tool_chat)}
        onChange={(tool_chat) => patch({ tool_chat })}
      />
    </div>
  );
}

function GenerationGroup({ fields }: { fields: SettingsSectionFields }) {
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    void fetchProviderModelCatalog().then((entries) => {
      if (!cancelled) setCatalog(entries);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Subsection
      title="Generation"
      hint="Timeouts and OpenAI-compatible endpoints for daemon text generation."
    >
      {GENERATION_NUMBER_FIELDS.map((field) => (
        <NumberConfigField
          key={field.suffix}
          fields={fields}
          path={`${GENERATION_PREFIX}.${field.suffix}`}
          label={field.label}
          ariaLabel={`Generation ${field.label.toLowerCase()}`}
        />
      ))}
      <KeyValueMapField<GenerationEndpoint>
        label="Generation endpoints"
        ariaLabel="Generation endpoint"
        value={sanitizeEndpointMap(
          asMap<GenerationEndpoint>(fields.getValue(GENERATION_ENDPOINTS_PATH)),
        )}
        keyPlaceholder="endpoint slug"
        addLabel="Add endpoint"
        createValue={() => ({
          protocol: OPENAI_COMPATIBLE_PROTOCOL,
          wire_api: CHAT_COMPLETIONS_WIRE_API,
          api_base: "",
          model: "",
        })}
        onChange={(next) =>
          fields.setValue(GENERATION_ENDPOINTS_PATH, sanitizeEndpointMap(next))
        }
        renderValue={(endpoint, onValueChange, key) => (
          <GenerationEndpointEditor
            slug={key}
            value={endpoint}
            onChange={onValueChange}
            schema={fields.schema}
            catalog={catalog}
          />
        )}
      />
      <KeyValueMapField<string[]>
        label="Profile default candidates"
        ariaLabel="Profile default"
        value={asMap<string[]>(fields.getValue(PROFILE_DEFAULTS_PATH))}
        keyPlaceholder="feature profile (e.g. feature_mid)"
        addLabel="Add profile override"
        createValue={() => []}
        onChange={(next) => fields.setValue(PROFILE_DEFAULTS_PATH, next)}
        renderValue={(candidates, onValueChange, key) => (
          <StringListField
            label=""
            ariaLabel={`${key || "profile"} default candidates`}
            value={candidates}
            addLabel="Add candidate"
            placeholder="provider/model"
            onChange={onValueChange}
          />
        )}
      />
    </Subsection>
  );
}

function ContextWindowGroup({ fields }: { fields: SettingsSectionFields }) {
  // `context_window_overrides.{model_match}` keys are dynamic segments:
  // stored encoded (a model match like "gpt-4.1" contains a dot), edited
  // decoded.
  return (
    <Subsection
      title="Context window overrides"
      hint="Override the context window size for models whose name contains the key."
    >
      <KeyValueRowsField<number>
        label="Overrides"
        ariaLabel="Context window override"
        value={decodeDynamicMapRows(
          asMap<number>(fields.getValue(CONTEXT_WINDOW_PATH)),
        )}
        keyPlaceholder="model substring (e.g. opus)"
        addLabel="Add override"
        createValue={() => 0}
        onChange={(next) =>
          fields.setValue(CONTEXT_WINDOW_PATH, encodeDynamicMapRows(next))
        }
        renderValue={(tokens, onValueChange, key) => (
          <NumberField
            label=""
            ariaLabel={`Context window for ${key || "new entry"}`}
            value={typeof tokens === "number" ? tokens : null}
            min={0}
            onChange={(next) => onValueChange(next ?? 0)}
          />
        )}
      />
    </Subsection>
  );
}

export function ProvidersModelsSection() {
  const { clientSettings, providerSelection } = useSettingsSectionContext();

  return (
    <SettingsSection sectionId="providers-models" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <ModelProviderControls
            clientSettings={clientSettings}
            providerSelection={providerSelection}
          />
          {FEATURE_SPECS.map((spec) => (
            <FeatureGroup key={spec.key} fields={fields} spec={spec} />
          ))}
          <GenerationGroup fields={fields} />
          <ContextWindowGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
