import { TextField } from '../../activity/fields'
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
import { asString } from './configAccessors'

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
    <div className="settings-hub-fields">
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
 * and remove control. Writes the whole map back through the draft.
 */
function SkillHubsField({ fields }: { fields: SettingsSectionFields }) {
  const hubs = asHubMap(fields.getValue('skills.hubs'))
  const entries = Object.entries(hubs)

  function commit(next: Record<string, HubConfig>) {
    fields.setValue('skills.hubs', next)
  }

  function updateKey(index: number, nextKey: string) {
    commit(
      Object.fromEntries(
        entries.map((entry, i) => (i === index ? [nextKey, entry[1]] : entry)),
      ),
    )
  }

  function updateHub(index: number, nextHub: HubConfig) {
    commit(
      Object.fromEntries(
        entries.map((entry, i) => (i === index ? [entry[0], nextHub] : entry)),
      ),
    )
  }

  function removeEntry(index: number) {
    commit(Object.fromEntries(entries.filter((_, i) => i !== index)))
  }

  function addEntry() {
    commit({ ...hubs, '': { type: 'clawdhub' } })
  }

  return (
    <div className="settings-field settings-hubs-field" role="group">
      <span className="settings-field__label">Skill hubs</span>
      <p className="settings-field__hint">
        External hubs searched by the skills hub, keyed by hub name.
      </p>
      {entries.length > 0 ? (
        <ul className="settings-hubs-field__items">
          {entries.map(([key, hub], index) => (
            <li key={index} className="settings-hubs-field__item">
              <div className="settings-hubs-field__head">
                <input
                  type="text"
                  className="settings-field__input"
                  value={key}
                  placeholder="hub-name"
                  aria-label={`Skill hub key ${index + 1}`}
                  onChange={(event) => updateKey(index, event.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  aria-label={
                    key ? `Remove ${key}` : `Remove skill hub ${index + 1}`
                  }
                  onClick={() => removeEntry(index)}
                >
                  Remove
                </button>
              </div>
              <HubEntryFields
                hub={hub}
                hubKey={key}
                onChange={(next) => updateHub(index, next)}
              />
            </li>
          ))}
        </ul>
      ) : (
        <p className="settings-field__empty">No skill hubs.</p>
      )}
      <div className="settings-field__actions">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          onClick={addEntry}
        >
          Add skill hub
        </button>
      </div>
    </div>
  )
}

function SkillsGroup({ fields }: { fields: SettingsSectionFields }) {
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
      <SkillHubsField fields={fields} />
    </Subsection>
  )
}

export function McpToolsSection() {
  return (
    <SettingsSection sectionId="mcp-tools" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <McpProxyGroup fields={fields} />
          <SkillsGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  )
}
