export const TRANSCRIPT_PAGE_SIZE = 50
export const WINDOW_MAX_GROUPS = TRANSCRIPT_PAGE_SIZE * 5
export const START_INDEX = 1_000_000

export interface TranscriptWindowMessage {
  id: string
}

export interface TranscriptWindowPage<TMessage extends TranscriptWindowMessage> {
  messages: TMessage[]
  renderedTotal: number
  returnedCount: number
}

export interface TranscriptWindowState<TMessage extends TranscriptWindowMessage> {
  messages: TMessage[]
  windowStart: number
  windowEnd: number
  renderedTotal: number
  firstItemIndex: number
}

export interface TranscriptWindowUpdate<TMessage extends TranscriptWindowMessage> {
  state: TranscriptWindowState<TMessage>
  changed: boolean
  addedCount: number
  appendedCount: number
  replacedCount: number
  trimmedHeadCount: number
  trimmedTailCount: number
}

function normalizeCount(count: number): number {
  if (!Number.isFinite(count) || count <= 0) return 0
  return Math.floor(count)
}

function uniqueById<TMessage extends TranscriptWindowMessage>(
  messages: TMessage[],
): TMessage[] {
  const seen = new Set<string>()
  const unique: TMessage[] = []
  for (const message of messages) {
    if (seen.has(message.id)) continue
    seen.add(message.id)
    unique.push(message)
  }
  return unique
}

function currentIds<TMessage extends TranscriptWindowMessage>(
  messages: TMessage[],
): Set<string> {
  return new Set(messages.map((message) => message.id))
}

function replaceExisting<TMessage extends TranscriptWindowMessage>(
  messages: TMessage[],
  replacements: TMessage[],
): { messages: TMessage[]; replacedCount: number } {
  const replacementById = new Map(replacements.map((message) => [message.id, message]))
  let replacedCount = 0
  const nextMessages = messages.map((message) => {
    const replacement = replacementById.get(message.id)
    if (!replacement) return message
    replacedCount += 1
    return replacement
  })
  return { messages: nextMessages, replacedCount }
}

function updateResult<TMessage extends TranscriptWindowMessage>({
  state,
  previous,
  addedCount = 0,
  appendedCount = 0,
  replacedCount = 0,
  trimmedHeadCount = 0,
  trimmedTailCount = 0,
}: {
  state: TranscriptWindowState<TMessage>
  previous: TranscriptWindowState<TMessage>
  addedCount?: number
  appendedCount?: number
  replacedCount?: number
  trimmedHeadCount?: number
  trimmedTailCount?: number
}): TranscriptWindowUpdate<TMessage> {
  const changed =
    state !== previous &&
    (state.messages !== previous.messages ||
      state.windowStart !== previous.windowStart ||
      state.windowEnd !== previous.windowEnd ||
      state.renderedTotal !== previous.renderedTotal ||
      state.firstItemIndex !== previous.firstItemIndex)

  return {
    state,
    changed,
    addedCount,
    appendedCount,
    replacedCount,
    trimmedHeadCount,
    trimmedTailCount,
  }
}

export function createEmptyTranscriptWindow<TMessage extends TranscriptWindowMessage>(
  firstItemIndex = START_INDEX,
): TranscriptWindowState<TMessage> {
  return {
    messages: [],
    windowStart: 0,
    windowEnd: 0,
    renderedTotal: 0,
    firstItemIndex,
  }
}

export function createTailTranscriptWindow<TMessage extends TranscriptWindowMessage>(
  page: TranscriptWindowPage<TMessage>,
  firstItemIndex = START_INDEX,
  maxGroups = WINDOW_MAX_GROUPS,
): TranscriptWindowState<TMessage> {
  const renderedTotal = Math.max(
    normalizeCount(page.renderedTotal),
    normalizeCount(page.returnedCount),
    page.messages.length,
  )
  const uniqueMessages = uniqueById(page.messages)
  const trimmedHeadCount = Math.max(0, uniqueMessages.length - maxGroups)
  const messages =
    trimmedHeadCount > 0 ? uniqueMessages.slice(trimmedHeadCount) : uniqueMessages
  const windowEnd = renderedTotal
  const windowStart = Math.max(0, windowEnd - messages.length)

  return {
    messages,
    windowStart,
    windowEnd,
    renderedTotal,
    firstItemIndex: firstItemIndex + trimmedHeadCount,
  }
}

export function prependOlderTranscriptPage<TMessage extends TranscriptWindowMessage>(
  state: TranscriptWindowState<TMessage>,
  page: TranscriptWindowPage<TMessage>,
  maxGroups = WINDOW_MAX_GROUPS,
): TranscriptWindowUpdate<TMessage> {
  const ids = currentIds(state.messages)
  const olderMessages = uniqueById(page.messages).filter(
    (message) => !ids.has(message.id),
  )
  const renderedTotal = Math.max(state.renderedTotal, normalizeCount(page.renderedTotal))

  if (olderMessages.length === 0) {
    if (renderedTotal === state.renderedTotal) {
      return updateResult({ state, previous: state })
    }
    const nextState = { ...state, renderedTotal }
    return updateResult({ state: nextState, previous: state })
  }

  const untrimmedMessages = [...olderMessages, ...state.messages]
  const trimmedTailCount = Math.max(0, untrimmedMessages.length - maxGroups)
  const messages =
    trimmedTailCount > 0
      ? untrimmedMessages.slice(0, untrimmedMessages.length - trimmedTailCount)
      : untrimmedMessages
  const windowStart = Math.max(0, state.windowStart - olderMessages.length)
  const windowEnd = windowStart + messages.length
  const nextState = {
    messages,
    windowStart,
    windowEnd,
    renderedTotal: Math.max(renderedTotal, windowEnd),
    firstItemIndex: state.firstItemIndex - olderMessages.length,
  }

  return updateResult({
    state: nextState,
    previous: state,
    addedCount: olderMessages.length,
    trimmedTailCount,
  })
}

export function appendNewerTranscriptPage<TMessage extends TranscriptWindowMessage>(
  state: TranscriptWindowState<TMessage>,
  page: TranscriptWindowPage<TMessage>,
  maxGroups = WINDOW_MAX_GROUPS,
): TranscriptWindowUpdate<TMessage> {
  const ids = currentIds(state.messages)
  const newerMessages = uniqueById(page.messages).filter(
    (message) => !ids.has(message.id),
  )
  const renderedTotal = Math.max(state.renderedTotal, normalizeCount(page.renderedTotal))

  if (newerMessages.length === 0) {
    if (renderedTotal === state.renderedTotal) {
      return updateResult({ state, previous: state })
    }
    const nextState = { ...state, renderedTotal }
    return updateResult({ state: nextState, previous: state })
  }

  const untrimmedMessages = [...state.messages, ...newerMessages]
  const trimmedHeadCount = Math.max(0, untrimmedMessages.length - maxGroups)
  const messages =
    trimmedHeadCount > 0 ? untrimmedMessages.slice(trimmedHeadCount) : untrimmedMessages
  const windowStart = state.windowStart + trimmedHeadCount
  const windowEnd = windowStart + messages.length
  const nextState = {
    messages,
    windowStart,
    windowEnd,
    renderedTotal: Math.max(renderedTotal, windowEnd),
    firstItemIndex: state.firstItemIndex + trimmedHeadCount,
  }

  return updateResult({
    state: nextState,
    previous: state,
    addedCount: newerMessages.length,
    appendedCount: newerMessages.length,
    trimmedHeadCount,
  })
}

export function applyTailRefreshTranscriptPage<TMessage extends TranscriptWindowMessage>(
  state: TranscriptWindowState<TMessage>,
  page: TranscriptWindowPage<TMessage>,
  atBottom: boolean,
  maxGroups = WINDOW_MAX_GROUPS,
): TranscriptWindowUpdate<TMessage> {
  const refreshedMessages = uniqueById(page.messages)
  const replaced = replaceExisting(state.messages, refreshedMessages)
  const ids = currentIds(state.messages)
  const tailContiguous = state.windowEnd >= state.renderedTotal
  const shouldAppend = tailContiguous && atBottom
  const appendedMessages = shouldAppend
    ? refreshedMessages.filter((message) => !ids.has(message.id))
    : []
  const untrimmedMessages =
    appendedMessages.length > 0
      ? [...replaced.messages, ...appendedMessages]
      : replaced.messages
  const trimmedHeadCount = Math.max(0, untrimmedMessages.length - maxGroups)
  const messages =
    trimmedHeadCount > 0 ? untrimmedMessages.slice(trimmedHeadCount) : untrimmedMessages
  const renderedTotal = Math.max(
    state.renderedTotal,
    normalizeCount(page.renderedTotal),
    state.renderedTotal + appendedMessages.length,
  )
  const windowStart = state.windowStart + trimmedHeadCount
  const windowEnd = state.windowEnd + appendedMessages.length
  const nextState = {
    messages,
    windowStart,
    windowEnd,
    renderedTotal: Math.max(renderedTotal, windowEnd),
    firstItemIndex: state.firstItemIndex + trimmedHeadCount,
  }

  return updateResult({
    state: nextState,
    previous: state,
    addedCount: appendedMessages.length,
    appendedCount: appendedMessages.length,
    replacedCount: replaced.replacedCount,
    trimmedHeadCount,
  })
}

export function applyLiveTranscriptMessage<TMessage extends TranscriptWindowMessage>(
  state: TranscriptWindowState<TMessage>,
  message: TMessage,
  atBottom: boolean,
  maxGroups = WINDOW_MAX_GROUPS,
): TranscriptWindowUpdate<TMessage> {
  const existingIndex = state.messages.findIndex((current) => current.id === message.id)
  if (existingIndex >= 0) {
    const messages = [...state.messages]
    messages[existingIndex] = message
    const nextState = { ...state, messages }
    return updateResult({
      state: nextState,
      previous: state,
      replacedCount: 1,
    })
  }

  const renderedTotal = state.renderedTotal + 1
  const tailContiguous = state.windowEnd >= state.renderedTotal
  if (!tailContiguous || !atBottom) {
    const nextState = { ...state, renderedTotal }
    return updateResult({
      state: nextState,
      previous: state,
      addedCount: 1,
    })
  }

  const untrimmedMessages = [...state.messages, message]
  const trimmedHeadCount = Math.max(0, untrimmedMessages.length - maxGroups)
  const messages =
    trimmedHeadCount > 0 ? untrimmedMessages.slice(trimmedHeadCount) : untrimmedMessages
  const windowStart = state.windowStart + trimmedHeadCount
  const windowEnd = state.windowEnd + 1
  const nextState = {
    messages,
    windowStart,
    windowEnd,
    renderedTotal: Math.max(renderedTotal, windowEnd),
    firstItemIndex: state.firstItemIndex + trimmedHeadCount,
  }

  return updateResult({
    state: nextState,
    previous: state,
    addedCount: 1,
    appendedCount: 1,
    trimmedHeadCount,
  })
}
