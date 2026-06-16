import {
  Component,
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { KnowledgeGraphData } from "../../../hooks/useMemory";
import { DetailActionButton } from "../fields";

const DEFAULT_GRAPH_LIMIT = 500;

const KnowledgeGraph = lazy(() =>
  import("./KnowledgeGraph").then((module) => ({ default: module.KnowledgeGraph })),
);

interface MemoryGraphViewProps {
  fetchKnowledgeGraph: (limit?: number) => Promise<KnowledgeGraphData | null>;
  fetchEntityNeighbors: (entityKey: string) => Promise<KnowledgeGraphData | null>;
  releasePanelOverride: () => void;
  onClose: () => void;
  limit?: number;
}

interface MemoryGraphErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
}

class MemoryGraphErrorBoundary extends Component<
  MemoryGraphErrorBoundaryProps,
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError(): { hasError: boolean } {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[MemoryGraphView]", error, info);
  }

  render() {
    if (this.state.hasError) return this.props.fallback;
    return this.props.children;
  }
}

export function MemoryGraphView({
  fetchKnowledgeGraph,
  fetchEntityNeighbors,
  releasePanelOverride,
  onClose,
  limit = DEFAULT_GRAPH_LIMIT,
}: MemoryGraphViewProps) {
  const releasedRef = useRef(false);
  const [graphFailed, setGraphFailed] = useState(false);

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

  const errorFallback = (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-4 text-center text-sm text-muted-foreground">
      <div>3D knowledge graph failed to load.</div>
      <div className="flex items-center gap-2">
        <DetailActionButton label="Try again" onClick={() => setGraphFailed(false)} />
        <DetailActionButton label="Close graph" variant="accent" onClick={handleClose} />
      </div>
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      <div className="flex h-10 items-center justify-between gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
        <h2 className="truncate text-sm font-medium text-foreground">Memory graph</h2>
        <DetailActionButton label="Close graph" onClick={handleClose} />
      </div>
      <div className="min-h-0 flex-1 p-3">
        {graphFailed ? (
          errorFallback
        ) : (
          <MemoryGraphErrorBoundary fallback={errorFallback}>
            <Suspense
              fallback={
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Loading 3D graph...
                </div>
              }
            >
              <KnowledgeGraph
                fetchKnowledgeGraph={fetchKnowledgeGraph}
                fetchEntityNeighbors={fetchEntityNeighbors}
                limit={limit}
                onError={() => setGraphFailed(true)}
              />
            </Suspense>
          </MemoryGraphErrorBoundary>
        )}
      </div>
    </div>
  );
}
