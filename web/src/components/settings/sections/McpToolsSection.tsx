import { useEffect, useState } from 'react'

import { TextField } from '../../activity/fields'
import { Button } from '../../ui/Button'
import { Input } from '../../ui/Input'
import { BoundedSelectField } from '../fields'
import { SettingsSection, type SettingsSectionFields } from './SettingsSection'
import {
  NumberConfigField,
  NumberMapConfigField,
  SchemaSelectField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from './configFields'
import {
  asString,
  decodeDynamicMapRows,
  encodeDynamicMapRows,
  hasBlankDynamicMapKey,
  type DynamicMapRow,
} from './configAccessors'

/**
 * MCP & Tools settings section: the MCP client proxy (connection timeouts and
 * tool search/recommendation) and skills-hub configuration. These are the
 * `mcp_client_proxy.*` and `skills.*` keep-rows from the configuration audit,
 * with the two former text-fallback map rows (`tool_timeouts`, `hubs`) fixed
 * into typed editors. Every row is a draft-backed DaemonConfig dotted-path,
 * saved through the section shell's Save/Discard footer.
 */

const PROXY_PATHS = [
  'mcp_client_proxy.enabled',
  'mcp_client_proxy.connect_timeout',
  'mcp_client_proxy.proxy_timeout',
  'mcp_client_proxy.tool_timeout',
  'mcp_client_proxy.tool_timeouts',
  'mcp_client_proxy.search_mode',
  'mcp_client_proxy.min_similarity',
  'mcp_client_proxy.top_k',
  'mcp_client_proxy.refresh_on_server_add',
  'mcp_client_proxy.refresh_timeout',
] as const

const SKILLS_PATHS = [
  'skills.inject_core_skills',
  'skills.core_skills_path',
  'skills.injection_format',
  'skills.hubs',
] as const

const OWNED_PATHS: readonly string[] = [...PROXY_PATHS, ...SKILLS_PATHS]

/** Hub `type` enum from `HubConfig`. Stable, so listed explicitly here. */
const HUB_TYPE_OPTIONS = [
  { value: 'clawdhub', label: 'ClawdHub' },
  { value: 'skillsmp', label: 'SkillsMP' },
  { value: 'github-collection', label: 'GitHub collection' },
  { value: 'claude-plugins', label: 'Claude plugins' },
]

const REQUIRED_HUB_KEY_ERROR = 'Hub key is required before saving'

/**
 * One configured skill hub. `type` is required; the rest are optional and clear
 * back to `null` when emptied so the daemon falls back to its hub-type default.
 */
interface HubConfig {
  type: string
  base_url?: string | null
  repo?: string | null
  branch?: string | null
  path?: string | null
  auth_key_name?: string | null
}

function McpProxyGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="MCP proxy"
      hint="Connection timeouts and tool search behavior for downstream MCP servers."
    >
      <SwitchConfigField
        fields={fields}
        path="mcp_client_proxy.enabled"
        label="Enable MCP proxy"
        ariaLabel="Enable MCP proxy"
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.connect_timeout"
        label="Connect timeout (seconds)"
        ariaLabel="MCP connect timeout"
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.proxy_timeout"
        label="Proxy call timeout (seconds)"
        ariaLabel="MCP proxy call timeout"
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.tool_timeout"
        label="Tool schema timeout (seconds)"
        ariaLabel="Tool schema timeout"
      />
      <NumberMapConfigField
        fields={fields}
        path="mcp_client_proxy.tool_timeouts"
        label="Per-tool timeouts (seconds)"
        ariaLabel="Per-tool timeout"
        keyPlaceholder="tool name"
        addLabel="Add tool timeout"
      />
      <SchemaSelectField
        fields={fields}
        path="mcp_client_proxy.search_mode"
        label="Tool search mode"
        ariaLabel="Tool search mode"
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.min_similarity"
        label="Minimum semantic similarity"
        ariaLabel="Minimum semantic similarity"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.top_k"
        label="Semantic result count"
        ariaLabel="Semantic result count"
      />
      <SwitchConfigField
        fields={fields}
        path="mcp_client_proxy.refresh_on_server_add"
        label="Refresh tool embeddings when a server is added"
        ariaLabel="Refresh embeddings on server add"
      />
      <NumberConfigField
        fields={fields}
        path="mcp_client_proxy.refresh_timeout"
        label="Tool refresh timeout (seconds)"
        ariaLabel="Tool refresh timeout"
      />
    </Subsection>
  )
}

/** The typed editor for one `skills.hubs` entry (a `HubConfig` object). */
function HubEntryFields({
  hub,
  hubKey,
  onChange,
}: {
  hub: HubConfig
  hubKey: string
  onChange: (next: HubConfig) => void
}) {
  const name = hubKey || 'new hub'
  return (
    <div className="flex flex-col gap-2.5">
      <BoundedSelectField
        label="Type"
        ariaLabel={`${name} hub type`}
        value={asString(hub.type)}
        options={HUB_TYPE_OPTIONS}
        onChange={(value) => onChange({ ...hub, type: value })}
      />
      <TextField
        label="Base URL"
        ariaLabel={`${name} hub base URL`}
        value={asString(hub.base_url)}
        placeholder="https://hub.example"
        onChange={(value) => onChange({ ...hub, base_url: value || null })}
      />
      <TextField
        label="Repository"
        ariaLabel={`${name} hub repository`}
        value={asString(hub.repo)}
        placeholder="owner/repo"
        onChange={(value) => onChange({ ...hub, repo: value || null })}
      />
      <TextField
        label="Branch"
        ariaLabel={`${name} hub branch`}
        value={asString(hub.branch)}
        onChange={(value) => onChange({ ...hub, branch: value || null })}
      />
      <TextField
        label="Path"
        ariaLabel={`${name} hub path`}
        value={asString(hub.path)}
        placeholder="skills/"
        onChange={(value) => onChange({ ...hub, path: value || null })}
      />
      <TextField
        label="Auth key name"
        ariaLabel={`${name} hub auth key name`}
        value={asString(hub.auth_key_name)}
        placeholder="Secret name in the secret store"
        onChange={(value) => onChange({ ...hub, auth_key_name: value || null })}
      />
    </div>
  )
}

/** Coerce the stored `skills.hubs` value into a `Record<string, HubConfig>`. */
function asHubMap(value: unknown): Record<string, HubConfig> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const out: Record<string, HubConfig> = {}
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    if (entry && typeof entry === 'object' && !Array.isArray(entry)) {
      const hub = entry as Record<string, unknown>
      out[key] = { ...hub, type: asString(hub.type) } as HubConfig
    } else {
      out[key] = { type: '' }
    }
  }
  return out
}

/**
 * The `skills.hubs` editor: a `dict[str, HubConfig]` of named hubs. Hand-rolled
 * rather than a generic map field because each value is a multi-field object,
 * so it needs the per-entry `HubEntryFields` sub-form plus its own key input
 * and remove control. Writes the whole map back through the draft. Hub names
 * are `skills.hubs.{hub}` dynamic segments: stored encoded, edited decoded.
 */
function SkillHubsField({
  fields,
  onValidityChange,
}: {
  fields: SettingsSectionFields
  onValidityChange: (isValid: boolean) => void
}) {
  const entries = decodeDynamicMapRows(asHubMap(fields.getValue('skills.hubs')))
  const [keyError, setKeyError] = useState<string | null>(null)
  const hasBlankKey = hasBlankDynamicMapKey(entries)
  const visibleKeyError = !hasBlankKey && keyError === REQUIRED_HUB_KEY_ERROR
    ? null
    : keyError

  useEffect(() => {
    onValidityChange(!hasBlankKey)
  }, [hasBlankKey, onValidityChange])

  function commit(next: DynamicMapRow<HubConfig>[]) {
    const nextHasBlankKey = hasBlankDynamicMapKey(next)
    setKeyError(nextHasBlankKey ? REQUIRED_HUB_KEY_ERROR : null)
    onValidityChange(!nextHasBlankKey)
    fields.setValue('skills.hubs', encodeDynamicMapRows(next))
  }

  function updateKey(index: number, nextKey: string) {
    const isDuplicate =
      nextKey !== '' &&
      entries.some((entry, i) => i !== index && entry.displayKey === nextKey)
    if (isDuplicate) {
      setKeyError(`Hub key "${nextKey}" already exists`)
      onValidityChange(!hasBlankDynamicMapKey(entries))
      console.warn(
        `SkillHubsField: ignored rename to duplicate hub "${nextKey}" to avoid overwriting an existing entry`,
      )
      return
    }
    commit(entries.map((entry, i) => (i === index ? { ...entry, displayKey: nextKey } : entry)))
  }

  function updateHub(index: number, nextHub: HubConfig) {
    commit(entries.map((entry, i) => (i === index ? { ...entry, value: nextHub } : entry)))
  }

  function removeEntry(index: number) {
    commit(entries.filter((_, i) => i !== index))
  }

  function addEntry() {
    commit([...entries, { storedKey: '', displayKey: '', value: { type: 'clawdhub' } }])
  }

  return (
    <div className="flex flex-col gap-2" role="group">
      <span className="text-base font-medium leading-[1.3] text-foreground">
        Skill hubs
      </span>
      <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
        External hubs searched by the skills hub, keyed by hub name.
      </p>
      {visibleKeyError && (
        <p
          className="max-w-[48ch] text-sm leading-[1.4] text-destructive"
          role="alert"
        >
          {visibleKeyError}
        </p>
      )}
      {entries.length > 0 ? (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {entries.map((entry, index) => (
            <li
              key={index}
              className="flex flex-col gap-3 rounded-lg border border-border bg-surface-secondary p-3.5"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  type="text"
                  wrapperClassName="flex-1"
                  className="min-w-0 flex-[1_1_10rem] text-foreground [font-family:inherit] pointer-coarse:min-h-11"
                  value={entry.displayKey}
                  placeholder="hub-name"
                  aria-label={`Skill hub key ${index + 1}`}
                  onChange={(event) => updateKey(index, event.target.value)}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={
                    entry.displayKey
                      ? `Remove ${entry.displayKey}`
                      : `Remove skill hub ${index + 1}`
                  }
                  onClick={() => removeEntry(index)}
                >
                  Remove
                </Button>
              </div>
              <HubEntryFields
                hub={entry.value}
                hubKey={entry.displayKey}
                onChange={(next) => updateHub(index, next)}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm leading-[1.4] text-foreground-muted">
          No skill hubs.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          onClick={addEntry}
        >
          Add skill hub
        </Button>
      </div>
    </div>
  )
}

function SkillsGroup({
  fields,
  onHubsValidityChange,
}: {
  fields: SettingsSectionFields
  onHubsValidityChange: (isValid: boolean) => void
}) {
  return (
    <Subsection
      title="Skills hub"
      hint="How skills are advertised in session context and where they are fetched from."
    >
      <SwitchConfigField
        fields={fields}
        path="skills.inject_core_skills"
        label="Advertise core skills in session context"
        ariaLabel="Advertise core skills"
      />
      <TextConfigField
        fields={fields}
        path="skills.core_skills_path"
        label="Core skills path"
        ariaLabel="Core skills path"
        placeholder="install/shared/skills/"
        nullable
      />
      <SchemaSelectField
        fields={fields}
        path="skills.injection_format"
        label="Skill manifest format"
        ariaLabel="Skill manifest format"
      />
      <SkillHubsField fields={fields} onValidityChange={onHubsValidityChange} />
    </Subsection>
  )
}

export function McpToolsSection() {
  const [hubsValid, setHubsValid] = useState(true)
  return (
    <SettingsSection
      sectionId="mcp-tools"
      ownedPaths={OWNED_PATHS}
      saveDisabled={!hubsValid}
    >
      {(fields) => (
        <>
          <McpProxyGroup fields={fields} />
          <SkillsGroup fields={fields} onHubsValidityChange={setHubsValid} />
        </>
      )}
    </SettingsSection>
  )
}
