export function isSuccessfulToolCall(result: any): boolean {
  if (result?.success !== true) {
    return false;
  }

  const inner = result?.result;
  if (inner && typeof inner === "object" && "success" in inner) {
    return inner.success === true;
  }

  return true;
}

export function getToolCallError(result: any, fallback: string): string {
  if (typeof result?.error === "string" && result.error) {
    return result.error;
  }

  const inner = result?.result;
  if (inner && typeof inner === "object" && typeof inner.error === "string") {
    return inner.error;
  }

  return fallback;
}
