export interface PendingProxyMessage {
  clientMessageId: string;
  currentMessageId: string;
  sessionId: string;
}

export function enqueuePendingProxyMessage(
  pendingQueues: Map<string, string[]>,
  entry: PendingProxyMessage,
): void {
  const queue = pendingQueues.get(entry.sessionId) ?? [];
  queue.push(entry.clientMessageId);
  pendingQueues.set(entry.sessionId, queue);
}

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

  pendingQueues.delete(sessionId);
  return null;
}

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

export function clearPendingProxyMessages(
  pending: Map<string, PendingProxyMessage>,
  pendingQueues: Map<string, string[]>,
): void {
  pending.clear();
  pendingQueues.clear();
}
