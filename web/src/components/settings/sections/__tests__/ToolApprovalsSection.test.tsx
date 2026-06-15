import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ToolApprovalsSection } from '../ToolApprovalsSection'
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from '../SettingsSectionContext'

// Schema covering the rows the assertions touch. `tool_approval` resolves one
// hop through `$ref` to ToolApprovalConfig, whose `default_policy` enum drives
// the schema-backed select and whose `policies` array (items -> ToolApprovalPolicy)
// is the audit's `array<object>` "fix" row rendered as a structured editor.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    ToolApprovalPolicy: {
      type: 'object',
      properties: {
        server_pattern: { type: 'string' },
        tool_pattern: { type: 'string' },
        policy: {
          enum: ['auto', 'approve_once', 'always_ask'],
          type: 'string',
        },
      },
    },
    ToolApprovalConfig: {
      type: 'object',
      properties: {
        enabled: { type: 'boolean' },
        default_policy: {
          enum: ['auto', 'approve_once', 'always_ask'],
          type: 'string',
        },
        policies: {
          type: 'array',
          items: { $ref: '#/$defs/ToolApprovalPolicy' },
        },
      },
    },
  },
  type: 'object',
  properties: {
    tool_approval: { $ref: '#/$defs/ToolApprovalConfig' },
  },
}

function makeConfigValues(): Record<string, unknown> {
  return {
    tool_approval: {
      enabled: true,
      default_policy: 'approve_once',
      policies: [
        {
          server_pattern: 'gobby-tasks',
          tool_pattern: 'create_task',
          policy: 'always_ask',
        },
      ],
    },
  }
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    globalApprovalRules: ['tool:Write'],
    defaultApprovalRules: ['tool:Read', 'tool:Glob'],
    builtInApprovalExemptions: ['tool:TodoWrite'],
    saveGlobalApprovalRules: vi.fn(async () => true),
    ...overrides,
  }
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ToolApprovalsSection />
    </SettingsSectionContext.Provider>,
  )
}

describe('ToolApprovalsSection', () => {
  it('renders the enablement toggle and schema-backed default policy select', () => {
    renderSection(makeContext())

    expect(
      screen.getByRole('switch', { name: 'Enable tool approval prompts' }),
    ).toBeChecked()

    const policy = screen.getByLabelText('Default approval policy')
    expect(policy).toHaveValue('approve_once')
    expect(within(policy).getAllByRole('option')).toHaveLength(3)
  })

  it('renders the existing per-tool policy row as a structured editor', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Per-tool policy 1 server pattern')).toHaveValue(
      'gobby-tasks',
    )
    expect(screen.getByLabelText('Per-tool policy 1 tool pattern')).toHaveValue(
      'create_task',
    )
    const rowPolicy = screen.getByLabelText('Per-tool policy 1 approval')
    expect(rowPolicy).toHaveValue('always_ask')
    expect(within(rowPolicy).getAllByRole('option')).toHaveLength(3)
  })

  it('persists an edited per-tool policy through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Per-tool policy 1 server pattern'), {
      target: { value: 'gobby-memory' },
    })
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        'tool_approval.policies': [
          {
            server_pattern: 'gobby-memory',
            tool_pattern: 'create_task',
            policy: 'always_ask',
          },
        ],
      }),
    )
  })

  it('appends a default-shaped per-tool policy row through the section Save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('button', { name: 'Add policy' }))
    const save = screen.getByRole('button', { name: 'Save' })
    await waitFor(() => expect(save).toBeEnabled())
    fireEvent.click(save)

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1))
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        'tool_approval.policies': [
          {
            server_pattern: 'gobby-tasks',
            tool_pattern: 'create_task',
            policy: 'always_ask',
          },
          { server_pattern: '*', tool_pattern: '*', policy: 'always_ask' },
        ],
      }),
    )
  })

  it('renders the global auto-allow rules, defaults hint, and read-only exemptions', () => {
    renderSection(makeContext())

    expect(screen.getByLabelText('Auto-allow rule item 1')).toHaveValue(
      'tool:Write',
    )
    expect(screen.getByText(/tool:Read, tool:Glob/)).toBeInTheDocument()
    const exemption = screen.getByLabelText('Built-in exemption 1')
    expect(exemption).toHaveValue('tool:TodoWrite')
    expect(exemption).toBeDisabled()
  })

  it('saves global auto-allow rules immediately, separate from the config draft', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.change(screen.getByLabelText('Auto-allow rule item 1'), {
      target: { value: 'tool:Edit' },
    })
    const saveRules = screen.getByRole('button', { name: 'Save rules' })
    await waitFor(() => expect(saveRules).toBeEnabled())
    fireEvent.click(saveRules)

    await waitFor(() =>
      expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledTimes(1),
    )
    expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledWith(['tool:Edit'])
    expect(ctx.saveConfig).not.toHaveBeenCalled()
  })

  it('trims blank rows out of the global rules on save', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('button', { name: 'Add rule' }))
    const saveRules = screen.getByRole('button', { name: 'Save rules' })
    await waitFor(() => expect(saveRules).toBeEnabled())
    fireEvent.click(saveRules)

    await waitFor(() =>
      expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledTimes(1),
    )
    expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledWith(['tool:Write'])
  })

  it('resets the global rules to the recommended defaults before saving', async () => {
    const ctx = makeContext()
    renderSection(ctx)

    fireEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }))
    const saveRules = screen.getByRole('button', { name: 'Save rules' })
    await waitFor(() => expect(saveRules).toBeEnabled())
    fireEvent.click(saveRules)

    await waitFor(() =>
      expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledTimes(1),
    )
    expect(ctx.saveGlobalApprovalRules).toHaveBeenCalledWith([
      'tool:Read',
      'tool:Glob',
    ])
  })

  it('falls back gracefully when the global rules surface is unavailable', () => {
    renderSection(makeContext({ saveGlobalApprovalRules: undefined }))

    expect(
      screen.queryByRole('button', { name: 'Save rules' }),
    ).not.toBeInTheDocument()
    expect(
      screen.getByText('Global approval rules are unavailable.'),
    ).toBeInTheDocument()
  })
})
