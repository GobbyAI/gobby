import { describe, expect, it } from 'vitest'
import {
  extractTaskPayload,
  isRawTaskPayload,
  normalizeStageRow,
  normalizeStagesRegistryResponse,
  normalizeTaskPayload,
  parseReviewerAgentSelector,
} from '../taskNormalization'

describe('isRawTaskPayload optional field guard', () => {
  it('accepts string, string array, and null optional task fields', () => {
    expect(
      isRawTaskPayload({
        id: 'task-1',
        description: 'Describe the task',
        validation_criteria: null,
        labels: ['web', 'tasks'],
        additional_skills: null,
      }),
    ).toBe(true)
  })

  it('rejects invalid optional task field values', () => {
    const invalidPayloads = [
      { id: 'task-1', description: 42 },
      { id: 'task-1', validation_criteria: ['run tests'] },
      { id: 'task-1', labels: 'web' },
      { id: 'task-1', labels: ['web', 42] },
      { id: 'task-1', owner_session_ref: { session_id: 'sess-1' } },
      { id: 'task-1', owner_session_ref: { session_id: '', ref: '#1' } },
      { id: 'task-1', owner_session_ref: { session_id: 'sess-1', ref: ' ' } },
    ]

    invalidPayloads.forEach(payload => {
      expect(isRawTaskPayload(payload)).toBe(false)
    })
  })

  it('accepts resolved owner session refs', () => {
    expect(
      isRawTaskPayload({
        id: 'task-1',
        owner_session_ref: {
          session_id: 'sess-1',
          ref: '#12',
          source: 'codex',
        },
      }),
    ).toBe(true)
  })

  it('accepts resolved owner session refs without source', () => {
    expect(
      isRawTaskPayload({
        id: 'task-1',
        owner_session_ref: {
          session_id: 'sess-1',
          ref: '#12',
        },
      }),
    ).toBe(true)
  })
})

describe('extractTaskPayload', () => {
  it('extracts nested task payloads from data wrappers', () => {
    expect(extractTaskPayload({ data: { data: { task: { id: 'task-1' } } } })).toEqual({
      id: 'task-1',
    })
  })

  it('caps nested payload extraction depth', () => {
    expect(
      extractTaskPayload({ data: { data: { data: { data: { data: { task: { id: 'task-1' } } } } } } }),
    ).toBeNull()
  })

  it('extracts payloads before the depth guard boundary', () => {
    expect(
      extractTaskPayload({ data: { data: { data: { data: { task: { id: 'task-1' } } } } } }),
    ).toEqual({ id: 'task-1' })
  })

  it('returns null for circular wrapper payloads', () => {
    const payload: Record<string, unknown> = {}
    payload.data = payload
    expect(extractTaskPayload(payload)).toBeNull()
  })
})

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
  it('rejects missing, blank, non-object, and blank-default payloads', () => {
    expect(parseReviewerAgentSelector(null)).toBeNull()
    expect(parseReviewerAgentSelector(undefined)).toBeNull()
    expect(parseReviewerAgentSelector('')).toBeNull()
    expect(parseReviewerAgentSelector('   ')).toBeNull()
    expect(parseReviewerAgentSelector('[]')).toBeNull()
    expect(parseReviewerAgentSelector('"qa-reviewer"')).toBeNull()
    expect(parseReviewerAgentSelector('42')).toBeNull()
    expect(parseReviewerAgentSelector('true')).toBeNull()
    expect(parseReviewerAgentSelector(JSON.stringify({ default: '   ', rules: [] }))).toBeNull()
  })

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

  it('trims padded selector defaults, categories, and reviewer agents', () => {
    expect(
      parseReviewerAgentSelector(
        JSON.stringify({
          default: ' qa-reviewer ',
          rules: [{ category: ' docs ', reviewer_agent: ' doc-reviewer ' }],
        }),
      ),
    ).toEqual({
      default: 'qa-reviewer',
      rules: [{ category: 'docs', reviewer_agent: 'doc-reviewer' }],
    })
  })
})
