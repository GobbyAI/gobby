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
      },
      fontSize: {
        '2xs': 'var(--text-2xs)',
        xs: 'var(--text-xs)',
        sm: 'var(--text-sm)',
        md: 'var(--text-md)',
        base: 'var(--text-base)',
        lg: 'var(--text-lg)',
        xl: 'var(--text-xl)',
        '2xl': 'var(--text-2xl)',
        '3xl': 'var(--text-3xl)',
        '4xl': 'var(--text-4xl)',
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
