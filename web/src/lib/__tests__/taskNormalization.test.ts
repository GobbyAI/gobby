import { describe, expect, it } from 'vitest'
import { normalizeStageRow } from '../taskNormalization'

describe('normalizeStageRow display_name fallback', () => {
  it('preserves QA as an acronym when titleizing snake_case stage names', () => {
    const stage = normalizeStageRow({ name: 'holistic_qa' })
    expect(stage.display_name).toBe('Holistic QA')
  })

  it('preserves PR as an acronym when titleizing snake_case stage names', () => {
    expect(normalizeStageRow({ name: 'pr' }).display_name).toBe('PR')
  })

  it('preserves multi-letter acronyms in mixed segments', () => {
    expect(normalizeStageRow({ name: 'mcp_audit' }).display_name).toBe(
      'MCP Audit',
    )
    expect(normalizeStageRow({ name: 'cli_smoke' }).display_name).toBe(
      'CLI Smoke',
    )
  })

  it('title-cases non-acronym segments', () => {
    expect(normalizeStageRow({ name: 'development' }).display_name).toBe(
      'Development',
    )
    expect(normalizeStageRow({ name: 'test_arch' }).display_name).toBe(
      'Test Arch',
    )
  })

  it('prefers display_label over the titleizer fallback', () => {
    const stage = normalizeStageRow({
      name: 'holistic_qa',
      display_label: 'Holistic QA Review',
    })
    expect(stage.display_name).toBe('Holistic QA Review')
  })

  it('prefers display_name over display_label when both are present', () => {
    const stage = normalizeStageRow({
      name: 'holistic_qa',
      display_name: 'Custom Name',
      display_label: 'Holistic QA',
    })
    expect(stage.display_name).toBe('Custom Name')
  })
})
