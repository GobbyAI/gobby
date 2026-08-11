import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { compile } from '@tailwindcss/node'
import { describe, expect, it } from 'vitest'
import {
  BTN_CLASS_ALLOWLIST,
  CLS_CONSTANT_ALLOWLIST,
  CSS_FILE_ALLOWLIST,
  CSS_TOTAL_LINE_PIN,
  IMPORTANT_ALLOWLIST,
  RAW_ELEMENT_ALLOWLIST,
  type RawElement,
} from './styleRatchet.allowlist'

// Style-debt ratchet: legacy idioms may only shrink, never grow. Each check
// compares a fresh scan against the recorded allowlist and fails in both
// directions — above the ceiling (new debt) and below it (stale allowlist
// entry that must be tightened). See docs/guides/frontend-style-guide.md.

const STYLE_GUIDE = 'docs/guides/frontend-style-guide.md'
const ALLOWLIST = 'src/__tests__/styleRatchet.allowlist.ts'
const SKIP_DIRS = new Set(['__tests__', '__visual__', '__fixtures__', '__mocks__', 'test'])
const UI_PRIMITIVES_DIR = 'src/components/ui/'
const ALLOWLIST_REPO_PATH = `web/${ALLOWLIST}`
const AGENT_EDITOR_FILES = [
  'src/components/agents/AgentEditForm.tsx',
  'src/components/agents/AgentRulesEditor.tsx',
  'src/components/agents/AgentSkillsEditor.tsx',
  'src/components/agents/AgentStepsEditor.tsx',
  'src/components/agents/AgentToolBlocksEditor.tsx',
  'src/components/agents/AgentVariablesEditor.tsx',
  'src/components/agents/IsolationTargetSelector.tsx',
] as const
const AGENT_SURFACE_FILES = [
  'src/components/activity/agents/AgentsTabList.tsx',
  'src/components/agents/AgentPortfolioPage.tsx',
] as const
const ACTIVITY_LIST_DETAIL_SURFACE_FILES = [
  'src/components/activity/ActivityMcpTab.tsx',
  'src/components/activity/CronTab.tsx',
  'src/components/activity/FileChangesTab.tsx',
  'src/components/activity/IntegrationsTab.tsx',
  'src/components/activity/MemoryTab.tsx',
  'src/components/activity/PlanReviewCard.tsx',
  'src/components/activity/RulesTab.tsx',
  'src/components/activity/SkillsTab.tsx',
  'src/components/activity/StagesTab.tsx',
  'src/components/activity/TasksTabDetailPanel.tsx',
  'src/components/activity/TracesTab.tsx',
  'src/components/activity/fields/KeyValueField.tsx',
  'src/components/activity/integrations/ChannelDetailPanel.tsx',
  'src/components/activity/integrations/ChannelsList.tsx',
  'src/components/activity/integrations/IntegrationsFilterPanel.tsx',
  'src/components/activity/memory/MemoryDetailPanel.tsx',
  'src/components/activity/memory/MemoryTabList.tsx',
  'src/components/activity/rules/RulesTabList.tsx',
  'src/components/activity/skills/SkillsHubView.tsx',
  'src/components/activity/skills/SkillsInstalledList.tsx',
  'src/components/activity/stages/ProfilesList.tsx',
  'src/components/activity/stages/StagesList.tsx',
  'src/components/activity/taskdetail/TaskDetailKV.tsx',
  'src/components/activity/taskdetail/TaskDetailRelationships.tsx',
] as const
const ACTIVITY_SELECT_FIELD_FILES = [
  'src/components/activity/RulesTab.tsx',
  'src/components/activity/SkillsTab.tsx',
  'src/components/activity/integrations/IntegrationsFilterPanel.tsx',
  'src/components/activity/skills/SkillsHubView.tsx',
] as const
const ACTIVITY_CHIP_ADOPTER_FILES = [
  'src/components/activity/ActivityMcpTab.tsx',
  'src/components/activity/integrations/ChannelDetailPanel.tsx',
  'src/components/activity/integrations/ChannelsList.tsx',
  'src/components/activity/memory/MemoryDetailPanel.tsx',
  'src/components/activity/memory/MemoryTabList.tsx',
  'src/components/activity/rules/RulesDetailPanel.tsx',
  'src/components/activity/rules/RulesTabList.tsx',
  'src/components/activity/skills/SkillsHubView.tsx',
  'src/components/activity/skills/SkillsInstalledDetail.tsx',
  'src/components/activity/skills/SkillsInstalledList.tsx',
  'src/components/activity/stages/ProfileDetailPanel.tsx',
  'src/components/activity/stages/ProfilesList.tsx',
  'src/components/activity/stages/StageDetailPanel.tsx',
  'src/components/activity/stages/StagesList.tsx',
] as const
const PIPELINE_SURFACE_FILES = [
  'src/components/activity/PipelinesTab.tsx',
  'src/components/activity/pipelines/PipelineEditor.tsx',
  'src/components/activity/pipelines/PipelineStepFields.tsx',
  'src/components/activity/pipelines/PipelineStepList.tsx',
  'src/components/activity/pipelines/PipelinesDefsDetail.tsx',
  'src/components/activity/pipelines/PipelinesDefsList.tsx',
  'src/components/shared/executions/execution-utils.tsx',
] as const

const BTN_CLASS = /(?<![\w-])btn(?:-[\w-]+)?\b/g
const CLS_CONSTANT = /\bconst\s+[A-Za-z0-9_]*_CLS\b\s*=/g
// Viewport width variants hand-roll a breakpoint the responsive contract
// forbids: the tier is authored once in tailwind-theme.css (width <=767px OR
// height <=500px, 768px desktop) and per-component width thresholds collapse
// into the shared `mobile:` variant — whether written as `max-[Npx]:` or as a
// raw `[@media(max-width:…)]:` arbitrary variant (#19183). `@max-[...]`
// container queries are element-scoped and exempt.
const TIER_VARIANT =
  /(?<!@)max-\[\d+px\]:|\[@media\((?:max|min)-(?:width|height):[^\]]+\)\]:/g
const IMPORTANT = /!\s*important\b/g
const RAW_ELEMENTS: Record<RawElement, RegExp> = {
  button: /<button\b/g,
  input: /<input\b/g,
  select: /<select\b/g,
  textarea: /<textarea\b/g,
}

function scannedFiles(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) files.push(...scannedFiles(path))
      continue
    }
    if (!entry.isFile()) continue
    if (/\.(?:test|spec)\.(?:ts|tsx)$/.test(entry.name)) continue
    if (entry.name.endsWith('.d.ts')) continue
    if (/\.(?:ts|tsx|css)$/.test(entry.name)) files.push(path)
  }
  return files
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

function countMatches(source: string, pattern: RegExp): number {
  return Array.from(source.matchAll(pattern)).length
}

interface Scan {
  btnClass: Map<string, number>
  rawElements: Record<RawElement, Map<string, number>>
  clsConstant: Map<string, number>
  cssFiles: string[]
  important: Map<string, number>
  cssTotalLines: number
  tierVariant: Map<string, number>
}

function runScan(): Scan {
  const scan: Scan = {
    btnClass: new Map(),
    rawElements: { button: new Map(), input: new Map(), select: new Map(), textarea: new Map() },
    clsConstant: new Map(),
    cssFiles: [],
    important: new Map(),
    cssTotalLines: 0,
    tierVariant: new Map(),
  }

  for (const file of scannedFiles(join(process.cwd(), 'src'))) {
    const rel = relative(process.cwd(), file).split('\\').join('/')
    const raw = readFileSync(file, 'utf8')
    const source = stripComments(raw)

    if (rel.endsWith('.css')) {
      scan.cssFiles.push(rel)
      scan.cssTotalLines += raw.split('\n').length
    } else {
      const btn = countMatches(source, BTN_CLASS)
      if (btn > 0) scan.btnClass.set(rel, btn)
      const cls = countMatches(source, CLS_CONSTANT)
      if (cls > 0) scan.clsConstant.set(rel, cls)
      const tier = countMatches(source, TIER_VARIANT)
      if (tier > 0) scan.tierVariant.set(rel, tier)
      if (rel.endsWith('.tsx') && !rel.startsWith(UI_PRIMITIVES_DIR)) {
        for (const [element, pattern] of Object.entries(RAW_ELEMENTS)) {
          const count = countMatches(source, pattern)
          if (count > 0) scan.rawElements[element as RawElement].set(rel, count)
        }
      }
    }

    const importantCount = countMatches(source, IMPORTANT)
    if (importantCount > 0) scan.important.set(rel, importantCount)
  }

  return scan
}

const scan = runScan()

// Compares a scan against its allowlist. `remedy` is appended to over-ceiling
// failures; stale failures always demand tightening the allowlist entry.
function ratchet(
  actual: Map<string, number>,
  allowed: Record<string, number>,
  label: string,
  remedy: string,
): string[] {
  const failures: string[] = []
  for (const [file, count] of [...actual.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const ceiling = allowed[file] ?? 0
    if (count > ceiling) {
      failures.push(`${file}: ${count} ${label} (allowlist permits ${ceiling}). ${remedy}`)
    }
  }
  for (const [file, ceiling] of Object.entries(allowed)) {
    const count = actual.get(file) ?? 0
    if (count < ceiling) {
      failures.push(
        `${file}: allowlist records ${ceiling} ${label} but only ${count} remain. ` +
          `Decrease or delete the entry in ${ALLOWLIST} — entries only ever shrink.`,
      )
    }
  }
  return failures
}

interface AllowlistSnapshot {
  counts: Map<string, number>
  cssFiles: Set<string>
  cssTotalLinePin: number
}

function delimitedBody(
  source: string,
  marker: string,
  openChar: '{' | '[',
  closeChar: '}' | ']',
): string {
  const markerIndex = source.indexOf(marker)
  if (markerIndex < 0) throw new Error(`Missing ${marker} in style ratchet allowlist`)
  const openIndex = source.indexOf(openChar, markerIndex + marker.length)
  if (openIndex < 0) throw new Error(`Missing ${openChar} after ${marker}`)
  let depth = 0
  for (let index = openIndex; index < source.length; index += 1) {
    if (source[index] === openChar) depth += 1
    if (source[index] === closeChar) depth -= 1
    if (depth === 0) return source.slice(openIndex + 1, index)
  }
  throw new Error(`Unclosed ${openChar} after ${marker}`)
}

function pathCounts(body: string): Map<string, number> {
  return new Map(
    [...body.matchAll(/'([^']+)':\s*(\d+)/g)].map((match) => [match[1], Number(match[2])]),
  )
}

function parseAllowlistSnapshot(source: string): AllowlistSnapshot {
  const counts = new Map<string, number>()
  for (const name of ['BTN_CLASS_ALLOWLIST', 'CLS_CONSTANT_ALLOWLIST', 'IMPORTANT_ALLOWLIST']) {
    for (const [file, count] of pathCounts(delimitedBody(source, `export const ${name}`, '{', '}'))) {
      counts.set(`${name}:${file}`, count)
    }
  }

  const rawBody = delimitedBody(source, 'export const RAW_ELEMENT_ALLOWLIST', '{', '}')
  for (const element of Object.keys(RAW_ELEMENTS) as RawElement[]) {
    for (const [file, count] of pathCounts(delimitedBody(rawBody, `${element}:`, '{', '}'))) {
      counts.set(`RAW_ELEMENT_ALLOWLIST:${element}:${file}`, count)
    }
  }

  const cssFiles = new Set(
    [...delimitedBody(source, 'export const CSS_FILE_ALLOWLIST', '[', ']').matchAll(/'([^']+)'/g)]
      .map((match) => match[1]),
  )
  const linePinMatch = source.match(/export const CSS_TOTAL_LINE_(?:PIN|CEILING)\s*=\s*(\d+)/)
  if (!linePinMatch) {
    throw new Error('Missing CSS_TOTAL_LINE_PIN or legacy CSS_TOTAL_LINE_CEILING')
  }

  return {
    counts,
    cssFiles,
    cssTotalLinePin: Number(linePinMatch[1]),
  }
}

function allowlistPath(path: string): string {
  return path.startsWith('web/') ? path.slice('web/'.length) : path
}

function renamedAllowlistPaths(targetRef: string): Map<string, string> {
  const diff = execFileSync(
    'git',
    ['diff', '--name-status', '-M', targetRef, 'HEAD', '--', 'web/src'],
    { encoding: 'utf8' },
  )
  const renames = new Map<string, string>()
  for (const line of diff.split('\n')) {
    const [status, targetPath, currentPath] = line.split('\t')
    if (!status?.startsWith('R') || !targetPath || !currentPath) continue
    renames.set(allowlistPath(currentPath), allowlistPath(targetPath))
  }
  return renames
}

function targetKeyForCurrent(key: string, renames: ReadonlyMap<string, string>): string {
  for (const [currentPath, targetPath] of renames) {
    const suffix = `:${currentPath}`
    if (key.endsWith(suffix)) {
      return `${key.slice(0, -currentPath.length)}${targetPath}`
    }
  }
  return key
}

function targetBranchFailures(
  current: AllowlistSnapshot,
  target: AllowlistSnapshot,
  renames: ReadonlyMap<string, string> = new Map(),
): string[] {
  const failures: string[] = []
  for (const [key, count] of current.counts) {
    const targetCount = target.counts.get(targetKeyForCurrent(key, renames))
    if (targetCount === undefined) {
      failures.push(`${key}: new allowlist entry; migrate the debt instead`)
    } else if (count > targetCount) {
      failures.push(`${key}: allowance increased from ${targetCount} to ${count}`)
    }
  }
  for (const file of current.cssFiles) {
    if (!target.cssFiles.has(renames.get(file) ?? file)) {
      failures.push(`CSS_FILE_ALLOWLIST:${file}: new recorded stylesheet`)
    }
  }
  if (current.cssTotalLinePin > target.cssTotalLinePin) {
    failures.push(
      `CSS_TOTAL_LINE_PIN: increased from ${target.cssTotalLinePin} ` +
        `to ${current.cssTotalLinePin}`,
    )
  }
  return failures
}

describe('style ratchet', () => {
  it('never loosens the allowlist relative to the pull request target branch', () => {
    const targetRef = process.env.STYLE_RATCHET_TARGET_REF?.trim()
    if (!targetRef) return
    const targetSource = execFileSync(
      'git',
      ['show', `${targetRef}:${ALLOWLIST_REPO_PATH}`],
      { encoding: 'utf8' },
    )
    const currentSource = readFileSync(ALLOWLIST, 'utf8')

    expect(
      targetBranchFailures(
        parseAllowlistSnapshot(currentSource),
        parseAllowlistSnapshot(targetSource),
        renamedAllowlistPaths(targetRef),
      ),
    ).toEqual([])
  })

  it('keeps verified file renames under the target branch debt ceiling', () => {
    const target: AllowlistSnapshot = {
      counts: new Map([['RAW_ELEMENT_ALLOWLIST:select:src/OldPanel.tsx', 2]]),
      cssFiles: new Set(['styles/old-panel.css']),
      cssTotalLinePin: 10,
    }
    const current: AllowlistSnapshot = {
      counts: new Map([['RAW_ELEMENT_ALLOWLIST:select:src/NewPanel.tsx', 2]]),
      cssFiles: new Set(['styles/new-panel.css']),
      cssTotalLinePin: 10,
    }
    const renames = new Map([
      ['src/NewPanel.tsx', 'src/OldPanel.tsx'],
      ['styles/new-panel.css', 'styles/old-panel.css'],
    ])

    expect(targetBranchFailures(current, target, renames)).toEqual([])

    current.counts.set('RAW_ELEMENT_ALLOWLIST:select:src/NewPanel.tsx', 3)
    expect(targetBranchFailures(current, target, renames)).toEqual([
      'RAW_ELEMENT_ALLOWLIST:select:src/NewPanel.tsx: allowance increased from 2 to 3',
    ])
  })

  it('parses legacy target-branch line ceilings as exact pins', () => {
    const currentSource = readFileSync(ALLOWLIST, 'utf8')
    const legacyTargetSource = `${currentSource.replace(
      'CSS_TOTAL_LINE_PIN',
      'CSS_TOTAL_LINE_CEILING',
    )}\nexport const CSS_LINE_TIGHTEN_SLACK = 200\n`

    expect(parseAllowlistSnapshot(currentSource).cssTotalLinePin).toBe(CSS_TOTAL_LINE_PIN)
    expect(parseAllowlistSnapshot(legacyTargetSource).cssTotalLinePin).toBe(CSS_TOTAL_LINE_PIN)
  })

  it('bans .btn class usage', () => {
    expect(BTN_CLASS_ALLOWLIST).toEqual({})
    expect([...scan.btnClass.entries()]).toEqual([])
  })

  it('keeps raw interactive elements at or below the recorded per-file counts', () => {
    const failures = (Object.keys(RAW_ELEMENTS) as RawElement[]).flatMap((element) =>
      ratchet(
        scan.rawElements[element],
        RAW_ELEMENT_ALLOWLIST[element],
        `raw <${element}> elements`,
        `Use the components/ui primitive instead of a raw <${element}>; additions require an ` +
          `explicit moat — see ${STYLE_GUIDE}.`,
      ),
    )
    expect(failures).toEqual([])
  })

  it('keeps agent editors at zero raw-control and shared-style debt', () => {
    for (const element of Object.keys(RAW_ELEMENTS) as RawElement[]) {
      for (const file of AGENT_EDITOR_FILES) {
        expect(scan.rawElements[element].get(file) ?? 0, `${file} raw <${element}> count`).toBe(0)
      }
    }
    expect(scan.clsConstant.get('src/components/agents/agents-styles.ts') ?? 0).toBe(0)
  })

  it('keeps swept agent surfaces at zero raw-control and SidebarPanel stylesheet debt', () => {
    for (const element of Object.keys(RAW_ELEMENTS) as RawElement[]) {
      for (const file of AGENT_SURFACE_FILES) {
        expect(scan.rawElements[element].get(file) ?? 0, `${file} raw <${element}> count`).toBe(0)
      }
    }
    expect(scan.cssFiles).not.toContain('src/components/shared/SidebarPanel.css')
  })

  it('keeps swept activity lists and detail panels at zero raw-control debt', () => {
    for (const element of Object.keys(RAW_ELEMENTS) as RawElement[]) {
      for (const file of ACTIVITY_LIST_DETAIL_SURFACE_FILES) {
        expect(scan.rawElements[element].get(file) ?? 0, `${file} raw <${element}> count`).toBe(0)
      }
    }
  })

  it('keeps activity filter-panel selects on SelectField', () => {
    for (const file of ACTIVITY_SELECT_FIELD_FILES) {
      expect(readFileSync(file, 'utf8'), `${file} SelectField composition`).toMatch(/<SelectField\b/)
    }
  })

  it('keeps activity metadata chip adopters on ui Chip', () => {
    for (const file of ACTIVITY_CHIP_ADOPTER_FILES) {
      const source = readFileSync(file, 'utf8')
      expect(source, `${file} Chip composition`).toMatch(/<Chip\b/)
      // Adoption must orphan the legacy pill classes so 5.4 can delete
      // their rules with the sheet; tones replace the -- modifiers.
      expect(source, `${file} legacy chip class residue`).not.toMatch(/activity-(?:mcp-)?chip/)
    }
  })

  it('keeps swept pipeline surfaces at zero raw-control and shared-style debt', () => {
    for (const element of Object.keys(RAW_ELEMENTS) as RawElement[]) {
      for (const file of PIPELINE_SURFACE_FILES) {
        expect(scan.rawElements[element].get(file) ?? 0, `${file} raw <${element}> count`).toBe(0)
      }
    }
    expect(
      existsSync('src/components/activity/pipelines/PipelineEditor.styles.ts'),
      'PipelineEditor.styles.ts must stay retired',
    ).toBe(false)
    expect(scan.clsConstant.get('src/components/shared/executions/execution-utils.tsx') ?? 0).toBe(0)
  })

  it('bans *_CLS style constants', () => {
    expect(CLS_CONSTANT_ALLOWLIST).toEqual({})
    expect([...scan.clsConstant.entries()]).toEqual([])
  })

  it('bans viewport width variants outside the shared tier', () => {
    expect([...scan.tierVariant.entries()]).toEqual([])
  })

  it('bans stylesheets beyond the recorded set', () => {
    const recorded = new Set(CSS_FILE_ALLOWLIST)
    const actual = new Set(scan.cssFiles)
    const failures = [
      ...scan.cssFiles
        .filter((file) => !recorded.has(file))
        .map(
          (file) =>
            `${file}: new stylesheet. New CSS files are banned — use Tailwind utilities and components/ui primitives (${STYLE_GUIDE}).`,
        ),
      ...CSS_FILE_ALLOWLIST.filter((file) => !actual.has(file)).map(
        (file) => `${file}: recorded stylesheet no longer exists. Delete its entry in ${ALLOWLIST}.`,
      ),
    ]
    expect(failures).toEqual([])
  })

  it('keeps !important usage at or below the recorded per-file counts', () => {
    expect(
      ratchet(
        scan.important,
        IMPORTANT_ALLOWLIST,
        '!important declarations',
        `Never add !important; fix specificity at the source — see ${STYLE_GUIDE}.`,
      ),
    ).toEqual([])
  })

  it('pins the exact total CSS line count', () => {
    expect(
      scan.cssTotalLines,
      `Total CSS is ${scan.cssTotalLines} lines; the exact pin is ${CSS_TOTAL_LINE_PIN}. ` +
        `Update CSS_TOTAL_LINE_PIN consciously in the same commit as any infra CSS change ` +
        `(${ALLOWLIST}).`,
    ).toBe(CSS_TOTAL_LINE_PIN)
  })
})

describe('tailwind cascade', () => {
  // Plan section 6.1: the `important: true` config flag is retired. Utilities
  // win by layer order alone; the only !important left in the codebase is the
  // six-declaration set recorded in IMPORTANT_ALLOWLIST.
  it('emits no !important from compiled utilities', async () => {
    const webRoot = join(fileURLToPath(import.meta.url), '..', '..', '..')
    const tailwind = await compile(
      '@import "tailwindcss";\n@config "./tailwind.config.ts";',
      { base: webRoot, onDependency() {} },
    )
    const css = tailwind.build(['flex', 'h-4', 'text-sm', 'animate-spin'])
    // Preflight legitimately ships one !important ([hidden] display), so the
    // assertion scopes to the utilities layer — the surface the retired
    // `important: true` flag used to blanket.
    const utilitiesLayer = css.slice(css.indexOf('@layer utilities'))
    expect(utilitiesLayer).toContain('@layer utilities')
    expect(utilitiesLayer).not.toMatch(IMPORTANT)
  })
})
