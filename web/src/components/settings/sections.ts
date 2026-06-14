/**
 * Settings overlay information architecture.
 *
 * Section ids are the stable kebab-case ids from the configuration audit
 * (`docs/audits/configuration-audit.md` → "Proposed Overlay IA"). The order
 * here is the canonical nav order. Per-section content arrives in #17046; this
 * shell only renders the nav and a placeholder content pane per section.
 */

export interface SettingsSection {
  /** Stable kebab-case id; also the deep-link anchor for the section. */
  readonly id: string
  /** Nav + content-header label. */
  readonly label: string
  /** One-line purpose, shown under the section header. */
  readonly description: string
}

export const SETTINGS_SECTIONS = [
  {
    id: 'appearance',
    label: 'Appearance',
    description: 'Theme, density, font size, and plan-pending presentation.',
  },
  {
    id: 'providers-models',
    label: 'Providers & Models',
    description:
      'Provider and model selection, local generation endpoints, feature model candidates.',
  },
  {
    id: 'chat-voice',
    label: 'Chat & Voice',
    description: 'Default chat mode, voice preferences, and STT/TTS daemon settings.',
  },
  {
    id: 'projects-sessions',
    label: 'Projects & Sessions',
    description:
      'Project selection, session lifecycle, summaries, and validation evidence detection.',
  },
  {
    id: 'tool-approvals',
    label: 'Tool Approvals',
    description: 'Global auto-allow rules and per-tool approval policy.',
  },
  {
    id: 'secrets-auth',
    label: 'Secrets & Auth',
    description: 'Secrets store, auth credentials, and secret references.',
  },
  {
    id: 'prompts-templates',
    label: 'Prompts & Templates',
    description: 'Prompt overrides, the full YAML template, and import/export.',
  },
  {
    id: 'automation-workflows',
    label: 'Automation & Workflows',
    description: 'Tasks, workflows, cron, pipelines, tmux automation, and variables.',
  },
  {
    id: 'mcp-tools',
    label: 'MCP & Tools',
    description: 'MCP proxy, tool search and recommendation, and skills hub configuration.',
  },
  {
    id: 'memory-knowledge',
    label: 'Memory & Knowledge',
    description: 'Memory, embeddings, Qdrant/FalkorDB, and wiki/codewiki watchers.',
  },
  {
    id: 'observability',
    label: 'Observability',
    description: 'Telemetry, logs, metrics, tracing, and exporters.',
  },
  {
    id: 'integrations-hooks',
    label: 'Integrations & Hooks',
    description: 'Communications channels, webhooks, and hook broadcasts.',
  },
  {
    id: 'runtime-infrastructure',
    label: 'Runtime & Infrastructure',
    description:
      'Daemon ports, CORS, directories, UI serving, code index, search, and freshness.',
  },
] as const satisfies readonly SettingsSection[]

export type SettingsSectionId = (typeof SETTINGS_SECTIONS)[number]['id']

export const DEFAULT_SETTINGS_SECTION: SettingsSectionId = 'appearance'

export function getSettingsSection(id: SettingsSectionId): SettingsSection {
  const section = SETTINGS_SECTIONS.find((entry) => entry.id === id)
  // The id type is the union of section ids, so this is always defined; the
  // fallback keeps the function total for forward-compat ids.
  return section ?? SETTINGS_SECTIONS[0]
}
