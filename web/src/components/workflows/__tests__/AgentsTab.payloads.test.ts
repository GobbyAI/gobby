import { describe, expect, it } from 'vitest'
import type { WorkflowStep } from '../../agents/AgentStepsEditor'
import {
  buildCreateAgentBody,
  buildUpdateAgentBody,
  buildYamlDefinitionBody,
  defaultSidebarYaml,
  extractAgentEditWorkflowState,
  parseYamlObject,
  type AgentPayloadState,
} from '../AgentsTab.payloads'
import { DEFAULT_FORM, type AgentDefInfo } from '../AgentsTab.types'

function payloadState(overrides: Partial<AgentPayloadState> = {}): AgentPayloadState {
  return {
    form: {
      ...DEFAULT_FORM,
      name: 'reviewer',
      provider: 'claude',
      mode: 'inherit',
      isolation: 'inherit',
      base_branch: 'main',
      timeout: 30,
      max_turns: 4,
    },
    rules: [],
    ruleSelectors: null,
    variables: {},
    skills: [],
    steps: [],
    blockedTools: [],
    blockedMcpTools: [],
    ...overrides,
  }
}

function agentDefinition(overrides: Partial<AgentDefInfo['definition']> = {}): AgentDefInfo {
  return {
    definition: {
      name: 'reviewer',
      description: null,
      surfaces: ['spawn'],
      role: null,
      goal: null,
      personality: null,
      instructions: null,
      provider: 'claude',
      model: null,
      fallback_agent: null,
      mode: 'inherit',
      isolation: null,
      base_branch: 'main',
      timeout: 30,
      max_turns: 4,
      default_workflow: null,
      sandbox: null,
      skill_profile: null,
      workflows: null,
      lifecycle_variables: {},
      default_variables: {},
      ...overrides,
    },
    source: 'installed',
    source_path: null,
    db_id: 'agent-id',
    enabled: true,
    overridden_by: null,
    deleted_at: null,
    tags: null,
  }
}

describe('AgentsTab payload helpers', () => {
  it('keeps create payload sparse while update payload clears nullable fields', () => {
    const state = payloadState({
      form: {
        ...payloadState().form,
        description: '',
        role: '',
        model: '',
      },
    })

    expect(buildCreateAgentBody(state)).not.toHaveProperty('description')
    expect(buildUpdateAgentBody(state)).toMatchObject({
      description: null,
      role: null,
      model: null,
    })
  })

  it('normalizes reasoning effort and required flag', () => {
    expect(buildCreateAgentBody(payloadState())).toMatchObject({
      reasoning_effort: null,
      reasoning_required: false,
    })

    const body = buildCreateAgentBody(payloadState({
      form: {
        ...payloadState().form,
        reasoning_effort: 'high',
        reasoning_required: true,
      },
    }))
    expect(body).toMatchObject({
      reasoning_effort: 'high',
      reasoning_required: true,
    })
  })

  it('nests workflow fields and includes blocked tool arrays', () => {
    const steps: WorkflowStep[] = [{ name: 'review', allowed_tools: 'all' }]
    const body = buildUpdateAgentBody(payloadState({
      form: { ...payloadState().form, pipeline: 'qa-pipeline' },
      rules: ['rule-a'],
      ruleSelectors: { include: ['tag:qa'], exclude: ['tag:wip'] },
      variables: { threshold: 2 },
      skills: ['code-review'],
      steps,
      blockedTools: ['Bash'],
      blockedMcpTools: ['gobby-tasks.close_task'],
    }))

    expect(body.workflows).toEqual({
      pipeline: 'qa-pipeline',
      rules: ['rule-a'],
      rule_selectors: { include: ['tag:qa'], exclude: ['tag:wip'] },
      variables: { threshold: 2 },
      skill_selectors: { include: ['code-review'] },
    })
    expect(body.steps).toEqual(steps)
    expect(body.blocked_tools).toEqual(['Bash'])
    expect(body.blocked_mcp_tools).toEqual(['gobby-tasks.close_task'])
  })

  it('extracts skill selectors with legacy skill_profile fallback', () => {
    expect(extractAgentEditWorkflowState(agentDefinition({
      workflows: { skill_selectors: { include: ['*', 'code-review'] } },
      skill_profile: { legacy: {} },
    })).skills).toEqual(['code-review'])

    expect(extractAgentEditWorkflowState(agentDefinition({
      skill_profile: { legacy: {}, docs: {} },
    })).skills).toEqual(['legacy', 'docs'])
  })

  it('extracts edit workflow state including blocked tools', () => {
    const state = extractAgentEditWorkflowState(agentDefinition({
      workflows: {
        rules: ['rule-a'],
        rule_selectors: { include: ['tag:qa'], exclude: [] },
        variables: { owner: 'qa' },
      },
      steps: [{ name: 'review' }],
      blocked_tools: ['Bash'],
      blocked_mcp_tools: ['gobby-tasks.close_task'],
    }))

    expect(state).toMatchObject({
      rules: ['rule-a'],
      ruleSelectors: { include: ['tag:qa'], exclude: [] },
      variables: { owner: 'qa' },
      skills: [],
      blockedTools: ['Bash'],
      blockedMcpTools: ['gobby-tasks.close_task'],
    })
    expect(state.steps).toEqual([{ name: 'review' }])
  })

  it('converts YAML into the sidebar/API update body', () => {
    const parsed = parseYamlObject('name: yaml-agent\nworkflows:\n  pipeline: qa-pipeline\n')
    const body = buildYamlDefinitionBody(parsed, {
      name: 'fallback-agent',
      surfaces: ['spawn'],
      provider: 'claude',
      mode: 'inherit',
      baseBranch: 'main',
      timeout: 30,
      maxTurns: 4,
    })

    expect(body).toMatchObject({
      name: 'yaml-agent',
      surfaces: ['spawn'],
      provider: 'claude',
      mode: 'inherit',
      base_branch: 'main',
      timeout: 30,
      max_turns: 4,
      workflows: { pipeline: 'qa-pipeline' },
    })
    expect(() => parseYamlObject('- invalid')).toThrow('Invalid YAML: expected an object')
    expect(defaultSidebarYaml()).toContain('provider: inherit')
  })
})
