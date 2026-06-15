import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { createElement } from 'react'
import { render } from '@testing-library/react'
import { beforeAll, describe, it, expect, vi } from 'vitest'
import { SETTINGS_SECTIONS, type SettingsSectionId } from '../../sections'
// vitest hoists the vi.mock calls below above these imports, so the sections
// load against the stubbed shell.
import { AppearanceSection } from '../AppearanceSection'
import { ProvidersModelsSection } from '../ProvidersModelsSection'
import { ChatVoiceSection } from '../ChatVoiceSection'
import { ProjectsSessionsSection } from '../ProjectsSessionsSection'
import { ToolApprovalsSection } from '../ToolApprovalsSection'
import { SecretsAuthSection } from '../SecretsAuthSection'
import { PromptsTemplatesSection } from '../PromptsTemplatesSection'
import { AutomationWorkflowsSection } from '../AutomationWorkflowsSection'
import { McpToolsSection } from '../McpToolsSection'
import { MemoryKnowledgeSection } from '../MemoryKnowledgeSection'
import { ObservabilitySection } from '../ObservabilitySection'
import { IntegrationsHooksSection } from '../IntegrationsHooksSection'
import { RuntimeInfrastructureSection } from '../RuntimeInfrastructureSection'

/**
 * Final-verification coverage gate for the settings overlay (plan 13.2.1).
 *
 * The configuration audit (`docs/audits/configuration-audit.md`) is the source
 * of truth for which backend config rows the overlay must expose and in which
 * section. This test renders every section with a stubbed `SettingsSection` to
 * capture the exact `ownedPaths` each section registers, then asserts against
 * the audit:
 *
 * - every DaemonConfig-backed keep/fix row is editable in exactly one section
 *   (no orphan keep-rows, no duplicates),
 * - each owned path lives in the section the audit assigns it to,
 * - sections never own overlapping path subtrees.
 *
 * Capturing the real `ownedPaths` prop (rather than a parallel registry) means
 * the gate cannot drift from what the sections actually wire up.
 *
 * Scope note: only rows whose backend source is the `DaemonConfig` schema flow
 * through the dotted-path config draft that `ownedPaths` governs. The audit's
 * `ui_settings.*`, `secrets.store.*`, prompt/template, import/export, approvals,
 * variables, and `rules.*` keep-rows reach their sections through dedicated
 * endpoints/clients, not the config draft, so they are intentionally outside
 * this union (their target sections own zero draft paths for those rows).
 */

const { capturedOwnedPaths } = vi.hoisted(() => ({
  capturedOwnedPaths: new Map<string, readonly string[]>(),
}))

// Stub the section shell so rendering a section records the paths it owns
// without mounting the field tree (the render-prop children are never invoked).
vi.mock('../SettingsSection', () => ({
  SettingsSection: ({
    sectionId,
    ownedPaths,
  }: {
    sectionId: string
    ownedPaths: readonly string[]
  }) => {
    capturedOwnedPaths.set(sectionId, ownedPaths)
    return null
  },
}))

// Sections destructure this at the top before returning the (stubbed) shell;
// the values are only consumed inside the un-rendered children, so an empty
// context is sufficient and avoids standing up every provider.
vi.mock('../SettingsSectionContext', () => ({
  useSettingsSectionContext: () => ({}),
}))

const SECTION_COMPONENTS = [
  AppearanceSection,
  ProvidersModelsSection,
  ChatVoiceSection,
  ProjectsSessionsSection,
  ToolApprovalsSection,
  SecretsAuthSection,
  PromptsTemplatesSection,
  AutomationWorkflowsSection,
  McpToolsSection,
  MemoryKnowledgeSection,
  ObservabilitySection,
  IntegrationsHooksSection,
  RuntimeInfrastructureSection,
]

const SECTION_IDS = new Set<string>(SETTINGS_SECTIONS.map((s) => s.id))

const AUDIT_REL = 'docs/audits/configuration-audit.md'

/**
 * Resolve the repo-root audit file by walking up from the working directory.
 * Robust to vitest running with cwd at `web/` (`cd web && vitest`) or at the
 * repo root (`npm --prefix web run test`).
 */
function resolveAuditPath(): string {
  let dir = process.cwd()
  for (;;) {
    const candidate = resolve(dir, AUDIT_REL)
    if (existsSync(candidate)) return candidate
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  throw new Error(`Could not locate ${AUDIT_REL} from cwd ${process.cwd()}`)
}

/**
 * Extract the audit's Full-Matrix rows that are backed by the DaemonConfig
 * schema and assigned to a real overlay section. Returns a path -> sectionId
 * map. Markdown tables with a different shape (inventory tables, the follow-up
 * fix/drop tables) never match because their backend cell does not begin with
 * `DaemonConfig schema`.
 */
function parseAuditConfigKeepRows(markdown: string): Map<string, string> {
  const rows = new Map<string, string>()
  for (const line of markdown.split('\n')) {
    if (!line.startsWith('|')) continue
    const cells = line.split('|').map((cell) => cell.trim())
    // Leading + 7 data cells + trailing => at least 9 entries for matrix rows.
    if (cells.length < 9) continue
    const option = cells[1]
    const backend = cells[2]
    const section = cells[6]
    if (!backend.startsWith('DaemonConfig schema')) continue
    if (!SECTION_IDS.has(section)) continue
    rows.set(option, section)
  }
  return rows
}

const expectedByPath = parseAuditConfigKeepRows(
  readFileSync(resolveAuditPath(), 'utf8'),
)

/**
 * The config draft is dotted-path addressable: owning an object path edits the
 * whole subtree, so owning `validation_detection` makes every
 * `validation_detection.*` leaf editable. A leaf is therefore covered by an
 * owned path when they are equal or the owned path is a strict ancestor.
 */
function covers(ownedPath: string, leaf: string): boolean {
  return leaf === ownedPath || leaf.startsWith(`${ownedPath}.`)
}

function ownedEntries(): Array<[SettingsSectionId, readonly string[]]> {
  return [...capturedOwnedPaths.entries()] as Array<
    [SettingsSectionId, readonly string[]]
  >
}

beforeAll(() => {
  for (const Section of SECTION_COMPONENTS) {
    render(createElement(Section))
  }
})

describe('settings overlay section coverage', () => {
  it('parses a non-trivial DaemonConfig keep-row set from the audit', () => {
    // Guards against a silently-empty parse (e.g. moved audit file or reshaped
    // table) that would make every coverage assertion vacuously pass.
    expect(expectedByPath.size).toBeGreaterThan(300)
    expect(expectedByPath.get('daemon_port')).toBe('runtime-infrastructure')
    expect(expectedByPath.get('memory.enabled')).toBe('memory-knowledge')
    expect(expectedByPath.get('cors_origins')).toBe('runtime-infrastructure')
  })

  it('captures owned paths for every section id', () => {
    for (const section of SETTINGS_SECTIONS) {
      expect(capturedOwnedPaths.has(section.id)).toBe(true)
    }
    expect(capturedOwnedPaths.size).toBe(SETTINGS_SECTIONS.length)
  })

  it('owns no duplicate path within a single section', () => {
    const dupes: string[] = []
    for (const [sectionId, paths] of ownedEntries()) {
      const seen = new Set<string>()
      for (const path of paths) {
        if (seen.has(path)) dupes.push(`${sectionId}: ${path}`)
        seen.add(path)
      }
    }
    expect(dupes).toEqual([])
  })

  it('owns no path that overlaps another section (disjoint, ancestor-aware)', () => {
    const entries = ownedEntries()
    const overlaps: string[] = []
    for (let i = 0; i < entries.length; i += 1) {
      for (let j = i + 1; j < entries.length; j += 1) {
        const [sectionA, pathsA] = entries[i]
        const [sectionB, pathsB] = entries[j]
        for (const a of pathsA) {
          for (const b of pathsB) {
            if (covers(a, b) || covers(b, a)) {
              overlaps.push(`${sectionA}:${a} <-> ${sectionB}:${b}`)
            }
          }
        }
      }
    }
    expect(overlaps).toEqual([])
  })

  it('makes every audit keep-row editable in exactly its assigned section', () => {
    // The core contract (13.2.1): every DaemonConfig keep-row is editable in
    // exactly one section, and that section is the one the audit assigns.
    // Ancestor-aware so an object-subtree editor (e.g. validation_detection)
    // covers its leaf rows. Sections may additionally own config fields added
    // after the audit's frozen draft; those are checked for disjointness above
    // and by each section's own unit test, not against this audit snapshot.
    const violations: string[] = []
    for (const [leaf, expectedSection] of expectedByPath) {
      const covering = ownedEntries()
        .filter(([, paths]) => paths.some((path) => covers(path, leaf)))
        .map(([sectionId]) => sectionId)
      if (covering.length !== 1 || covering[0] !== expectedSection) {
        violations.push(
          `${leaf}: expected [${expectedSection}], covered by [${
            covering.join(', ') || 'none'
          }]`,
        )
      }
    }
    expect(violations).toEqual([])
  })
})
