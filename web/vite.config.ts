/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const GOBBY_HTTP_PORT = process.env.GOBBY_DAEMON_PORT || '60887'
const GOBBY_WS_PORT = process.env.GOBBY_WS_PORT || '60888'
const GOBBY_UI_HOST = process.env.GOBBY_UI_HOST || 'localhost'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    dedupe: ['@codemirror/state', '@codemirror/view', '@codemirror/language'],
  },
  server: {
    host: GOBBY_UI_HOST,
    port: 60889,
    allowedHosts: GOBBY_UI_HOST === '0.0.0.0' ? true : ['localhost', '.ts.net', ...(process.env.VITE_ALLOWED_HOST ? [process.env.VITE_ALLOWED_HOST] : [])],
    proxy: {
      // Proxy API requests to Gobby daemon
      ...Object.fromEntries(
        ['/api', '/mcp', '/admin', '/tasks', '/sessions', '/memories', '/skills'].map(
          (path) => [path, { target: `http://localhost:${GOBBY_HTTP_PORT}`, changeOrigin: true }]
        )
      ),
      // Proxy WebSocket to Gobby WebSocket server
      '/ws': {
        target: `ws://localhost:${GOBBY_WS_PORT}`,
        ws: true,
        rewriteWsOrigin: true,
        rewrite: (path) => path.replace(/^\/ws/, ''),
      },
    },
  },
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary', 'lcov'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/__tests__/**',
        'src/test/**',
        'src/setup/**',
        'src/vite-env.d.ts',
      ],
      thresholds: {
        lines: 80,
        branches: 80,
        functions: 80,
        statements: 80,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1300, // Largest chunks: KnowledgeGraph (1.2MB Three.js, lazy-loaded), vendor-syntax (632KB), vendor-codemirror (586KB) — all irreducible
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('@codemirror') || id.includes('codemirror')) return 'vendor-codemirror'
            if (id.includes('@xterm') || id.includes('xterm')) return 'vendor-xterm'
            if (id.includes('react-syntax-highlighter') || id.includes('refractor') || id.includes('prismjs') || id.includes('highlight.js')) return 'vendor-syntax'
            if (id.includes('react-markdown') || id.includes('remark') || id.includes('rehype') || id.includes('unified') || id.includes('mdast') || id.includes('hast') || id.includes('micromark') || id.includes('marked')) return 'vendor-markdown'
            if (id.includes('d3-') || id.includes('@dagrejs')) return 'vendor-d3'
          }
        },
      },
    },
  },
})
