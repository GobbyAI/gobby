import { describe, it, expect, afterEach } from 'vitest'
import { createMockWebSocket } from '../mocks/websocket'
import { createMockFetch } from '../mocks/fetch'
import { createMockLocalStorage } from '../mocks/localStorage'

describe('test utilities', () => {
  describe('mock WebSocket', () => {
    let restore: () => void

    afterEach(() => restore?.())

    it('creates mock instances and simulates messages', () => {
      const ws = createMockWebSocket()
      restore = ws.restore

      new WebSocket('ws://localhost:1234')
      expect(ws.instances).toHaveLength(1)
      expect(ws.instances[0].url).toBe('ws://localhost:1234')

      let received = ''
      ws.instances[0].onmessage = (ev) => {
        received = ev.data
      }
      ws.instances[0].simulateOpen()
      expect(ws.instances[0].readyState).toBe(WebSocket.OPEN)

      ws.instances[0].simulateMessage({ hello: 'world' })
      expect(received).toBe('{"hello":"world"}')

      ws.instances[0].simulateClose()
      expect(ws.instances[0].readyState).toBe(WebSocket.CLOSED)
    })
  })

  describe('mock fetch', () => {
    let mockFetch: ReturnType<typeof createMockFetch>

    afterEach(() => mockFetch?.restore())

    it('routes requests and returns JSON responses', async () => {
      mockFetch = createMockFetch()
      mockFetch.mockJsonResponse('/api/tasks', { tasks: [1, 2, 3] })

      const res = await fetch('/api/tasks')
      expect(res.ok).toBe(true)
      const data = await res.json()
      expect(data.tasks).toEqual([1, 2, 3])
    })

    it('returns 404 for unmatched routes', async () => {
      mockFetch = createMockFetch()
      const res = await fetch('/unknown')
      expect(res.status).toBe(404)
    })

    it('supports error responses', async () => {
      mockFetch = createMockFetch()
      mockFetch.mockErrorResponse('/api/fail', 500, 'Server Error')
      const res = await fetch('/api/fail')
      expect(res.status).toBe(500)
    })
  })

  describe('mock localStorage', () => {
    let mockStorage: ReturnType<typeof createMockLocalStorage>

    afterEach(() => mockStorage?.restore())

    it('supports get/set/remove/clear operations', () => {
      mockStorage = createMockLocalStorage()

      localStorage.setItem('key1', 'value1')
      expect(localStorage.getItem('key1')).toBe('value1')
      expect(mockStorage.length).toBe(1)

      localStorage.setItem('key2', 'value2')
      expect((mockStorage.key as (index: number) => string | null)(0)).toBe('key1')
      expect(mockStorage.length).toBe(2)

      localStorage.removeItem('key1')
      expect(localStorage.getItem('key1')).toBeNull()
      expect(mockStorage.length).toBe(1)

      localStorage.clear()
      expect(mockStorage.length).toBe(0)
    })
  })
})
