function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isSuccessfulToolCall(result: unknown): boolean {
  if (!isRecord(result) || result.success !== true) {
    return false;
  }

  const inner = result.result;
  if (isRecord(inner) && "success" in inner) {
    return inner.success === true;
  }

  return true;
}

export function getToolCallError(result: unknown, fallback: string): string {
  if (!isRecord(result)) {
    return fallback;
  }

  if (typeof result.error === "string" && result.error) {
    return result.error;
  }

  const inner = result.result;
  if (isRecord(inner) && typeof inner.error === "string") {
    return inner.error;
  }

  return fallback;
}
