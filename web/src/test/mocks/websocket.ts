import { vi } from 'vitest'

export interface MockWebSocketInstance {
  url: string
  readyState: number
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  onopen: ((ev: Event) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  onmessage: ((ev: MessageEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  addEventListener: ReturnType<typeof vi.fn>
  removeEventListener: ReturnType<typeof vi.fn>
  /** Simulate the server opening the connection */
  simulateOpen(): void
  /** Simulate the server closing the connection */
  simulateClose(code?: number, reason?: string): void
  /** Simulate a message from the server */
  simulateMessage(data: string | object): void
  /** Simulate an error */
  simulateError(): void
}

export function createMockWebSocket(): {
  instances: MockWebSocketInstance[]
  MockWebSocket: typeof WebSocket
  restore: () => void
} {
  const instances: MockWebSocketInstance[] = []
  const OriginalWebSocket = globalThis.WebSocket

  const MockWebSocket = vi.fn(function (this: MockWebSocketInstance, url: string) {
    this.url = url
    this.readyState = WebSocket.CONNECTING
    this.send = vi.fn()
    this.close = vi.fn(() => {
      this.readyState = WebSocket.CLOSED
    })
    this.onopen = null
    this.onclose = null
    this.onmessage = null
    this.onerror = null
    this.addEventListener = vi.fn()
    this.removeEventListener = vi.fn()

    this.simulateOpen = () => {
      this.readyState = WebSocket.OPEN
      this.onopen?.(new Event('open'))
    }

    this.simulateClose = (code = 1000, reason = '') => {
      this.readyState = WebSocket.CLOSED
      this.onclose?.(new CloseEvent('close', { code, reason }))
    }

    this.simulateMessage = (data: string | object) => {
      const payload = typeof data === 'object' ? JSON.stringify(data) : data
      this.onmessage?.(new MessageEvent('message', { data: payload }))
    }

    this.simulateError = () => {
      this.onerror?.(new Event('error'))
    }

    instances.push(this)
  }) as unknown as typeof WebSocket

  // Copy static constants
  Object.assign(MockWebSocket, {
    CONNECTING: 0,
    OPEN: 1,
    CLOSING: 2,
    CLOSED: 3,
  })

  globalThis.WebSocket = MockWebSocket

  return {
    instances,
    MockWebSocket,
    restore: () => {
      globalThis.WebSocket = OriginalWebSocket
    },
  }
}
