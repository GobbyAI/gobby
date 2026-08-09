import { useState, type FormEvent } from "react";

import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { Input } from "../../ui/Input";

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
        <Input
          id="terminal-input"
          wrapperClassName="min-w-0 flex-1"
          className="min-h-9 min-w-0 bg-[var(--bg-secondary)] px-3 py-2 font-mono text-sm"
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
        />
        <Button
          variant="primary"
          size="md"
          dense
          className={cn("min-h-9", coarseHitAreaCls)}
          type="submit"
          disabled={value.length === 0}
        >
          Send
        </Button>
      </div>
      <div
        className="flex flex-wrap gap-1.5"
        role="group"
        aria-label="Terminal quick keys"
      >
        {QUICK_KEYS.map(({ label, accessibleLabel, data }) => (
          <Button
            key={label}
            variant="secondary"
            size="sm"
            dense
            className={cn(
              "min-h-8 min-w-8 bg-[var(--bg-secondary)] px-2 font-mono text-xs active:bg-muted/80",
              coarseHitAreaCls,
            )}
            type="button"
            aria-label={accessibleLabel}
            onClick={() => sendInput(data)}
          >
            {label}
          </Button>
        ))}
      </div>
    </form>
  );
}
