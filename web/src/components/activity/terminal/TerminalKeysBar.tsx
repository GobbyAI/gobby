import { useState, type FormEvent } from "react";

interface TerminalKeysBarProps {
  sendInput: (data: string) => void;
}

interface QuickKey {
  label: string;
  accessibleLabel?: string;
  data: string;
}

const QUICK_KEYS: readonly QuickKey[] = [
  { label: "Esc", data: "\x1b" },
  { label: "Tab", data: "\t" },
  { label: "Enter", data: "\r" },
  { label: "↑", accessibleLabel: "Up", data: "\x1b[A" },
  { label: "↓", accessibleLabel: "Down", data: "\x1b[B" },
  { label: "Ctrl+C", data: "\x03" },
  { label: "1", data: "1" },
  { label: "2", data: "2" },
  { label: "3", data: "3" },
];

export function TerminalKeysBar({ sendInput }: TerminalKeysBarProps) {
  const [value, setValue] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (value.length === 0) return;
    sendInput(`${value}\r`);
    setValue("");
  };

  return (
    <form className="flex min-w-0 flex-col gap-2" onSubmit={submit}>
      <label className="text-xs font-medium text-muted-foreground" htmlFor="terminal-input">
        Terminal input
      </label>
      <div className="flex min-w-0 gap-2">
        <input
          id="terminal-input"
          className="min-h-9 min-w-0 flex-1 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2 font-mono text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background pointer-coarse:min-h-11"
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <button
          className="min-h-9 rounded-md bg-accent px-3 text-sm font-medium text-accent-foreground transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
          type="submit"
          disabled={value.length === 0}
        >
          Send
        </button>
      </div>
      <div className="flex flex-wrap gap-1.5" aria-label="Terminal quick keys">
        {QUICK_KEYS.map(({ label, accessibleLabel, data }) => (
          <button
            key={label}
            className="min-h-8 min-w-8 rounded-md border border-border bg-[var(--bg-secondary)] px-2 font-mono text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background active:bg-muted/80 pointer-coarse:min-h-11 pointer-coarse:min-w-11"
            type="button"
            aria-label={accessibleLabel}
            onClick={() => sendInput(data)}
          >
            {label}
          </button>
        ))}
      </div>
    </form>
  );
}
