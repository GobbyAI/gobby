import { useMemo, useRef, useState } from "react";

import {
  useWiki,
  type WikiCompileRequest,
  type WikiEnvelope,
  type WikiIngestRequest,
} from "../../hooks/useWiki";
import { WikiActionResult, type WikiActionKind, type WikiActionResultState } from "./WikiActionResult";

interface WikiChatActionsProps {
  projectId?: string | null;
  disabled?: boolean;
  onActionComplete?: () => void | Promise<void>;
}

interface WikiActionDefinition {
  kind: WikiActionKind;
  label: string;
  title: string;
  inputLabel: string;
  placeholder: string;
  requiresInput: boolean;
  requiresIntent: boolean;
}

const ACTIONS: WikiActionDefinition[] = [
  {
    kind: "search",
    label: "Search wiki",
    title: "Wiki search",
    inputLabel: "Search query",
    placeholder: "hooks",
    requiresInput: true,
    requiresIntent: false,
  },
  {
    kind: "read",
    label: "Read wiki",
    title: "Wiki read",
    inputLabel: "Wiki path or title",
    placeholder: "wiki/hooks.md",
    requiresInput: true,
    requiresIntent: false,
  },
  {
    kind: "attach",
    label: "Attach file",
    title: "Wiki attach",
    inputLabel: "File",
    placeholder: "",
    requiresInput: true,
    requiresIntent: true,
  },
  {
    kind: "ingest",
    label: "Ingest URLs",
    title: "Wiki ingest",
    inputLabel: "URLs or file paths",
    placeholder: "https://example.test/a\nhttps://example.test/b",
    requiresInput: true,
    requiresIntent: true,
  },
  {
    kind: "compile",
    label: "Compile wiki",
    title: "Wiki compile",
    inputLabel: "Output path",
    placeholder: "Optional output path",
    requiresInput: false,
    requiresIntent: true,
  },
  {
    kind: "audit",
    label: "Audit wiki",
    title: "Wiki audit",
    inputLabel: "Audit",
    placeholder: "",
    requiresInput: false,
    requiresIntent: false,
  },
  {
    kind: "health",
    label: "Wiki health",
    title: "Wiki health",
    inputLabel: "Health",
    placeholder: "",
    requiresInput: false,
    requiresIntent: false,
  },
];

function parseLines(value: string): string[] {
  return value
    .split(/\s+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function buildIngestRequest(value: string): WikiIngestRequest {
  const entries = parseLines(value);
  const urls = entries.filter((entry) => /^https?:\/\//i.test(entry));
  if (urls.length) return { urls };
  return entries.length > 1 ? { paths: entries } : { path: entries[0] };
}

function buildCompileRequest(value: string): WikiCompileRequest {
  const output = value.trim();
  return output ? { output } : {};
}

function readSelector(value: string) {
  const selector = value.trim();
  return selector.includes("/") || selector.endsWith(".md")
    ? { path: selector }
    : { title: selector };
}

export function WikiChatActions({
  projectId,
  disabled = false,
  onActionComplete,
}: WikiChatActionsProps) {
  const wiki = useWiki({ projectId });
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [activeKind, setActiveKind] = useState<WikiActionKind | null>(null);
  const [input, setInput] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [pendingIntent, setPendingIntent] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WikiActionResultState | null>(null);

  const activeAction = useMemo(
    () => ACTIONS.find((action) => action.kind === activeKind) ?? null,
    [activeKind],
  );

  const selectAction = (action: WikiActionDefinition) => {
    setActiveKind(action.kind);
    setInput("");
    setSelectedFile(null);
    setPendingIntent(action.requiresIntent);
    setError(null);
    setResult(null);
  };

  const complete = async (action: WikiActionDefinition, envelope: WikiEnvelope) => {
    setResult({ kind: action.kind, title: action.title, envelope });
    await onActionComplete?.();
  };

  const runAction = async () => {
    if (!activeAction || isRunning) return;
    if (activeAction.requiresIntent && pendingIntent) {
      setPendingIntent(false);
    }
    if (activeAction.requiresInput && activeAction.kind !== "attach" && !input.trim()) {
      setError(`${activeAction.inputLabel} is required`);
      return;
    }
    if (activeAction.kind === "attach" && !selectedFile) {
      setError("Choose a file to attach");
      return;
    }

    setIsRunning(true);
    setError(null);
    try {
      let envelope: WikiEnvelope;
      if (activeAction.kind === "search") {
        envelope = await wiki.search({ query: input.trim() });
      } else if (activeAction.kind === "read") {
        envelope = await wiki.read(readSelector(input));
      } else if (activeAction.kind === "attach") {
        envelope = await wiki.attach(selectedFile as File);
      } else if (activeAction.kind === "ingest") {
        envelope = await wiki.ingest(buildIngestRequest(input));
      } else if (activeAction.kind === "compile") {
        envelope = await wiki.compileWiki(buildCompileRequest(input));
      } else if (activeAction.kind === "audit") {
        envelope = await wiki.audit();
      } else {
        envelope = await wiki.checkHealth();
      }
      await complete(activeAction, envelope);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : String(nextError));
    } finally {
      setIsRunning(false);
    }
  };

  const runLabel = pendingIntent ? "Run wiki write" : "Run wiki action";

  return (
    <div className="relative">
      <button
        type="button"
        className="inline-flex h-[36px] items-center gap-1 rounded-md border border-border px-2 text-xs text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
        aria-label="Wiki actions"
        title="Wiki actions"
        disabled={disabled}
        onClick={() => setIsOpen((open) => !open)}
      >
        <span aria-hidden="true">Wiki</span>
      </button>
      {isOpen ? (
        <div className="absolute left-0 top-10 z-30 w-[min(360px,calc(100vw-2rem))] rounded-md border border-border bg-background p-3 shadow-lg">
          <div className="grid grid-cols-2 gap-2">
            {ACTIONS.map((action) => (
              <button
                key={action.kind}
                type="button"
                className="rounded-md border border-border px-2 py-1.5 text-left text-xs text-foreground hover:bg-muted"
                onClick={() => selectAction(action)}
              >
                {action.label}
              </button>
            ))}
          </div>

          {activeAction ? (
            <div className="mt-3 space-y-2">
              {pendingIntent ? (
                <div className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-xs text-warning-foreground">
                  Confirm wiki write
                </div>
              ) : null}
              {activeAction.kind === "attach" ? (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="rounded-md border border-border px-2 py-1.5 text-xs text-foreground hover:bg-muted"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    Choose file
                  </button>
                  <span className="min-w-0 truncate text-xs text-muted-foreground">
                    {selectedFile?.name ?? "No file selected"}
                  </span>
                  <input
                    ref={fileInputRef}
                    type="file"
                    className="hidden"
                    onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
                  />
                </div>
              ) : activeAction.requiresInput || activeAction.kind === "compile" ? (
                <textarea
                  aria-label="Wiki action input"
                  className="min-h-[68px] w-full resize-none rounded-md border border-border bg-muted px-2 py-1.5 text-xs text-foreground outline-none focus:ring-2 focus:ring-accent"
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder={activeAction.placeholder}
                  rows={3}
                />
              ) : null}
              <button
                type="button"
                className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground hover:bg-accent-hover disabled:pointer-events-none disabled:opacity-50"
                onClick={() => void runAction()}
                disabled={isRunning}
              >
                {isRunning ? "Running" : runLabel}
              </button>
              {error ? <div className="text-xs text-destructive">{error}</div> : null}
              {result ? <WikiActionResult result={result} /> : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
