import { vi, type Mock } from 'vitest'

type MockResponseInit = {
  status?: number
  statusText?: string
  headers?: Record<string, string>
}

export interface MockFetchInstance {
  /** The underlying vi.fn — use for assertions */
  fn: Mock
  /** Register a JSON response for a URL pattern */
  mockJsonResponse(urlPattern: string | RegExp, data: unknown, init?: MockResponseInit): void
  /** Register an error response */
  mockErrorResponse(urlPattern: string | RegExp, status: number, body?: string): void
  /** Reset all mocked routes */
  resetRoutes(): void
  /** Restore original fetch */
  restore(): void
}

interface Route {
  pattern: string | RegExp
  response: () => Response
}

export function createMockFetch(): MockFetchInstance {
  const routes: Route[] = []
  const originalFetch = globalThis.fetch

  const fn = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url

    for (const route of routes) {
      const matches =
        typeof route.pattern === 'string'
          ? url.includes(route.pattern)
          : route.pattern.test(url)
      if (matches) return route.response()
    }

    return new Response(JSON.stringify({ error: 'no mock route matched' }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    })
  })

  globalThis.fetch = fn as unknown as typeof fetch

  return {
    fn,
    mockJsonResponse(urlPattern, data, init) {
      routes.push({
        pattern: urlPattern,
        response: () =>
          new Response(JSON.stringify(data), {
            status: init?.status ?? 200,
            statusText: init?.statusText ?? 'OK',
            headers: { 'Content-Type': 'application/json', ...init?.headers },
          }),
      })
    },
    mockErrorResponse(urlPattern, status, body) {
      routes.push({
        pattern: urlPattern,
        response: () =>
          new Response(body ?? `Error ${status}`, {
            status,
            statusText: `Error ${status}`,
          }),
      })
    },
    resetRoutes() {
      routes.length = 0
      fn.mockClear()
    },
    restore() {
      globalThis.fetch = originalFetch
    },
  }
}
