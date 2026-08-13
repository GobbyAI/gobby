import { useState, useCallback, type FormEvent } from "react";
import { GobbyLogo } from "../shared/GobbyLogo";
import { Heading } from "../shared/Heading";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";

interface LoginPageProps {
  onLogin: (
    email: string,
    password: string,
    rememberMe: boolean,
  ) => Promise<string | null>;
}

export function LoginPage({ onLogin }: LoginPageProps): JSX.Element {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setError(null);
      setLoading(true);
      const err = await onLogin(email, password, rememberMe);
      setLoading(false);
      if (err) setError(err);
    },
    [email, password, rememberMe, onLogin],
  );

  return (
    <div style={styles.container}>
      <form onSubmit={handleSubmit} style={styles.card}>
        <div style={styles.logoRow}>
          <GobbyLogo label="Gobby" size={36} />
          <Heading level={1} style={styles.title}>
            Gobby
          </Heading>
        </div>
        <p style={styles.subtitle}>Sign in to continue</p>

        {error && <div style={styles.error}>{error}</div>}

        <div style={styles.label}>
          <label htmlFor="login-email">Email</label>
          <Input
            id="login-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
            autoComplete="email"
            required
            style={styles.input}
          />
        </div>

        <div style={styles.label}>
          <label htmlFor="login-password">Password</label>
          <Input
            id="login-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
            style={styles.input}
          />
        </div>

        <div style={styles.checkboxLabel}>
          <Input
            id="login-remember-me"
            type="checkbox"
            wrapperClassName="w-auto shrink-0"
            className="h-4 w-4 shrink-0 rounded border-border p-0 accent-accent"
            checked={rememberMe}
            onChange={(e) => setRememberMe(e.target.checked)}
          />
          <label htmlFor="login-remember-me">Remember me for 30 days</label>
        </div>

        <Button
          type="submit"
          variant="primary"
          disabled={loading || !email || !password}
          style={styles.button}
        >
          {loading ? "Signing in..." : "Sign in"}
        </Button>
      </form>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    minHeight: "100vh",
    background: "var(--bg-primary)",
    padding: "1rem",
  },
  card: {
    display: "flex",
    flexDirection: "column",
    gap: "1rem",
    width: "100%",
    maxWidth: 360,
    padding: "2rem",
    background: "var(--bg-secondary)",
    border: "1px solid var(--border)",
    borderRadius: 12,
  },
  logoRow: {
    display: "flex",
    alignItems: "center",
    gap: "0.75rem",
    justifyContent: "center",
  },
  title: {
    margin: 0,
    color: "var(--text-primary)",
    fontSize: "var(--text-3xl)",
    fontWeight: "var(--font-weight-bold)",
  },
  subtitle: {
    margin: 0,
    textAlign: "center" as const,
    color: "var(--text-secondary)",
    fontSize: "var(--text-base)",
  },
  error: {
    padding: "0.5rem 0.75rem",
    borderRadius: 6,
    background: "var(--color-error-soft)",
    color: "var(--color-error)",
    fontSize: "var(--text-base)",
    textAlign: "center" as const,
  },
  label: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "0.35rem",
    fontSize: "var(--text-base)",
    fontWeight: 500,
    color: "var(--text-secondary)",
  },
  // Geometry, chrome, and type scale come from the Input primitive (its
  // error border state must stay live); only the text color is local.
  input: {
    color: "var(--text-primary)",
  },
  checkboxLabel: {
    display: "flex",
    alignItems: "center",
    gap: "0.5rem",
    fontSize: "var(--text-base)",
    color: "var(--text-secondary)",
    cursor: "pointer",
  },
  // Chrome, type scale, cursor, and disabled dimming come from the primary
  // Button primitive; the block padding (taller than md's min-height
  // floor) and offset are local.
  button: {
    paddingBlock: "0.6rem",
    marginTop: "0.25rem",
  },
};
