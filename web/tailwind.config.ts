import type { Config } from 'tailwindcss'

export default {
  content: ['./src/**/*.{ts,tsx}'],
  important: true,
  theme: {
    extend: {
      colors: {
        background: 'var(--bg-primary)',
        foreground: 'var(--text-primary)',
        muted: { DEFAULT: 'var(--bg-tertiary)', foreground: 'var(--text-secondary)' },
        accent: { DEFAULT: 'var(--accent)', foreground: 'var(--accent-foreground)', hover: 'var(--accent-hover)' },
        border: 'var(--border)',
        destructive: { DEFAULT: 'var(--color-destructive)', foreground: 'var(--color-destructive-foreground)' },
        warning: { DEFAULT: 'var(--color-warning)', foreground: 'var(--color-warning-foreground)' },
        success: { DEFAULT: 'var(--color-success)', foreground: 'var(--color-success-foreground)' },
        error: { DEFAULT: 'var(--color-error)', soft: 'var(--color-error-soft)' },
        info: { DEFAULT: 'var(--color-info)', soft: 'var(--color-info-soft)' },
        agent: { DEFAULT: 'var(--color-agent)', soft: 'var(--color-agent-soft)' },
        review: { DEFAULT: 'var(--color-review)', soft: 'var(--color-review-soft)' },
        'surface-tint-subtle': 'var(--surface-tint-subtle)',
        'surface-tint': 'var(--surface-tint)',
        'surface-tint-strong': 'var(--surface-tint-strong)',
      },
      fontSize: {
        '2xs': ['var(--text-2xs)', { lineHeight: '1.25' }],
        xs: ['var(--text-xs)', { lineHeight: '1.25' }],
        sm: ['var(--text-sm)', { lineHeight: '1.25' }],
        md: ['var(--text-md)', { lineHeight: '1.25' }],
        base: ['var(--text-base)', { lineHeight: '1.4' }],
        lg: ['var(--text-lg)', { lineHeight: '1.4' }],
        xl: ['var(--text-xl)', { lineHeight: '1.2' }],
        '2xl': ['var(--text-2xl)', { lineHeight: '1.2' }],
        '3xl': ['var(--text-3xl)', { lineHeight: '1.2' }],
        '4xl': ['var(--text-4xl)', { lineHeight: '1.2' }],
      },
      fontWeight: {
        normal: 'var(--font-weight-normal)',
        medium: 'var(--font-weight-medium)',
        semibold: 'var(--font-weight-semibold)',
        bold: 'var(--font-weight-bold)',
      },
    },
  },
} satisfies Config
