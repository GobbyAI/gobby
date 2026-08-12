import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import ForceGraph3D from "react-force-graph-3d";
import SpriteText from "three-spritetext";
import { SphereGeometry, MeshLambertMaterial, Mesh } from "three";
import { IS_IOS_DEVICE, IS_MOBILE_DEVICE } from "../../../utils/platform";
import { resolveCssVar, cn, escapeHtml } from "../../../lib/utils";
import type {
  KnowledgeGraphData,
  KnowledgeEntity,
} from "../../../hooks/useMemory";
import { Button } from "../../ui/Button";
import { Card } from "../../ui/Card";
import { Input } from "../../ui/Input";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import {
  DEFAULT_GRAPH_LIMITS,
  MOBILE_ENTITY_CAP,
  MOBILE_RELATIONSHIP_CAP,
  buildForceData,
  buildNeighborIndex,
  buildNodeCardHtml,
  effectiveGraphLimits,
  getEntityColorCss,
  mergeGraphData,
  numericId,
  sanitizeGraphLimit,
  type GraphLimits,
} from "./KnowledgeGraphModel";

// `isolate` scopes the z-10 overlays to this container so panel chrome with a
// lower z-index (.activity-panel-mobile-menu, z-5) still layers above the graph.
// `knowledge-graph` is the hook base.css uses to unstyle the injected
// float-tooltip wrapper so the node card owns its surface.
interface KnowledgeGraphProps {
  fetchKnowledgeGraph: (
    limit?: number,
    relationshipLimit?: number,
  ) => Promise<KnowledgeGraphData | null>;
  fetchEntityNeighbors: (
    entityKey: string,
  ) => Promise<KnowledgeGraphData | null>;
  limits?: GraphLimits;
  onLimitsChange?: (next: GraphLimits) => void;
  onError?: () => void;
  onClose?: () => void;
}

const DEFAULT_CHARGE = -120;
const DEFAULT_LINK_DIST = 60;
const DEFAULT_CENTER = 0.05;

// Close lives in the graph's floating toolbar (and alone in the loading/error/
// empty branches) so the graph needs no chrome bar of its own.
function GraphCloseButton({ onClose }: { onClose: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon"
      dense
      className={cn(
        "flex h-7 min-h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
        coarseHitAreaCls,
      )}
      onClick={onClose}
      title="Close graph"
      aria-label="Close graph"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M18 6 6 18" />
        <path d="m6 6 12 12" />
      </svg>
    </Button>
  );
}

// Committed number field for the settings panel: draft state locally, persist
// on blur/Enter. An emptied field reverts to the last committed value instead
// of silently meaning "no limit".
function GraphLimitRow({
  label,
  ariaLabel,
  value,
  onCommit,
}: {
  label: string;
  ariaLabel: string;
  value: number;
  onCommit: (next: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  // Re-sync the draft when the committed value changes from outside
  // (adjust-during-render — https://react.dev/learn/you-might-not-need-an-effect).
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    setDraft(String(value));
  }
  const commit = () => {
    const parsed =
      draft.trim() === "" ? value : sanitizeGraphLimit(Number(draft), value);
    setDraft(String(parsed));
    onCommit(parsed);
  };
  return (
    <div className="flex items-center justify-between gap-1.5">
      <span className="min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]">
        {label}
      </span>
      <Input
        type="number"
        min={0}
        value={draft}
        wrapperClassName="w-auto"
        className="h-6 w-[84px] rounded border-[var(--border)] bg-[var(--bg-primary)] px-1.5 text-right font-[inherit] text-[length:var(--text-xs)] text-[var(--text-primary)] tabular-nums"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            e.currentTarget.blur();
          }
        }}
        aria-label={ariaLabel}
      />
    </div>
  );
}

export function KnowledgeGraph({
  fetchKnowledgeGraph,
  fetchEntityNeighbors,
  limits = DEFAULT_GRAPH_LIMITS,
  onLimitsChange,
  onError,
  onClose,
}: KnowledgeGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<any>(null);
  const [graphData, setGraphData] = useState<KnowledgeGraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  const [expandingNode, setExpandingNode] = useState<string | null>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  const [animateIdle, setAnimateIdle] = useState(() => {
    try {
      return localStorage.getItem("gobby-kg-animate") === "true";
    } catch {
      return false;
    }
  });
  const [showPhysics, setShowPhysics] = useState(false);
  const [charge, setCharge] = useState(() => {
    try {
      const v = localStorage.getItem("gobby-kg-charge");
      const n = Number(v);
      return v && Number.isFinite(n) ? n : DEFAULT_CHARGE;
    } catch {
      return DEFAULT_CHARGE;
    }
  });
  const [linkDist, setLinkDist] = useState(() => {
    try {
      const v = localStorage.getItem("gobby-kg-link-dist");
      const n = Number(v);
      return v && Number.isFinite(n) ? n : DEFAULT_LINK_DIST;
    } catch {
      return DEFAULT_LINK_DIST;
    }
  });
  const [centerStrength, setCenterStrength] = useState(() => {
    try {
      const v = localStorage.getItem("gobby-kg-center");
      const n = Number(v);
      return v && Number.isFinite(n) ? n : DEFAULT_CENTER;
    } catch {
      return DEFAULT_CENTER;
    }
  });

  // Catch async WebGL/Three.js errors that escape React error boundaries
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);
  useEffect(() => {
    const handleError = (e: ErrorEvent) => {
      const msg = (e.message || "").toLowerCase();
      if (
        msg.includes("webgl") ||
        msg.includes("three") ||
        msg.includes("context lost") ||
        msg.includes("texture") ||
        msg.includes("gl_")
      ) {
        console.error(
          "[KnowledgeGraph] WebGL/Three.js error caught:",
          e.message,
        );
        e.preventDefault();
        onErrorRef.current?.();
      }
    };
    const handleRejection = (e: PromiseRejectionEvent) => {
      const msg = String(e.reason || "").toLowerCase();
      if (
        msg.includes("webgl") ||
        msg.includes("three") ||
        msg.includes("context lost")
      ) {
        console.error("[KnowledgeGraph] Unhandled WebGL rejection:", e.reason);
        e.preventDefault();
        onErrorRef.current?.();
      }
    };
    window.addEventListener("error", handleError);
    window.addEventListener("unhandledrejection", handleRejection);
    return () => {
      window.removeEventListener("error", handleError);
      window.removeEventListener("unhandledrejection", handleRejection);
    };
  }, []);

  // Handle WebGL context lost on the canvas element
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const canvas = container.querySelector("canvas");
    if (!canvas) return;
    const handleContextLost = (e: Event) => {
      e.preventDefault();
      console.error("[KnowledgeGraph] WebGL context lost");
      onErrorRef.current?.();
    };
    canvas.addEventListener("webglcontextlost", handleContextLost);
    return () =>
      canvas.removeEventListener("webglcontextlost", handleContextLost);
  }, [loading]); // re-run after loading completes since canvas only exists after render

  // Track container size
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setDimensions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // Persist animation toggle
  const toggleAnimate = useCallback(() => {
    setAnimateIdle((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("gobby-kg-animate", String(next));
      } catch {
        /* noop */
      }
      return next;
    });
  }, []);

  // Manual auto-rotation (TrackballControls lacks autoRotate)
  useEffect(() => {
    if (!animateIdle) return;
    let raf: number;
    const rotate = () => {
      const fg = fgRef.current;
      if (fg) {
        const pos = fg.cameraPosition();
        const dist = Math.sqrt(pos.x * pos.x + pos.z * pos.z);
        const angle = Math.atan2(pos.z, pos.x) + 0.002;
        fg.cameraPosition({
          x: dist * Math.cos(angle),
          y: pos.y,
          z: dist * Math.sin(angle),
        });
      }
      raf = requestAnimationFrame(rotate);
    };
    raf = requestAnimationFrame(rotate);
    return () => cancelAnimationFrame(raf);
  }, [animateIdle]);

  // Mobile GPUs get a hard cap regardless of the persisted setting (#19157)
  const effectiveLimits = useMemo(
    () => effectiveGraphLimits(limits, IS_MOBILE_DEVICE),
    [limits],
  );

  // Initial data fetch (refetches when limits change)
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await fetchKnowledgeGraph(
          effectiveLimits.entities,
          effectiveLimits.relationships,
        );
        if (!cancelled) {
          if (data) setGraphData(data);
          setLoadError(false);
          setLoading(false);
        }
      } catch (error) {
        if (cancelled) return;
        console.error("Failed to load knowledge graph", error);
        setLoadError(true);
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchKnowledgeGraph, effectiveLimits, loadAttempt]);

  // Build force graph data
  const forceData = useMemo(() => {
    if (!graphData) return { nodes: [], links: [] };
    return buildForceData(graphData);
  }, [graphData]);

  // Named connections per node for the hover card
  const neighborIndex = useMemo(
    () => buildNeighborIndex(forceData.nodes, forceData.links),
    [forceData],
  );

  // Apply force parameters whenever data or physics values change
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("charge")?.strength(charge);
    fg.d3Force("link")?.distance(linkDist);
    fg.d3Force("center")?.strength(centerStrength);

    // Cap pixel ratio on mobile — iPhone 16 PM is 3x; capping at 2x cuts framebuffer 9x → 4x
    if (IS_MOBILE_DEVICE) {
      try {
        fg.renderer().setPixelRatio(Math.min(window.devicePixelRatio, 2));
      } catch {
        /* renderer may not be ready */
      }
    }
  }, [forceData, charge, linkDist, centerStrength]);

  // Reheat simulation only when physics sliders change (not on data load)
  const physicsInitialized = useRef(false);
  useEffect(() => {
    if (!physicsInitialized.current) {
      physicsInitialized.current = true;
      return;
    }
    const fg = fgRef.current;
    if (!fg) return;
    try {
      fg.d3ReheatSimulation();
    } catch {
      /* simulation may not be ready */
    }
  }, [charge, linkDist, centerStrength]);

  // Search
  const searchLower = searchQuery.toLowerCase();
  const isSearchActive = searchQuery.length > 0;

  // Node click: expand neighbors
  const handleNodeClick = useCallback(
    (node: any) => {
      if (expandingNode) return;
      const entityKey = node.id as string;
      const displayName = (node.name as string) || entityKey;
      setExpandingNode(displayName);
      void (async () => {
        try {
          const data = await fetchEntityNeighbors(entityKey);
          if (data) {
            setGraphData((prev) => (prev ? mergeGraphData(prev, data) : data));
          }
        } finally {
          setExpandingNode(null);
        }
      })();
    },
    [fetchEntityNeighbors, expandingNode],
  );

  // Shared sphere geometry (reused across all iOS nodes to reduce GPU allocations)
  const sphereGeo = useMemo(
    () => (IS_IOS_DEVICE ? new SphereGeometry(3, 12, 8) : null),
    [],
  );

  // Custom node rendering — iOS: simple colored spheres (zero per-node textures);
  // other mobile: lightweight SpriteText; desktop: full SpriteText with backgrounds
  const nodeThreeObject = useCallback(
    (node: any) => {
      try {
        const label = node.name as string;
        const color = node.color as string;
        const dimmed =
          isSearchActive && !label.toLowerCase().includes(searchLower);

        // iOS: simple sphere mesh — no canvas textures at all
        if (IS_IOS_DEVICE && sphereGeo) {
          const mat = new MeshLambertMaterial({
            color: dimmed ? resolveCssVar("--text-muted") : color,
            transparent: dimmed,
            opacity: dimmed ? 0.4 : 1,
          });
          return new Mesh(sphereGeo, mat);
        }

        const sprite = new SpriteText(label);
        sprite.color = dimmed ? resolveCssVar("--text-muted") : color;
        sprite.fontFace = "SF Mono, Menlo, monospace";

        if (IS_MOBILE_DEVICE) {
          // Other mobile: smaller text, no background/border (smaller canvas textures)
          sprite.textHeight = 2;
        } else {
          // Desktop: full styling — textHeight 4 with a near-opaque backing plate
          // keeps labels legible against link clutter (#19153)
          sprite.textHeight = 4;
          sprite.backgroundColor = dimmed
            ? resolveCssVar("--bg-primary", 0.3)
            : resolveCssVar("--bg-primary", 0.85);
          sprite.borderColor = dimmed ? "transparent" : color;
          sprite.borderWidth = 0.3;
          sprite.borderRadius = 3;
          sprite.padding = [2, 4] as any;
        }
        return sprite;
      } catch (e) {
        console.error("[KnowledgeGraph] SpriteText creation failed:", e);
        const fallback = new SpriteText("?");
        fallback.color = resolveCssVar("--text-muted");
        fallback.textHeight = 3;
        return fallback;
      }
    },
    [isSearchActive, searchLower, sphereGeo],
  );

  // Link styling
  const linkColor = useCallback(
    (link: any) => {
      if (isSearchActive) {
        const sourceLabel =
          typeof link.source === "object"
            ? (link.source.name ?? link.source.id)
            : link.source;
        const targetLabel =
          typeof link.target === "object"
            ? (link.target.name ?? link.target.id)
            : link.target;
        const srcMatch = String(sourceLabel)
          .toLowerCase()
          .includes(searchLower);
        const tgtMatch = String(targetLabel)
          .toLowerCase()
          .includes(searchLower);
        if (!srcMatch && !tgtMatch) return resolveCssVar("--text-muted", 0.15);
      }
      return link.color || resolveCssVar("--text-muted", 0.4);
    },
    [isSearchActive, searchLower],
  );

  const linkLabel = useCallback(
    (link: any) => escapeHtml(String(link.type ?? "")),
    [],
  );

  // Node breathing effect — use ref to avoid prop changes that trigger graph rebuilds
  const animateRef = useRef(animateIdle);
  useEffect(() => {
    animateRef.current = animateIdle;
  }, [animateIdle]);
  const nodePositionUpdate = useCallback((obj: any) => {
    if (!animateRef.current) {
      // Restore original scale when animation stops
      if (obj.__origScale) {
        obj.scale.copy(obj.__origScale);
        delete obj.__origScale;
      }
      return;
    }
    // Capture SpriteText's dimensional scale on first animated frame
    if (!obj.__origScale) {
      obj.__origScale = obj.scale.clone();
    }
    const t = performance.now() * 0.001;
    const offset = (numericId(obj.id) % 100) * 0.1;
    const factor = 1 + Math.sin(t * 1.5 + offset) * 0.06;
    obj.scale.set(
      obj.__origScale.x * factor,
      obj.__origScale.y * factor,
      obj.__origScale.z * factor,
    );
  }, []);

  // Legend types
  const legendTypes = useMemo(() => {
    if (!graphData) return [];
    return [
      ...new Set(graphData.entities.map((e) => e.entity_type.toLowerCase())),
    ];
  }, [graphData]);

  // Close must stay reachable in every branch, so the loading/error/empty
  // returns render a toolbar stub holding only the close button.
  const closeControls = onClose ? (
    <Card className="absolute top-2 right-2 z-10 flex items-center gap-1 rounded-md border-[var(--border)] bg-[var(--bg-secondary)] p-[3px]">
      <GraphCloseButton onClose={onClose} />
    </Card>
  ) : null;

  // Loading state
  if (loading) {
    return (
      <Card asChild>
        <div
          className="knowledge-graph relative isolate flex min-h-0 flex-1 overflow-hidden rounded-lg border-[var(--border)] bg-[var(--bg-primary)]"
          ref={containerRef}
        >
          <div className="flex h-full min-h-[300px] flex-1 flex-col items-center justify-center gap-2 text-[var(--text-muted)]">
            Loading knowledge graph...
          </div>
          {closeControls}
        </div>
      </Card>
    );
  }

  if (loadError) {
    return (
      <Card asChild>
        <div
          className="knowledge-graph relative isolate flex min-h-0 flex-1 overflow-hidden rounded-lg border-[var(--border)] bg-[var(--bg-primary)]"
          ref={containerRef}
        >
          <div className="flex h-full min-h-[300px] flex-1 flex-col items-center justify-center gap-2 text-[var(--text-muted)]">
            <div>Failed to load knowledge graph</div>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              dense
              className={cn(
                "mt-2 rounded border-[var(--border)] px-3 py-1 text-[length:var(--text-sm)] font-normal hover:bg-[var(--bg-tertiary)]",
                coarseHitAreaCls,
              )}
              onClick={() => {
                setLoading(true);
                setLoadAttempt((attempt) => attempt + 1);
              }}
            >
              Retry
            </Button>
          </div>
          {closeControls}
        </div>
      </Card>
    );
  }

  // Empty state
  if (!graphData || graphData.entities.length === 0) {
    return (
      <Card asChild>
        <div
          className="knowledge-graph relative isolate flex min-h-0 flex-1 overflow-hidden rounded-lg border-[var(--border)] bg-[var(--bg-primary)]"
          ref={containerRef}
        >
          <div className="flex h-full min-h-[300px] flex-1 flex-col items-center justify-center gap-2 text-[var(--text-muted)]">
            <div>No entities found</div>
            <div className="mt-1 text-[length:var(--text-sm)]">
              Connect a FalkorDB instance to explore knowledge graph entities
              and relationships.
            </div>
          </div>
          {closeControls}
        </div>
      </Card>
    );
  }

  return (
    <Card asChild>
      <div
        className="knowledge-graph relative isolate flex min-h-0 flex-1 overflow-hidden rounded-lg border-[var(--border)] bg-[var(--bg-primary)]"
        ref={containerRef}
      >
        <ForceGraph3D
          ref={fgRef}
          width={dimensions.width}
          height={dimensions.height}
          graphData={forceData}
          nodeId="id"
          nodeLabel={(node: any) => {
            const e = node.entity as KnowledgeEntity;
            return buildNodeCardHtml(
              e,
              neighborIndex.get(node.id as string) ?? [],
            );
          }}
          nodeThreeObject={nodeThreeObject}
          nodeThreeObjectExtend={false}
          onNodeClick={handleNodeClick}
          linkSource="source"
          linkTarget="target"
          linkLabel={linkLabel}
          linkColor={linkColor}
          linkWidth={0.5}
          linkOpacity={0.6}
          linkDirectionalArrowLength={IS_MOBILE_DEVICE ? 0 : 3}
          linkDirectionalArrowRelPos={1}
          linkDirectionalParticles={IS_MOBILE_DEVICE ? 0 : 2}
          linkDirectionalParticleSpeed={0.004}
          linkDirectionalParticleWidth={0.8}
          linkDirectionalParticleColor={linkColor}
          nodePositionUpdate={IS_MOBILE_DEVICE ? undefined : nodePositionUpdate}
          backgroundColor="rgba(0,0,0,0)"
          showNavInfo={false}
          enableNodeDrag={true}
          {...(IS_MOBILE_DEVICE
            ? {
                rendererConfig: {
                  antialias: false,
                  powerPreference: "low-power" as const,
                },
              }
            : {})}
        />

        <div className="absolute top-2 left-2 z-10 flex flex-col items-start gap-1.5">
          <Card className="rounded-md border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 text-[length:var(--text-xs)] text-[var(--text-muted)]">
            {forceData.nodes.length} entities &middot; {forceData.links.length}{" "}
            relationships
          </Card>
          {expandingNode && (
            <Card className="rounded-md border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 text-[length:var(--text-xs)] text-[var(--accent)]">
              Expanding {expandingNode}...
            </Card>
          )}
        </div>

        {/* Legend overlay (bottom-left) */}
        {legendTypes.length > 0 && (
          <Card className="absolute bottom-2 left-2 z-10 flex items-center gap-2.5 rounded-md border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-xs)] text-[var(--text-muted)]">
            {legendTypes.map((type) => (
              <div key={type} className="flex items-center gap-1">
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: getEntityColorCss(type) }}
                />
                {type}
              </div>
            ))}
          </Card>
        )}

        {/* Controls (top-right) */}
        <Card className="absolute top-2 right-2 z-10 flex items-center gap-1 rounded-md border-[var(--border)] bg-[var(--bg-secondary)] p-[3px]">
          {showSearch && (
            <Input
              type="text"
              placeholder="Filter entities..."
              aria-label="Filter entities"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              wrapperClassName="w-auto"
              className="h-7 w-[150px] rounded border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 font-mono text-[length:var(--text-sm)] text-[var(--text-primary)]"
              autoFocus
            />
          )}
          <Button
            variant="ghost"
            size="icon"
            dense
            className={cn(
              "flex h-7 min-h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
              coarseHitAreaCls,
              (showSearch || isSearchActive) &&
                "bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]",
            )}
            onClick={() => {
              if (showSearch) setSearchQuery("");
              setShowSearch(!showSearch);
            }}
            title="Filter entities"
            aria-label="Filter entities"
            aria-expanded={showSearch}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
            </svg>
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={cn(
              "flex h-7 min-h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
              coarseHitAreaCls,
            )}
            onClick={() => fgRef.current?.zoomToFit(400)}
            title="Zoom to fit"
            aria-label="Zoom to fit"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
            </svg>
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={cn(
              "flex h-7 min-h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
              coarseHitAreaCls,
              animateIdle &&
                "bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]",
            )}
            onClick={toggleAnimate}
            title={animateIdle ? "Pause idle animation" : "Animate when idle"}
            aria-label={
              animateIdle ? "Pause idle animation" : "Animate when idle"
            }
          >
            {animateIdle ? (
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="currentColor"
              >
                <rect x="6" y="4" width="4" height="16" rx="1" />
                <rect x="14" y="4" width="4" height="16" rx="1" />
              </svg>
            ) : (
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="currentColor"
              >
                <path d="M8 5v14l11-7z" />
              </svg>
            )}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className={cn(
              "flex h-7 min-h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[length:var(--text-base)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]",
              coarseHitAreaCls,
              showPhysics &&
                "bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:text-[var(--bg-primary)]",
            )}
            onClick={() => setShowPhysics((p) => !p)}
            title="Graph settings"
            aria-label="Graph settings"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </Button>
          {onClose && <GraphCloseButton onClose={onClose} />}
        </Card>

        {/* Graph settings panel: data limits + physics */}
        {showPhysics && (
          <Card className="absolute top-[42px] right-2 z-10 flex min-w-[200px] flex-col gap-1.5 rounded-md border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2">
            <GraphLimitRow
              label="Entities"
              ariaLabel="Entity limit"
              value={limits.entities}
              onCommit={(entities) => {
                if (entities !== limits.entities)
                  onLimitsChange?.({ ...limits, entities });
              }}
            />
            <GraphLimitRow
              label="Relationships"
              ariaLabel="Relationship limit"
              value={limits.relationships}
              onCommit={(relationships) => {
                if (relationships !== limits.relationships)
                  onLimitsChange?.({ ...limits, relationships });
              }}
            />
            {IS_MOBILE_DEVICE ? (
              <div className="max-w-[210px] text-[length:var(--text-2xs)] leading-snug text-[var(--text-muted)]">
                Hard-capped at {MOBILE_ENTITY_CAP} entities /{" "}
                {MOBILE_RELATIONSHIP_CAP} relationships on this device.
              </div>
            ) : (
              <div
                className="flex max-w-[210px] items-start gap-1.5 text-[length:var(--text-2xs)] leading-snug text-[var(--color-warning-foreground)]"
                role="note"
              >
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                  className="mt-px shrink-0"
                >
                  <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" />
                  <path d="M12 9v4" />
                  <path d="M12 17h.01" />
                </svg>
                <span>
                  0 = no limit. Rendering capability varies by machine — very
                  high or unlimited values can be unstable.
                </span>
              </div>
            )}
            <div className="my-0.5 border-t border-[var(--border)]" />
            <div className="flex cursor-default items-center gap-1.5">
              <span className="min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]">
                Repulsion
              </span>
              <Input
                type="range"
                aria-label="Repulsion"
                wrapperClassName="w-auto flex-1"
                min={-500}
                max={-20}
                step={10}
                value={charge}
                className="h-1 cursor-pointer border-0 bg-transparent p-0 accent-[var(--accent)]"
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setCharge(v);
                  try {
                    localStorage.setItem("gobby-kg-charge", String(v));
                  } catch {
                    /* noop */
                  }
                }}
              />
              <span className="min-w-[36px] text-right font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)] tabular-nums">
                {charge}
              </span>
            </div>
            <div className="flex cursor-default items-center gap-1.5">
              <span className="min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]">
                Link dist
              </span>
              <Input
                type="range"
                aria-label="Link distance"
                wrapperClassName="w-auto flex-1"
                min={10}
                max={200}
                step={5}
                value={linkDist}
                className="h-1 cursor-pointer border-0 bg-transparent p-0 accent-[var(--accent)]"
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setLinkDist(v);
                  try {
                    localStorage.setItem("gobby-kg-link-dist", String(v));
                  } catch {
                    /* noop */
                  }
                }}
              />
              <span className="min-w-[36px] text-right font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)] tabular-nums">
                {linkDist}
              </span>
            </div>
            <div className="flex cursor-default items-center gap-1.5">
              <span className="min-w-[56px] text-[length:var(--text-xs)] text-[var(--text-secondary)]">
                Gravity
              </span>
              <Input
                type="range"
                aria-label="Gravity"
                wrapperClassName="w-auto flex-1"
                min={0.005}
                max={0.15}
                step={0.005}
                value={centerStrength}
                className="h-1 cursor-pointer border-0 bg-transparent p-0 accent-[var(--accent)]"
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setCenterStrength(v);
                  try {
                    localStorage.setItem("gobby-kg-center", String(v));
                  } catch {
                    /* noop */
                  }
                }}
              />
              <span className="min-w-[36px] text-right font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)] tabular-nums">
                {centerStrength.toFixed(3)}
              </span>
            </div>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              dense
              className={cn(
                "mt-0.5 cursor-pointer self-end rounded border-[var(--border)] bg-[var(--bg-tertiary)] py-0.5 text-[length:var(--text-xs)] font-normal text-[var(--text-secondary)] hover:bg-[var(--bg-primary)] hover:text-[var(--text-primary)]",
                coarseHitAreaCls,
              )}
              onClick={() => {
                setCharge(DEFAULT_CHARGE);
                setLinkDist(DEFAULT_LINK_DIST);
                setCenterStrength(DEFAULT_CENTER);
                try {
                  localStorage.removeItem("gobby-kg-charge");
                  localStorage.removeItem("gobby-kg-link-dist");
                  localStorage.removeItem("gobby-kg-center");
                } catch {
                  /* noop */
                }
              }}
            >
              Reset
            </Button>
          </Card>
        )}
      </div>
    </Card>
  );
}
