import { useState, useCallback, type FormEvent } from 'react'
import { GobbyLogo } from '../shared/GobbyLogo'
import { Heading } from '../shared/Heading'

interface LoginPageProps {
  onLogin: (username: string, password: string, rememberMe: boolean) => Promise<string | null>
}

export function LoginPage({ onLogin }: LoginPageProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [rememberMe, setRememberMe] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const handleSubmit = useCallback(async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setLoading(true)
    const err = await onLogin(username, password, rememberMe)
    setLoading(false)
    if (err) setError(err)
  }, [username, password, rememberMe, onLogin])

  return (
    <div style={styles.container}>
      <form onSubmit={handleSubmit} style={styles.card}>
        <div style={styles.logoRow}>
          <GobbyLogo label="Gobby" size={36} />
          <Heading level={1} style={styles.title}>Gobby</Heading>
        </div>
        <p style={styles.subtitle}>Sign in to continue</p>

        {error && <div style={styles.error}>{error}</div>}

        <label style={styles.label}>
          Username
          <input
            type="text"
            value={username}
            onChange={e => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            required
            style={styles.input}
          />
        </label>

        <label style={styles.label}>
          Password
          <input
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoComplete="current-password"
            required
            style={styles.input}
          />
        </label>

        <label style={styles.checkboxLabel}>
          <input
            type="checkbox"
            checked={rememberMe}
            onChange={e => setRememberMe(e.target.checked)}
          />
          <span>Remember me for 30 days</span>
        </label>

        <button
          type="submit"
          disabled={loading || !username || !password}
          style={{
            ...styles.button,
            opacity: loading || !username || !password ? 0.6 : 1,
          }}
        >
          {loading ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: '100vh',
    background: 'var(--bg-primary)',
    padding: '1rem',
  },
  card: {
    display: 'flex',
    flexDirection: 'column',
    gap: '1rem',
    width: '100%',
    maxWidth: 360,
    padding: '2rem',
    background: 'var(--bg-secondary)',
    border: '1px solid var(--border)',
    borderRadius: 12,
  },
  logoRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '0.75rem',
    justifyContent: 'center',
  },
  title: {
    margin: 0,
    color: 'var(--text-primary)',
    fontSize: 'var(--text-3xl)',
    fontWeight: 'var(--font-weight-bold)',
  },
  subtitle: {
    margin: 0,
    textAlign: 'center' as const,
    color: 'var(--text-secondary)',
    fontSize: '0.9rem',
  },
  error: {
    padding: '0.5rem 0.75rem',
    borderRadius: 6,
    background: 'var(--color-error-soft)',
    color: 'var(--color-error)',
    fontSize: '0.85rem',
    textAlign: 'center' as const,
  },
  label: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '0.35rem',
    fontSize: '0.85rem',
    fontWeight: 500,
    color: 'var(--text-secondary)',
  },
  input: {
    padding: '0.55rem 0.75rem',
    borderRadius: 6,
    border: '1px solid var(--border)',
    background: 'var(--bg-primary)',
    color: 'var(--text-primary)',
    fontSize: '0.95rem',
    outline: 'none',
  },
  checkboxLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
    fontSize: '0.85rem',
    color: 'var(--text-secondary)',
    cursor: 'pointer',
  },
  button: {
    padding: '0.6rem',
    borderRadius: 6,
    border: 'none',
    background: 'var(--accent)',
    color: 'var(--accent-foreground)',
    fontSize: '0.95rem',
    fontWeight: 600,
    cursor: 'pointer',
    marginTop: '0.25rem',
  },
}
