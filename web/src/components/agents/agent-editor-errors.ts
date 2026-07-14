export async function getAgentEditorResponseError(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body = await response.json() as Record<string, unknown>
    for (const key of ['detail', 'error', 'message']) {
      if (typeof body[key] === 'string' && body[key]) return body[key]
    }
  } catch {
    // The status text below still identifies non-JSON failures.
  }

  return response.statusText ? `${fallback}: ${response.statusText}` : fallback
}

export function getAgentEditorCaughtError(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}
