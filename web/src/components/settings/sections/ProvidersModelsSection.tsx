import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { NumberField, SwitchField, TextField } from '../../activity/fields'
import { BoundedSelectField, KeyValueMapField, StringListField } from '../fields'
import {
  fetchProviderModelCatalog,
  getModelsForProvider,
  getOrderedProviders,
  getProviderDisplayName,
  type ProviderModelEntry,
} from '../../../lib/providerModels'
import type { UseSettingsReturn } from '../../../hooks/useSettings'
import { enumOptionsAt, numberBoundsAt } from '../configSchema'
import { SettingsSection, type SettingsSectionFields } from './SettingsSection'
import {
  useSettingsSectionContext,
  type ProviderSelectionContextValue,
} from './SettingsSectionContext'

// One LLM feature's owned config rows. Each feature exposes a capability
// profile (`FeatureProfile` enum) and an ordered candidate list; some also
// carry an `enabled` toggle, prompt-path overrides, or a confidence threshold.
interface FeatureSpec {
  key: string
  label: string
  hasEnabled: boolean
  hasConfidence?: boolean
  promptPaths: { suffix: string; label: string }[]
}

const FEATURE_SPECS: readonly FeatureSpec[] = [
  {
    key: 'recommend_tools',
    label: 'Tool recommendation',
    hasEnabled: true,
    promptPaths: [
      { suffix: 'prompt_path', label: 'System prompt path' },
      { suffix: 'hybrid_rerank_prompt_path', label: 'Hybrid re-rank prompt path' },
      { suffix: 'llm_prompt_path', label: 'LLM prompt path' },
    ],
  },
  {
    key: 'tool_summarizer',
    label: 'Tool summarizer',
    hasEnabled: true,
    promptPaths: [
      { suffix: 'prompt_path', label: 'Prompt path' },
      { suffix: 'system_prompt_path', label: 'System prompt path' },
      { suffix: 'server_description_prompt_path', label: 'Server description prompt path' },
      {
        suffix: 'server_description_system_prompt_path',
        label: 'Server description system prompt path',
      },
    ],
  },
  {
    key: 'import_mcp_server',
    label: 'MCP server import',
    hasEnabled: true,
    promptPaths: [
      { suffix: 'prompt_path', label: 'Prompt path' },
      { suffix: 'github_fetch_prompt_path', label: 'GitHub fetch prompt path' },
      { suffix: 'search_fetch_prompt_path', label: 'Search fetch prompt path' },
    ],
  },
  {
    key: 'project_verification_synthesis',
    label: 'Verification synthesis',
    hasEnabled: false,
    hasConfidence: true,
    promptPaths: [],
  },
  { key: 'merge_resolution', label: 'Merge resolution', hasEnabled: false, promptPaths: [] },
  { key: 'skill_description', label: 'Skill description', hasEnabled: false, promptPaths: [] },
]

const GENERATION_NUMBER_FIELDS: readonly { suffix: string; label: string }[] = [
  { suffix: 'timeout_seconds', label: 'Timeout (seconds)' },
  { suffix: 'candidate_timeout_seconds', label: 'Candidate timeout (seconds)' },
  { suffix: 'cli_candidate_timeout_seconds', label: 'CLI candidate timeout (seconds)' },
]

const GENERATION_PREFIX = 'ai.generation'
const LOCAL_ENDPOINTS_PATH = `${GENERATION_PREFIX}.local.endpoints`
const PROFILE_DEFAULTS_PATH = `${GENERATION_PREFIX}.profile_defaults`
const CONTEXT_WINDOW_PATH = 'context_window_overrides'

// `LocalGenerationEndpointConfig.provider` enum from the daemon schema.
const LOCAL_PROVIDER_OPTIONS = [
  { value: 'openai-compatible', label: 'OpenAI-compatible' },
  { value: 'lmstudio', label: 'LM Studio' },
  { value: 'ollama', label: 'Ollama' },
]

interface LocalEndpoint {
  provider?: string
  api_base?: string
  model?: string
  api_key?: string | null
  vision_extract?: boolean
}

function featurePromptPaths(spec: FeatureSpec): string[] {
  return spec.promptPaths.map((prompt) => `${spec.key}.${prompt.suffix}`)
}

function featureOwnedPaths(spec: FeatureSpec): string[] {
  return [
    `${spec.key}.profile`,
    `${spec.key}.candidates`,
    ...(spec.hasEnabled ? [`${spec.key}.enabled`] : []),
    ...(spec.hasConfidence ? [`${spec.key}.confidence_threshold`] : []),
    ...featurePromptPaths(spec),
  ]
}

// The exact set of dotted config paths this section owns (and the draft is
// scoped to). Derived from the specs so the draft can never drift from what the
// section renders.
const OWNED_PATHS: readonly string[] = [
  ...FEATURE_SPECS.flatMap(featureOwnedPaths),
  ...GENERATION_NUMBER_FIELDS.map((field) => `${GENERATION_PREFIX}.${field.suffix}`),
  LOCAL_ENDPOINTS_PATH,
  PROFILE_DEFAULTS_PATH,
  CONTEXT_WINDOW_PATH,
]

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' ? value : null
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function asMap<V>(value: unknown): Record<string, V> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, V>)
    : {}
}

function Subsection({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="settings-subsection">
      <div className="settings-subsection__head">
        <h4 className="settings-subsection__title">{title}</h4>
        {hint ? <p className="settings-field__hint">{hint}</p> : null}
      </div>
      {children}
    </section>
  )
}

function ModelProviderControls({
  clientSettings,
  providerSelection,
}: {
  clientSettings?: UseSettingsReturn
  providerSelection?: ProviderSelectionContextValue
}) {
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([])

  useEffect(() => {
    let cancelled = false
    void fetchProviderModelCatalog().then((entries) => {
      if (!cancelled) setCatalog(entries)
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (!clientSettings || !providerSelection) {
    return (
      <Subsection title="Model & provider">
        <p className="settings-section__pending">
          Model and provider selection is unavailable — the settings provider is not mounted.
        </p>
      </Subsection>
    )
  }

  const provider = providerSelection.selectedProvider ?? ''
  const providerOptions = getOrderedProviders(catalog.map((entry) => entry.provider)).map(
    (id) => ({ value: id, label: getProviderDisplayName(id) || id }),
  )
  const modelOptions = provider
    ? getModelsForProvider(catalog, provider)
        .filter((model) => !model.hidden)
        .map((model) => ({ value: model.value, label: model.label }))
    : []

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
  )
}

function ProfileSelect({
  fields,
  path,
  ariaLabel,
}: {
  fields: SettingsSectionFields
  path: string
  ariaLabel: string
}) {
  return (
    <BoundedSelectField
      label="Capability profile"
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      options={enumOptionsAt(fields.schema, path)}
      onChange={(value) => fields.setValue(path, value)}
    />
  )
}

function CandidatesField({
  fields,
  path,
  ariaLabel,
}: {
  fields: SettingsSectionFields
  path: string
  ariaLabel: string
}) {
  return (
    <StringListField
      label="Provider/model candidates"
      ariaLabel={ariaLabel}
      value={asStringList(fields.getValue(path))}
      addLabel="Add candidate"
      placeholder="provider/model (e.g. claude/sonnet)"
      onChange={(value) => fields.setValue(path, value)}
    />
  )
}

function NumberConfigField({
  fields,
  path,
  label,
  ariaLabel,
  step,
}: {
  fields: SettingsSectionFields
  path: string
  label: string
  ariaLabel: string
  step?: number
}) {
  const bounds = numberBoundsAt(fields.schema, path)
  return (
    <NumberField
      label={label}
      ariaLabel={ariaLabel}
      value={asNumber(fields.getValue(path))}
      min={bounds.min}
      max={bounds.max}
      step={step}
      onChange={(value) => fields.setValue(path, value)}
    />
  )
}

function PromptPathField({
  fields,
  path,
  label,
  ariaLabel,
}: {
  fields: SettingsSectionFields
  path: string
  label: string
  ariaLabel: string
}) {
  return (
    <TextField
      label={label}
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      placeholder="features/… (leave blank for the default)"
      onChange={(value) => fields.setValue(path, value === '' ? null : value)}
    />
  )
}

function FeatureGroup({ fields, spec }: { fields: SettingsSectionFields; spec: FeatureSpec }) {
  return (
    <Subsection title={spec.label}>
      <ProfileSelect
        fields={fields}
        path={`${spec.key}.profile`}
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
      <CandidatesField
        fields={fields}
        path={`${spec.key}.candidates`}
        ariaLabel={`${spec.label} candidates`}
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
        <PromptPathField
          key={prompt.suffix}
          fields={fields}
          path={`${spec.key}.${prompt.suffix}`}
          label={prompt.label}
          ariaLabel={`${spec.label} ${prompt.label}`}
        />
      ))}
    </Subsection>
  )
}

function LocalEndpointEditor({
  slug,
  value,
  onChange,
}: {
  slug: string
  value: LocalEndpoint
  onChange: (next: LocalEndpoint) => void
}) {
  const name = slug || 'new endpoint'
  const patch = (partial: Partial<LocalEndpoint>) => onChange({ ...value, ...partial })

  return (
    <div className="settings-endpoint-editor">
      <BoundedSelectField
        label="Provider"
        ariaLabel={`Provider (${name})`}
        value={value.provider ?? 'openai-compatible'}
        options={LOCAL_PROVIDER_OPTIONS}
        onChange={(provider) => patch({ provider })}
      />
      <TextField
        label="API base"
        ariaLabel={`API base (${name})`}
        value={value.api_base ?? ''}
        placeholder="http://localhost:1234"
        onChange={(api_base) => patch({ api_base })}
      />
      <TextField
        label="Model"
        ariaLabel={`Model (${name})`}
        value={value.model ?? ''}
        onChange={(model) => patch({ model })}
      />
      <TextField
        label="API key"
        ariaLabel={`API key (${name})`}
        value={value.api_key ?? ''}
        placeholder="$secret:NAME for encrypted storage"
        onChange={(apiKey) => patch({ api_key: apiKey === '' ? null : apiKey })}
      />
      <SwitchField
        label="Vision extract"
        ariaLabel={`Vision extract (${name})`}
        value={Boolean(value.vision_extract)}
        onChange={(vision_extract) => patch({ vision_extract })}
      />
    </div>
  )
}

function GenerationGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Generation"
      hint="Timeouts and local OpenAI-compatible endpoints for daemon text generation."
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
      <KeyValueMapField<LocalEndpoint>
        label="Local endpoints"
        ariaLabel="Local endpoint"
        value={asMap<LocalEndpoint>(fields.getValue(LOCAL_ENDPOINTS_PATH))}
        keyPlaceholder="endpoint slug"
        addLabel="Add endpoint"
        createValue={() => ({ provider: 'openai-compatible', api_base: '', model: '' })}
        onChange={(next) => fields.setValue(LOCAL_ENDPOINTS_PATH, next)}
        renderValue={(endpoint, onValueChange, key) => (
          <LocalEndpointEditor slug={key} value={endpoint} onChange={onValueChange} />
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
            ariaLabel={`${key || 'profile'} default candidates`}
            value={candidates}
            addLabel="Add candidate"
            placeholder="provider/model"
            onChange={onValueChange}
          />
        )}
      />
    </Subsection>
  )
}

function ContextWindowGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Context window overrides"
      hint="Override the context window size for models whose name contains the key."
    >
      <KeyValueMapField<number>
        label="Overrides"
        ariaLabel="Context window override"
        value={asMap<number>(fields.getValue(CONTEXT_WINDOW_PATH))}
        keyPlaceholder="model substring (e.g. opus)"
        addLabel="Add override"
        createValue={() => 0}
        onChange={(next) => fields.setValue(CONTEXT_WINDOW_PATH, next)}
        renderValue={(tokens, onValueChange, key) => (
          <NumberField
            label=""
            ariaLabel={`Context window for ${key || 'new entry'}`}
            value={typeof tokens === 'number' ? tokens : null}
            min={0}
            onChange={(next) => onValueChange(next ?? 0)}
          />
        )}
      />
    </Subsection>
  )
}

export function ProvidersModelsSection() {
  const { clientSettings, providerSelection } = useSettingsSectionContext()

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
  )
}
