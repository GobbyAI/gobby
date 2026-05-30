/** Message awaiting proxy-session confirmation before it should appear in chat. */
export interface PendingProxyMessage {
  clientMessageId: string;
  currentMessageId: string;
  sessionId: string;
}

/** Append a client message id to the per-session FIFO queue. Mutates pendingQueues. */
export function enqueuePendingProxyMessage(
  pendingQueues: Map<string, string[]>,
  entry: PendingProxyMessage,
): void {
  const queue = pendingQueues.get(entry.sessionId) ?? [];
  queue.push(entry.clientMessageId);
  pendingQueues.set(entry.sessionId, queue);
}

/** Remove and return the next pending message for a session. Mutates pendingQueues only. */
export function consumePendingProxyMessage(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
  sessionId: string,
): PendingProxyMessage | null {
  const queue = pendingQueues.get(sessionId);
  if (!queue) {
    return null;
  }

  while (queue.length > 0) {
    const clientMessageId = queue.shift();
    if (!clientMessageId) {
      continue;
    }
    const entry = pending.get(clientMessageId) ?? null;
    if (entry) {
      if (queue.length === 0) {
        pendingQueues.delete(sessionId);
      } else {
        pendingQueues.set(sessionId, queue);
      }
      return entry;
    }
  }

  // Every queued id was already removed from the pending map; drop the stale queue.
  pendingQueues.delete(sessionId);
  return null;
}

/** Remove one queued client message id after cancellation or failure. Mutates pendingQueues. */
export function removePendingProxyMessageFromQueue(
  pendingQueues: Map<string, string[]>,
  sessionId: string,
  clientMessageId: string,
): void {
  const queue = pendingQueues.get(sessionId);
  if (!queue) {
    return;
  }

  const next = queue.filter((id) => id !== clientMessageId);
  if (next.length === 0) {
    pendingQueues.delete(sessionId);
    return;
  }
  pendingQueues.set(sessionId, next);
}

/** Drop all pending proxy messages and session queues. Mutates both maps. */
export function clearPendingProxyMessages(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
): void {
  pending.clear();
  pendingQueues.clear();
}
