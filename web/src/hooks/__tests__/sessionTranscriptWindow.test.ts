import { describe, expect, it } from 'vitest'
import {
  START_INDEX,
  appendNewerTranscriptPage,
  applyLiveTranscriptMessage,
  applyTailRefreshTranscriptPage,
  createTailTranscriptWindow,
  prependOlderTranscriptPage,
} from '../sessionTranscriptWindow'

type TestMessage = {
  id: string
  content: string
}

function message(index: number, content = `message-${index}`): TestMessage {
  return { id: `msg-${index}`, content }
}

function messages(start: number, end: number): TestMessage[] {
  return Array.from({ length: end - start }, (_, offset) => message(start + offset))
}

function page(start: number, end: number, renderedTotal: number) {
  const pageMessages = messages(start, end)
  return {
    messages: pageMessages,
    renderedTotal,
    returnedCount: pageMessages.length,
  }
}

describe('sessionTranscriptWindow', () => {
  it('prepends older pages and evicts the tail edge', () => {
    const state = createTailTranscriptWindow(page(5, 10, 10), START_INDEX, 5)

    const update = prependOlderTranscriptPage(state, page(0, 5, 10), 5)

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-0',
      'msg-1',
      'msg-2',
      'msg-3',
      'msg-4',
    ])
    expect(update.state.windowStart).toBe(0)
    expect(update.state.windowEnd).toBe(5)
    expect(update.trimmedTailCount).toBe(5)
    expect(update.state.firstItemIndex).toBe(START_INDEX - 5)
  })

  it('appends newer pages and evicts the head edge', () => {
    const tail = createTailTranscriptWindow(page(5, 10, 10), START_INDEX, 5)
    const older = prependOlderTranscriptPage(tail, page(0, 5, 10), 5).state

    const update = appendNewerTranscriptPage(older, page(5, 10, 10), 5)

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-5',
      'msg-6',
      'msg-7',
      'msg-8',
      'msg-9',
    ])
    expect(update.state.windowStart).toBe(5)
    expect(update.state.windowEnd).toBe(10)
    expect(update.trimmedHeadCount).toBe(5)
    expect(update.state.firstItemIndex).toBe(START_INDEX)
  })

  it('refetches the reverse edge without duplicates after overlap', () => {
    const state = createTailTranscriptWindow(page(2, 4, 4), START_INDEX, 5)

    const update = prependOlderTranscriptPage(
      state,
      {
        messages: [message(1), message(2, 'duplicate')],
        renderedTotal: 4,
        returnedCount: 2,
      },
      5,
    )

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-1',
      'msg-2',
      'msg-3',
    ])
    expect(update.state.windowStart).toBe(1)
    expect(update.state.windowEnd).toBe(4)
    expect(update.state.firstItemIndex).toBe(START_INDEX - 1)
  })

  it('keeps tail refresh counts only when the loaded window is away from tail', () => {
    const tail = createTailTranscriptWindow(page(5, 10, 10), START_INDEX, 5)
    const older = prependOlderTranscriptPage(tail, page(0, 5, 10), 5).state

    const update = applyTailRefreshTranscriptPage(
      older,
      page(8, 11, 11),
      true,
      5,
    )

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-0',
      'msg-1',
      'msg-2',
      'msg-3',
      'msg-4',
    ])
    expect(update.state.windowStart).toBe(0)
    expect(update.state.windowEnd).toBe(5)
    expect(update.state.renderedTotal).toBe(11)
    expect(update.appendedCount).toBe(0)
  })

  it('merges tail refresh rows and trims the head while at bottom', () => {
    const state = createTailTranscriptWindow(page(5, 10, 10), START_INDEX, 5)

    const update = applyTailRefreshTranscriptPage(
      state,
      {
        messages: [message(8, 'refreshed'), message(9), message(10)],
        renderedTotal: 11,
        returnedCount: 3,
      },
      true,
      5,
    )

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-6',
      'msg-7',
      'msg-8',
      'msg-9',
      'msg-10',
    ])
    expect(update.state.messages[2].content).toBe('refreshed')
    expect(update.state.windowStart).toBe(6)
    expect(update.state.windowEnd).toBe(11)
    expect(update.state.firstItemIndex).toBe(START_INDEX + 1)
  })

  it('advances live message totals without appending away from tail', () => {
    const tail = createTailTranscriptWindow(page(5, 10, 10), START_INDEX, 5)
    const older = prependOlderTranscriptPage(tail, page(0, 5, 10), 5).state

    const update = applyLiveTranscriptMessage(older, message(10), true, 5)

    expect(update.state.messages.map((item) => item.id)).toEqual([
      'msg-0',
      'msg-1',
      'msg-2',
      'msg-3',
      'msg-4',
    ])
    expect(update.state.renderedTotal).toBe(11)
    expect(update.appendedCount).toBe(0)
    expect(update.addedCount).toBe(1)
  })
})
