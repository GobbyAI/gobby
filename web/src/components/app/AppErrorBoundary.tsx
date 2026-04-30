import { Component, type ReactNode } from "react";

export class AppErrorBoundary extends Component<
  { children: ReactNode; activeTab: string; onReturnToChat: () => void },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: {
    children: ReactNode;
    activeTab: string;
    onReturnToChat: () => void;
  }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(
      "[AppErrorBoundary] Caught error in tab:",
      this.props.activeTab,
      error,
      info,
    );
  }

  componentDidUpdate(prevProps: { activeTab: string }) {
    if (prevProps.activeTab !== this.props.activeTab && this.state.hasError) {
      this.setState({ hasError: false, error: null });
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <main
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            flex: 1,
            gap: "1rem",
            padding: "2rem",
            color: "var(--text-secondary)",
          }}
        >
          <div
            style={{
              fontSize: "1.25rem",
              color: "var(--text-primary)",
              fontWeight: 600,
            }}
          >
            Something went wrong
          </div>
          <div
            style={{
              fontSize: "0.85rem",
              maxWidth: 480,
              textAlign: "center",
              lineHeight: 1.5,
            }}
          >
            An error occurred in the <b>{this.props.activeTab}</b> tab. This is
            usually caused by a rendering failure in a third-party library.
          </div>
          {this.state.error && (
            <code
              style={{
                fontSize: "0.75rem",
                color: "var(--text-muted)",
                background: "var(--bg-secondary)",
                padding: "0.5rem 1rem",
                borderRadius: 4,
                maxWidth: 600,
                overflow: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {this.state.error.message}
            </code>
          )}
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              style={{
                padding: "0.4rem 1rem",
                borderRadius: 4,
                border: "1px solid var(--border)",
                background: "var(--bg-secondary)",
                color: "var(--text-primary)",
                cursor: "pointer",
                fontSize: "0.8rem",
              }}
            >
              Try Again
            </button>
            <button
              onClick={this.props.onReturnToChat}
              style={{
                padding: "0.4rem 1rem",
                borderRadius: 4,
                border: "none",
                background: "var(--accent)",
                color: "var(--accent-foreground)",
                cursor: "pointer",
                fontSize: "0.8rem",
              }}
            >
              Return to Chat
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}
