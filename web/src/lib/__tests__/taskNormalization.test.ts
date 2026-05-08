import { describe, expect, it } from 'vitest'
import {
  normalizeStageRow,
  normalizeStagesRegistryResponse,
  normalizeTaskPayload,
  parseReviewerAgentSelector,
} from '../taskNormalization'

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
    expect(normalizeStageRow({ name: 'planning_review' }).display_name).toBe(
      'Planning Review',
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

  it('filters retired stages from registry and task payloads', () => {
    expect(
      normalizeStagesRegistryResponse({
        stages: [
          { name: 'development', display_name: 'Development' },
          { name: 'test_arch', display_name: 'Test Architecture' },
        ],
      }).map(stage => stage.name),
    ).toEqual(['development'])

    const task = normalizeTaskPayload({
      id: 'task-1',
      current_stage: { name: 'test_arch', state: 'ready' },
      stages: [
        { name: 'test_arch', state: 'ready' },
        { name: 'development', state: 'ready' },
      ],
    })

    expect(task.current_stage?.name).toBe('development')
    expect(task.stages.map(stage => stage.name)).toEqual(['development'])
  })
})

describe('parseReviewerAgentSelector', () => {
  it('parses reviewer agent selector JSON for future stage registry callers', () => {
    expect(
      parseReviewerAgentSelector(
        JSON.stringify({
          default: 'qa-reviewer',
          rules: [
            { category: 'docs', reviewer_agent: 'doc-reviewer' },
            { category: '', reviewer_agent: 'fallback-reviewer' },
            { category: 'bad' },
          ],
        }),
      ),
    ).toEqual({
      default: 'qa-reviewer',
      rules: [
        { category: 'docs', reviewer_agent: 'doc-reviewer' },
        { reviewer_agent: 'fallback-reviewer' },
      ],
    })
    expect(parseReviewerAgentSelector('{bad')).toBeNull()
    expect(parseReviewerAgentSelector(JSON.stringify({ rules: [] }))).toBeNull()
  })
})
