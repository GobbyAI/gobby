/**
 * Ask mode (plan wiki-obsidian-panel §5.1): grounded Q&A over the wiki.
 * Layout top→bottom: ask history (sessionStorage; restore/rerun/delete),
 * active answer area, composer pinned at the bottom with the
 * Extractive | Synthesized toggle. gwiki returns one JSON envelope — no
 * streaming — so the lifecycle is submit → staged progress (elapsed mm:ss,
 * cancel via client AbortController) → answer, or an inline error with
 * retry. Single-flight: the composer disables while a request is in flight.
 *
 * Citation invariant (the anti-DeepWiki guarantee): every citation chip is
 * re-resolved against the live node index — resolved chips navigate through
 * `nav.openPage` (auto mode-flip), unresolved chips are explicitly marked
 * broken with a "Search vault" fallback. Never a silent dead link.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import type { Components } from "react-markdown";
import type { PluggableList } from "unified";

import { remarkWikilink } from "../../../lib/markdown/remarkWikilink";
import { formatRelativeTime } from "../../../utils/formatTime";
import { Anchor } from "../../chat/CodeBlockRenderers";
import { MarkdownBody } from "../../shared/MarkdownBody";
import { SegmentedControl, type SegmentedControlOption } from "../../ui/SegmentedControl";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { fetchAsk, fetchPages, type WikiFetchScope } from "./WikiTabData";
import { buildNodeIndex, resolveWikilinkTarget, type WikiPageMeta } from "./WikiTabModel";
import {
  ASK_HISTORY_CAP,
  storeAskHistory,
  loadAskHistory,
  type AskHistoryEntry,
  type WikiNav,
} from "./WikiTabState";

const WIKILINK_PREFIX = "wikilink:";
/** The retrieval pass returns fast; past this the LLM is the wait. */
const SYNTHESIZING_HINT_AFTER_S = 8;

type AnswerMode = "extractive" | "synthesized";

const MODE_OPTIONS: readonly SegmentedControlOption<AnswerMode>[] = [
  { value: "extractive", label: "Extractive" },
  { value: "synthesized", label: "Synthesized" },
];

type AskPhase =
  | { status: "idle" }
  | { status: "pending"; question: string; llm: boolean; startedAt: number }
  | { status: "error"; question: string; llm: boolean; message: string }
  | { status: "ready"; entry: AskHistoryEntry };

const ghostButton =
  "rounded-md border border-border px-2 py-1 text-xs text-muted-foreground " +
  "hover:bg-muted hover:text-foreground disabled:opacity-40";

const chipClass =
  "flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs";

function makeId(): string {
  return `ask-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

interface AskRunnerDeps {
  scope: WikiFetchScope;
  abortRef: { current: AbortController | null };
  setPhase: (phase: AskPhase) => void;
  setNow: (now: number) => void;
  onAnswered: (entry: AskHistoryEntry) => void;
}

/** Module-level so render scope stays pure — timestamps and the request
 * lifecycle live here, outside the react-hooks/purity boundary. */
async function runAskRequest(
  { scope, abortRef, setPhase, setNow, onAnswered }: AskRunnerDeps,
  text: string,
  llm: boolean,
): Promise<void> {
  const trimmed = text.trim();
  // Single-flight: an in-flight controller means the composer is disabled;
  // rerun-from-history bypasses the disabled composer, so guard here too.
  if (!trimmed || abortRef.current) return;
  const controller = new AbortController();
  abortRef.current = controller;
  const startedAt = Date.now();
  setPhase({ status: "pending", question: trimmed, llm, startedAt });
  setNow(startedAt);
  try {
    const envelope = await fetchAsk(scope, {
      query: trimmed,
      llm,
      signal: controller.signal,
    });
    onAnswered({ id: makeId(), question: trimmed, llm, ts: Date.now(), envelope });
  } catch (error) {
    if (controller.signal.aborted) {
      // Client-side cancel — the server may keep working; nothing to show.
      setPhase({ status: "idle" });
      return;
    }
    setPhase({
      status: "error",
      question: trimmed,
      llm,
      message: error instanceof Error ? error.message : "Ask failed",
    });
  } finally {
    if (abortRef.current === controller) abortRef.current = null;
  }
}

function formatElapsed(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function WarningIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
      className="shrink-0"
    >
      <path d="M8 2.5 14.5 13.5H1.5L8 2.5Z" />
      <path d="M8 6.5V10" />
      <path d="M8 11.6v.2" />
    </svg>
  );
}

export interface WikiAskModeProps {
  scope: WikiFetchScope;
  nav: WikiNav;
  /** Gateway-down state — disables the composer with an info banner. */
  offline: boolean;
  /** Unresolved-citation fallback: fills the toolbar search in wiki mode. */
  onSearchVault: (query: string) => void;
}

export function WikiAskMode({ scope, nav, offline, onSearchVault }: WikiAskModeProps) {
  const [history, setHistory] = useState<AskHistoryEntry[]>(() => loadAskHistory());
  const [phase, setPhase] = useState<AskPhase>({ status: "idle" });
  const [question, setQuestion] = useState("");
  const [answerMode, setAnswerMode] = useState<AnswerMode>("extractive");
  const [now, setNow] = useState(0);
  const [pages, setPages] = useState<WikiPageMeta[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  // The node index backs citation resolution — same listing WikiBrowse uses.
  // A failed fetch degrades every citation to the explicit search fallback.
  useEffect(() => {
    let cancelled = false;
    fetchPages(scope)
      .then((result) => {
        if (!cancelled) setPages(result.pages);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [scope]);

  const nodeIndex = useMemo(() => buildNodeIndex(pages), [pages]);

  useEffect(() => {
    storeAskHistory(history);
  }, [history]);

  // Abort any in-flight ask when the mode unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  const pending = phase.status === "pending";
  useEffect(() => {
    if (!pending) return;
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, [pending]);

  const runAsk = (text: string, llm: boolean) =>
    runAskRequest(
      {
        scope,
        abortRef,
        setPhase,
        setNow,
        onAnswered: (entry) => {
          setHistory((prev) => [entry, ...prev].slice(0, ASK_HISTORY_CAP));
          setPhase({ status: "ready", entry });
          setQuestion("");
        },
      },
      text,
      llm,
    );

  const deleteEntry = (id: string) => {
    setHistory((prev) => prev.filter((entry) => entry.id !== id));
    setPhase((prev) => (prev.status === "ready" && prev.entry.id === id ? { status: "idle" } : prev));
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    void runAsk(question, answerMode === "synthesized");
  };

  // In-answer wikilinks share the citation invariant: resolved targets
  // navigate, unresolved targets fall back to vault search.
  const remarkPlugins = useMemo<PluggableList>(
    () => [
      [
        remarkWikilink,
        {
          resolve: (target: string) => {
            const resolved = resolveWikilinkTarget(nodeIndex, target);
            return resolved ? { path: resolved } : null;
          },
        },
      ],
    ],
    [nodeIndex],
  );

  const components = useMemo<Partial<Components>>(
    () => ({
      a: (props) => {
        const { href, children, node: _node, ...rest } = props;
        if (!href?.startsWith(WIKILINK_PREFIX)) {
          return (
            <Anchor href={href} {...rest}>
              {children}
            </Anchor>
          );
        }
        const rawTarget = decodeURIComponent(href.slice(WIKILINK_PREFIX.length));
        const pagePart = rawTarget.split("#")[0] ?? "";
        const resolved = resolveWikilinkTarget(nodeIndex, pagePart);
        const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
          event.preventDefault();
          if (resolved) void nav.openPage(resolved);
          else onSearchVault(pagePart);
        };
        return (
          <a href={href} {...rest} title={resolved ?? pagePart} onClick={handleClick}>
            {children}
          </a>
        );
      },
    }),
    [nav, nodeIndex, onSearchVault],
  );

  const elapsedSeconds =
    phase.status === "pending" ? Math.max(0, Math.floor((now - phase.startedAt) / 1_000)) : 0;
  const stagedHint =
    phase.status === "pending" && phase.llm && elapsedSeconds >= SYNTHESIZING_HINT_AFTER_S
      ? "Synthesizing…"
      : "Searching vault…";
  const composerDisabled = offline || pending;

  const historyMenuItems = (entry: AskHistoryEntry): QuickMenuItem[] => [
    { label: "Rerun", onSelect: () => void runAsk(entry.question, entry.llm) },
    { label: "Delete", destructive: true, onSelect: () => deleteEntry(entry.id) },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {history.length > 0 ? (
        <ul aria-label="Ask history" className="max-h-40 shrink-0 overflow-y-auto border-b border-border p-1">
          {history.map((entry) => (
            <li key={entry.id} className="group/row relative flex items-center">
              <button
                type="button"
                className="flex h-7 w-full min-w-0 items-center gap-1.5 rounded-md px-1.5 text-left text-sm text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                onClick={() => setPhase({ status: "ready", entry })}
              >
                <span className="truncate">{entry.question}</span>
                <span className="ml-auto flex shrink-0 items-center gap-1.5 pl-6">
                  <span className="rounded border border-border px-1 text-2xs text-muted-foreground">
                    {entry.llm ? "Synthesized" : "Extractive"}
                  </span>
                  <span className="text-2xs text-muted-foreground">
                    {formatRelativeTime(new Date(entry.ts).toISOString())}
                  </span>
                </span>
              </button>
              <span className="absolute right-1 opacity-0 focus-within:opacity-100 group-hover/row:opacity-100">
                <QuickMenu
                  items={historyMenuItems(entry)}
                  menuLabel={`Actions for ${entry.question}`}
                  triggerLabel={`Actions for ${entry.question}`}
                />
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        {phase.status === "idle" ? (
          <p className="max-w-[65ch] text-xs text-muted-foreground">
            Ask a grounded question about the wiki. Answers cite vault pages — every
            citation either opens or says it&rsquo;s broken.
          </p>
        ) : null}

        {phase.status === "pending" ? (
          <div className="flex items-center gap-3">
            <p
              role="status"
              aria-label="Ask progress"
              className="flex items-center gap-2 text-xs text-muted-foreground"
            >
              <span
                aria-hidden="true"
                className="size-3 shrink-0 animate-spin rounded-full border border-border border-t-foreground motion-reduce:animate-none"
              />
              <span>{stagedHint}</span>
              <span className="font-mono">{formatElapsed(elapsedSeconds)}</span>
              {phase.llm ? <span>Synthesized answers can take a few minutes.</span> : null}
            </p>
            <button
              type="button"
              className={ghostButton}
              title="Cancel the request — the server may keep working."
              onClick={() => abortRef.current?.abort()}
            >
              Cancel
            </button>
          </div>
        ) : null}

        {phase.status === "error" ? (
          <div className="flex items-center gap-3">
            <p role="alert" className="max-w-[65ch] text-xs text-destructive-foreground">
              {phase.message}
            </p>
            <button
              type="button"
              className={ghostButton}
              onClick={() => void runAsk(phase.question, phase.llm)}
            >
              Retry
            </button>
          </div>
        ) : null}

        {phase.status === "ready" ? (
          <AskAnswer
            entry={phase.entry}
            nodeIndex={nodeIndex}
            nav={nav}
            onSearchVault={onSearchVault}
            remarkPlugins={remarkPlugins}
            components={components}
          />
        ) : null}
      </div>

      <div className="shrink-0 space-y-2 border-t border-border px-3 py-2">
        {offline ? (
          <p className="max-w-[65ch] text-xs text-muted-foreground">
            The wiki gateway is unreachable — the composer is disabled until it recovers.
          </p>
        ) : null}
        <textarea
          aria-label="Ask the wiki"
          rows={2}
          value={question}
          disabled={composerDisabled}
          placeholder="Ask a grounded question…"
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={handleComposerKeyDown}
          className="w-full max-w-[65ch] resize-none rounded-md border border-border bg-transparent px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground disabled:opacity-60"
        />
        <div className="flex items-center gap-2">
          <SegmentedControl<AnswerMode>
            value={answerMode}
            onChange={setAnswerMode}
            options={MODE_OPTIONS}
            ariaLabel="Answer mode"
            disabled={composerDisabled}
          />
          <button
            type="button"
            className={ghostButton}
            disabled={composerDisabled || !question.trim()}
            onClick={() => void runAsk(question, answerMode === "synthesized")}
          >
            Ask
          </button>
        </div>
      </div>
    </div>
  );
}

interface AskAnswerProps {
  entry: AskHistoryEntry;
  nodeIndex: ReturnType<typeof buildNodeIndex>;
  nav: WikiNav;
  onSearchVault: (query: string) => void;
  remarkPlugins: PluggableList;
  components: Partial<Components>;
}

function AskAnswer({
  entry,
  nodeIndex,
  nav,
  onSearchVault,
  remarkPlugins,
  components,
}: AskAnswerProps) {
  const { envelope } = entry;
  return (
    <div className="space-y-3">
      <p className="max-w-[65ch] text-xs text-muted-foreground">{entry.question}</p>

      {envelope.groundingWarnings.length > 0 ? (
        // §5.1 grounding: the ungrounded claims callout — icon + warning
        // tokens + text so it stays legible in monochrome; envelopes without
        // warnings render nothing.
        <div
          role="status"
          aria-label="Grounding warnings"
          className="max-w-[65ch] rounded-md border border-border bg-[var(--color-warning-soft)] px-2.5 py-2"
        >
          <p className="flex items-center gap-1.5 text-xs font-medium text-[var(--color-warning-foreground)]">
            <WarningIcon />
            Grounding warnings
          </p>
          <ul className="mt-1 list-disc pl-4 text-xs text-foreground">
            {envelope.groundingWarnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {envelope.answer ? (
        <div className="max-w-[65ch] text-sm">
          <MarkdownBody
            content={envelope.answer}
            id={`ask-${entry.id}`}
            remarkPlugins={remarkPlugins}
            components={components}
          />
        </div>
      ) : (
        <ul aria-label="Retrieved passages" className="max-w-[65ch] space-y-2">
          {envelope.hits.map((hit, index) => {
            const target = hit.wikiPage ?? hit.sourcePath;
            return (
              <li key={`${target ?? "hit"}-${index}`} className="rounded-md border border-border px-2.5 py-2">
                {target ? (
                  <button
                    type="button"
                    className="text-sm font-medium text-foreground hover:underline"
                    onClick={() => void nav.openPage(target)}
                  >
                    {hit.title ?? target}
                  </button>
                ) : (
                  <span className="text-sm font-medium text-foreground">{hit.title}</span>
                )}
                {hit.snippet ? (
                  <p className="mt-0.5 text-xs text-muted-foreground">{hit.snippet}</p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      {envelope.citations.length > 0 ? (
        <section className="max-w-[65ch]">
          <h3 className="text-xs font-medium text-muted-foreground">Citations</h3>
          <ul aria-label="Citations" className="mt-1 flex list-none flex-wrap gap-1.5">
            {envelope.citations.map((citation) => {
              const resolved = resolveWikilinkTarget(nodeIndex, citation.target);
              if (resolved) {
                return (
                  <li key={citation.target}>
                    <button
                      type="button"
                      className={`${chipClass} border-border text-foreground hover:bg-muted`}
                      title={resolved}
                      onClick={() => void nav.openPage(resolved)}
                    >
                      {citation.title}
                    </button>
                  </li>
                );
              }
              return (
                <li
                  key={citation.target}
                  className={`${chipClass} border-dashed border-border text-muted-foreground`}
                >
                  <span title={citation.target}>{citation.title}</span>
                  <span aria-hidden="true">·</span>
                  <span>unresolved</span>
                  <button
                    type="button"
                    className="underline underline-offset-2 hover:text-foreground"
                    aria-label={`Search vault for ${citation.title}`}
                    onClick={() => onSearchVault(citation.target)}
                  >
                    Search vault
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
