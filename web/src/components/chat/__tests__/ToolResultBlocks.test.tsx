import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import {
  GsqzResultBlock,
  JsonResultBlock,
  MetadataStrip,
  ToolResultBody,
} from '../ToolResultBlocks'
import { TOOL_RESULT_CUSTOM_STYLE } from '../ToolCallCard.styles'

describe('JsonResultBlock', () => {
  it('renders embedded \\n inside string fields as actual line breaks', () => {
    const value = JSON.stringify({
      content: 'diff --git a/x.py b/x.py\n@@ -1,3 +1,3 @@\n-old\n+new',
      is_error: false,
    })
    const { container } = render(<JsonResultBlock value={value} />)
    const text = container.textContent ?? ''
    expect(text).toContain('diff --git a/x.py b/x.py')
    expect(text).toContain('@@ -1,3 +1,3 @@')
    expect(text).not.toContain('\\n')
  })

  it('renders the hanging-indent class for wrap support', () => {
    const { container } = render(<JsonResultBlock value={'{"k":"v"}'} />)
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre?.className).toContain('tool-result-wrap')
  })

  it('falls back to the raw string when input is not valid JSON', () => {
    const { container } = render(<JsonResultBlock value={'not json {{'} />)
    expect(container.textContent).toContain('not json {{')
  })

  it('uses the destructive palette in error variant', () => {
    const { container } = render(<JsonResultBlock value={'{"err":"x"}'} variant="error" />)
    const pre = container.querySelector('pre')
    expect(pre?.className).toMatch(/destructive/)
  })

  it('renders the normal variant transparently — no second off-shade slab (#14721)', () => {
    const { container } = render(<JsonResultBlock value={'{"k":"v"}'} />)
    const pre = container.querySelector('pre')
    // No bg-muted fill: the result sits on the bordered tool card surface.
    expect(pre?.className).not.toMatch(/bg-muted/)
    expect(pre?.className).not.toMatch(/bg-/)
    expect(pre?.className).toContain('text-foreground')
  })
})

describe('tool-result background (#14721)', () => {
  it('TOOL_RESULT_CUSTOM_STYLE overrides the shared code-bg pre fill', () => {
    // CodeBlock/PlainBody/LineNumberedBody read this so the syntax block
    // does not paint var(--code-bg) over the tool card.
    expect(TOOL_RESULT_CUSTOM_STYLE.background).toBe('transparent')
  })

  it('GsqzResultBlock wrapper carries no bg-muted fill', () => {
    const { container } = render(
      <GsqzResultBlock metadata={{ chunkId: 'abc' }} body="plain body" />,
    )
    const wrapper = container.firstElementChild as HTMLElement
    expect(wrapper.className).toContain('border-border/40')
    expect(wrapper.className).not.toMatch(/bg-muted/)
  })
})

describe('MetadataStrip', () => {
  it('renders nothing when meta is empty', () => {
    const { container } = render(<MetadataStrip meta={{}} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders key/value pairs for non-empty meta', () => {
    const { container } = render(
      <MetadataStrip meta={{ session_id: 12345, project_id: 'abc' }} />,
    )
    expect(container.textContent).toContain('session_id')
    expect(container.textContent).toContain('12345')
    expect(container.textContent).toContain('project_id')
    expect(container.textContent).toContain('abc')
  })
})

describe('GsqzResultBlock', () => {
  it('renders a metadata strip and routes the body for line-numbered Read output', () => {
    const numbered = '   1→from __future__ import annotations\n   2→import json'
    const { container } = render(
      <GsqzResultBlock
        metadata={{ chunkId: 'abc123', wallTimeSeconds: 0.5, exitCode: 0, tokenCount: 42 }}
        body={numbered}
      />,
    )
    expect(container.textContent).toContain('chunk abc123')
    expect(container.textContent).toContain('exit 0')
    expect(container.textContent).toContain('from __future__ import annotations')
  })

  it('shows non-zero exit codes with destructive styling', () => {
    const { container } = render(
      <GsqzResultBlock
        metadata={{ chunkId: 'def456', exitCode: 1 }}
        body="error output"
      />,
    )
    const strip = container.querySelector('div')
    expect(strip?.querySelector('div')?.className).toMatch(/destructive/)
  })
})

describe('ToolResultBody', () => {
  it('routes Read-style numbered output to a line-numbered renderer', () => {
    const numbered = '   1→hello\n   2→world'
    const { container } = render(<ToolResultBody body={numbered} />)
    expect(container.textContent).toContain('hello')
    expect(container.textContent).toContain('world')
  })

  it('routes JSON-shaped bodies through JsonResultBlock', () => {
    const body = '{"output":"a\\nb"}'
    const { container } = render(<ToolResultBody body={body} />)
    expect(container.textContent).toContain('a')
    expect(container.textContent).toContain('b')
    expect(container.textContent).not.toContain('\\n')
  })

  it('falls back to plain syntax-highlighted body for arbitrary text', () => {
    const { container } = render(<ToolResultBody body="plain output line" />)
    expect(container.textContent).toContain('plain output line')
  })
})
