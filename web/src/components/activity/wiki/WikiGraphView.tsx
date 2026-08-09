/**
 * §4.1 graph takeover view: full-panel width via the shell's panel override
 * (released exactly once — Esc, Close, node navigation, and external layout
 * toggles all funnel through a release-once ref), scope/layer filters with
 * settings persisted at `gobby:wiki-tab:graph`, a persistent deutan-safe kind
 * legend, and the lazily loaded canvas graph behind an error boundary.
 */

import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { useResolvedTheme } from "../../../hooks/useResolvedTheme";
import { Card } from "../../ui/Card";
import { Input } from "../../ui/Input";
import { SegmentedControl } from "../../ui/SegmentedControl";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import { DetailActionButton } from "../fields";
import {
  buildGraphScene,
  communityColorVar,
  MAX_GRAPH_NODES,
  type WikiGraphHandlers,
  type WikiGraphInteraction,
  type WikiGraphSceneOptions,
} from "./WikiGraphScene";
import { fetchGraph, type WikiFetchScope } from "./WikiTabData";
import { wikiNodeColorVar, type WikiGraphInclude, type WikiGraphPayload } from "./WikiTabModel";
import {
  loadGraphSettings,
  storeGraphSettings,
  type WikiGraphSettings,
} from "./WikiTabState";

const WikiForceGraph = lazy(() => import("./WikiForceGraph"));

const INCLUDE_OPTIONS = [
  { value: "all", label: "All" },
  { value: "knowledge", label: "Knowledge" },
  { value: "code", label: "Code" },
] as const;

type ToggleKey = Exclude<keyof WikiGraphSettings, "include">;

const TOGGLES: ReadonlyArray<{ key: ToggleKey; label: string }> = [
  { key: "sources", label: "Sources & citations" },
  { key: "unresolved", label: "Unresolved" },
  { key: "orphans", label: "Orphans" },
  { key: "trust", label: "Trust edges" },
  { key: "audit", label: "Citation edges" },
  { key: "codeEdges", label: "Code edges" },
  { key: "communities", label: "Community colors" },
];

const LEGEND_KINDS: ReadonlyArray<{ kind: string; label: string }> = [
  { kind: "wiki_page", label: "Wiki page" },
  { kind: "code", label: "Code page" },
  { kind: "document", label: "Document" },
  { kind: "source", label: "Source" },
  { kind: "citation", label: "Citation" },
  { kind: "unresolved_target", label: "Unresolved" },
];

interface WikiGraphErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
}

class WikiGraphErrorBoundary extends Component<
  WikiGraphErrorBoundaryProps,
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[WikiGraphView]", error, info);
  }

  render() {
    if (this.state.hasError) return this.props.fallback;
    return this.props.children;
  }
}

interface WikiGraphViewProps {
  scope: WikiFetchScope;
  /** Pre-filter for the fetch scope (code mode opens with "code"). */
  initialInclude: WikiGraphInclude | null;
  /** Navigate to a page — the host closes the graph and flips mode. */
  onOpenPage: (path: string) => void;
  onClose: () => void;
  releasePanelOverride: () => void;
}

export function WikiGraphView({
  scope,
  initialInclude,
  onOpenPage,
  onClose,
  releasePanelOverride,
}: WikiGraphViewProps) {
  const [settings, setSettings] = useState<WikiGraphSettings>(() => {
    const stored = loadGraphSettings();
    return initialInclude ? { ...stored, include: initialInclude } : stored;
  });
  const [graph, setGraph] = useState<{ key: string; payload: WikiGraphPayload } | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [boundarySeq, setBoundarySeq] = useState(0);
  const [searchText, setSearchText] = useState("");
  const [size, setSize] = useState({ width: 800, height: 600 });
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const theme = useResolvedTheme();
  const reducedMotion = useMemo(
    () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false,
    [],
  );

  const interactionRef = useRef<WikiGraphInteraction>({ hoverId: null, search: "" });
  const handlersRef = useRef<WikiGraphHandlers>({ onNodeClick: () => {} });

  const releasedRef = useRef(false);
  const releaseOnce = useCallback(() => {
    if (releasedRef.current) return;
    releasedRef.current = true;
    releasePanelOverride();
  }, [releasePanelOverride]);

  const handleClose = useCallback(() => {
    releaseOnce();
    onClose();
  }, [onClose, releaseOnce]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.key !== "Escape") return;
      event.preventDefault();
      handleClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleClose]);

  useEffect(() => {
    return () => releaseOnce();
  }, [releaseOnce]);

  useEffect(() => {
    handlersRef.current.onNodeClick = (node) => {
      if (!node.path) return;
      releaseOnce();
      onOpenPage(node.path);
    };
  }, [onOpenPage, releaseOnce]);

  const requestKey = `${scope.projectId ?? ""}:${scope.topic ?? ""}:${settings.include}#${retrySeq}`;
  useEffect(() => {
    if (graph?.key === requestKey) return;
    let cancelled = false;
    fetchGraph(scope, settings.include)
      .then((payload) => {
        if (cancelled) return;
        setFetchError(null);
        setGraph({ key: requestKey, payload });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setFetchError(error instanceof Error ? error.message : "Failed to load graph");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [graph, requestKey, scope, settings.include]);

  useEffect(() => {
    const element = bodyRef.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      setSize({ width: rect.width || 800, height: rect.height || 600 });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const updateSettings = useCallback((patch: Partial<WikiGraphSettings>) => {
    setSettings((previous) => {
      const next = { ...previous, ...patch };
      storeGraphSettings(next);
      return next;
    });
  }, []);

  const live = graph?.key === requestKey ? graph.payload : null;

  const sceneOptions = useMemo<WikiGraphSceneOptions>(
    () => ({
      sources: settings.sources,
      unresolved: settings.unresolved,
      orphans: settings.orphans,
      trust: settings.trust,
      audit: settings.audit,
      codeEdges: settings.codeEdges,
      communities: settings.communities,
    }),
    [settings],
  );

  // Baked token colors depend on the active theme, so the theme is a rebuild
  // key even though buildGraphScene reads it implicitly via resolveCssVar.
  const scene = useMemo(
    () => (live ? buildGraphScene(live, sceneOptions) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [live, sceneOptions, theme],
  );

  const dataRevision = `${requestKey}|${TOGGLES.map(({ key }) => (settings[key] ? "1" : "0")).join("")}|${theme}`;

  const legendKinds = useMemo(() => {
    if (!scene) return [];
    const present = new Set(scene.nodes.map((node) => node.kind));
    return LEGEND_KINDS.filter((entry) => present.has(entry.kind));
  }, [scene]);

  const loadingSkeleton = (
    <Card
      role="status"
      aria-label="Loading graph"
      className="m-4 h-40 animate-pulse bg-muted/30"
    />
  );

  const boundaryFallback = (
    <div className="flex h-full min-h-0 flex-col items-center justify-center gap-3 px-4 text-center text-sm text-muted-foreground">
      <div>The wiki graph failed to render.</div>
      <div className="flex items-center gap-2">
        <DetailActionButton
          label="Try again"
          onClick={() => setBoundarySeq((seq) => seq + 1)}
        />
        <DetailActionButton label="Close graph" variant="accent" onClick={handleClose} />
      </div>
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      <div className="flex h-10 shrink-0 items-center gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
        <h2 className="truncate text-sm font-medium text-foreground">Wiki graph</h2>
        {scene?.capped ? (
          <span className="rounded-full border border-border px-2 py-0.5 text-2xs text-muted-foreground">
            showing top {MAX_GRAPH_NODES.toLocaleString("en-US")} of{" "}
            {scene.totalNodes.toLocaleString("en-US")}
          </span>
        ) : null}
        <div className="ml-auto">
          <DetailActionButton label="Close graph" onClick={handleClose} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-3 py-2">
        <SegmentedControl
          value={settings.include}
          onChange={(include) => updateSettings({ include })}
          options={INCLUDE_OPTIONS}
          ariaLabel="Graph scope"
          controlHeight="sm"
        />
        <Input
          aria-label="Search graph"
          value={searchText}
          onChange={(event) => {
            setSearchText(event.target.value);
            interactionRef.current.search = event.target.value.trim().toLowerCase();
          }}
          placeholder="Search nodes…"
          spellCheck={false}
          wrapperClassName="min-w-0 flex-1 basis-40"
          className="h-7 px-2 text-xs"
        />
        {TOGGLES.map(({ key, label }) => (
          <div
            key={key}
            className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-muted-foreground"
          >
            <Input
              id={`wiki-graph-${key}`}
              type="checkbox"
              checked={settings[key]}
              onChange={() => updateSettings({ [key]: !settings[key] })}
              wrapperClassName="w-auto shrink-0"
              className="h-4 w-4 rounded-sm p-0 accent-[var(--accent)]"
            />
            <label htmlFor={`wiki-graph-${key}`}>{label}</label>
          </div>
        ))}
      </div>

      {legendKinds.length > 0 ? (
        <ul
          aria-label="Graph legend"
          className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5"
        >
          {legendKinds.map(({ kind, label }) => (
            <li
              key={kind}
              className="flex items-center gap-1.5 text-2xs text-muted-foreground"
            >
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={
                  kind === "unresolved_target"
                    ? {
                        border: "1.5px dashed var(--color-destructive-foreground)",
                        backgroundColor: "transparent",
                      }
                    : { backgroundColor: `var(${wikiNodeColorVar(kind)})` }
                }
              />
              {label}
            </li>
          ))}
          {settings.communities && scene
            ? Array.from({ length: Math.min(scene.communityCount, 6) }, (_, index) => (
                <li
                  key={`community-${index}`}
                  className="flex items-center gap-1.5 text-2xs text-muted-foreground"
                >
                  <span
                    aria-hidden="true"
                    className="inline-block h-2.5 w-2.5 rounded-full"
                    style={{ backgroundColor: `var(${communityColorVar(index)})` }}
                  />
                  Community {index + 1}
                </li>
              ))
            : null}
        </ul>
      ) : null}

      <div ref={bodyRef} className="relative min-h-0 flex-1">
        {fetchError ? (
          <ActivityPanelEmpty
            heading="Graph unavailable"
            body={fetchError}
            footer={
              <DetailActionButton
                label="Retry"
                onClick={() => setRetrySeq((seq) => seq + 1)}
              />
            }
          />
        ) : scene && scene.nodes.length === 0 ? (
          <ActivityPanelEmpty
            heading="Nothing to graph"
            body="This wiki has no pages in the selected layers yet. Add sources or widen the layer filter, then refresh."
            footer={
              <DetailActionButton
                label="Refresh"
                onClick={() => setRetrySeq((seq) => seq + 1)}
              />
            }
          />
        ) : scene ? (
          <WikiGraphErrorBoundary key={boundarySeq} fallback={boundaryFallback}>
            <Suspense fallback={loadingSkeleton}>
              <WikiForceGraph
                scene={scene}
                dataRevision={dataRevision}
                theme={theme}
                width={size.width}
                height={size.height}
                reducedMotion={reducedMotion}
                interactionRef={interactionRef}
                handlersRef={handlersRef}
              />
            </Suspense>
          </WikiGraphErrorBoundary>
        ) : (
          loadingSkeleton
        )}
      </div>
    </div>
  );
}
