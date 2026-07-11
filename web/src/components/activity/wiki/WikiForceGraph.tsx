/**
 * §4.1 kapsule-safe 2D canvas graph. react-force-graph re-applies props on
 * every parent render and destroys custom canvas objects, so this component
 * sits behind a memo wall keyed on (dataRevision, theme, size, reducedMotion)
 * only. Interaction state and callbacks arrive through parent-owned refs —
 * the ref identities are stable, so the wall never starves the accessors —
 * and `autoPauseRedraw={false}` keeps the graph's internal rAF repainting
 * them with zero React state updates per pointer-move.
 */

import {
  memo,
  useCallback,
  useMemo,
  useRef,
  type KeyboardEvent,
  type MutableRefObject,
} from "react";
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from "react-force-graph-2d";

import { useRafCoalescedHandler } from "../../../hooks/useRafCoalescedHandler";
import {
  nodeMatchesSearch,
  type WikiGraphHandlers,
  type WikiGraphInteraction,
  type WikiGraphScene,
  type WikiGraphSceneLink,
  type WikiGraphSceneNode,
} from "./WikiGraphScene";

type GraphNode = NodeObject<WikiGraphSceneNode>;
type GraphLink = LinkObject<WikiGraphSceneNode, WikiGraphSceneLink>;
type GraphHandle = ForceGraphMethods<GraphNode, GraphLink>;

const NODE_REL_SIZE = 4;
/** Obsidian behavior: labels appear only once zoomed in past this scale. */
const LABEL_SCALE_THRESHOLD = 1.4;
const SEARCH_DIM_ALPHA = 0.15;
const HOVER_DIM_ALPHA = 0.3;
const HOLLOW_RING_DASH = [2, 2];
const CODE_EDGE_DASH = [4, 2];
const ZOOM_STEP = 1.4;

interface WikiForceGraphProps {
  scene: WikiGraphScene;
  /** Bumps only when filters/scope/theme change the node/edge set. */
  dataRevision: string;
  theme: "light" | "dark";
  width: number;
  height: number;
  reducedMotion: boolean;
  interactionRef: MutableRefObject<WikiGraphInteraction>;
  handlersRef: MutableRefObject<WikiGraphHandlers>;
}

function nodeRadius(node: GraphNode): number {
  return Math.sqrt(Math.max(node.val, 1)) * NODE_REL_SIZE;
}

/** force-graph mutates link endpoints from id strings into node objects. */
function endpointId(endpoint: unknown): string | null {
  if (typeof endpoint === "object" && endpoint !== null) {
    const id = (endpoint as { id?: unknown }).id;
    return typeof id === "string" ? id : null;
  }
  return typeof endpoint === "string" ? endpoint : null;
}

function WikiForceGraphInner({
  scene,
  width,
  height,
  reducedMotion,
  interactionRef,
  handlersRef,
}: WikiForceGraphProps) {
  const fgRef = useRef<GraphHandle | undefined>(undefined);
  const fittedRef = useRef(false);
  const motionMs = reducedMotion ? 0 : 200;
  const nodesById = useMemo(
    () => new Map(scene.nodes.map((node) => [node.id, node])),
    [scene],
  );

  const applyHover = useCallback(
    (node: GraphNode | null) => {
      interactionRef.current.hoverId = node && typeof node.id === "string" ? node.id : null;
    },
    [interactionRef],
  );
  const handleHover = useRafCoalescedHandler<GraphNode | null>(applyHover);
  const onNodeHover = useCallback(
    (node: GraphNode | null) => handleHover(node),
    [handleHover],
  );

  const onNodeClick = useCallback(
    (node: GraphNode) => handlersRef.current.onNodeClick(node),
    [handlersRef],
  );

  const nodeAlpha = useCallback(
    (node: GraphNode): number => {
      const interaction = interactionRef.current;
      if (interaction.search && !nodeMatchesSearch(node, interaction.search)) {
        return SEARCH_DIM_ALPHA;
      }
      const hoverId = interaction.hoverId;
      if (hoverId && hoverId !== node.id && !scene.adjacency.get(hoverId)?.has(node.id)) {
        return HOVER_DIM_ALPHA;
      }
      return 1;
    },
    [interactionRef, scene],
  );

  const nodeCanvasObject = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const interaction = interactionRef.current;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const radius = nodeRadius(node);
      const hovered = interaction.hoverId === node.id;
      const neighbor =
        interaction.hoverId !== null &&
        (scene.adjacency.get(interaction.hoverId)?.has(node.id) ?? false);
      const matched = interaction.search !== "" && nodeMatchesSearch(node, interaction.search);

      ctx.save();
      ctx.globalAlpha = nodeAlpha(node);
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI, false);
      if (node.hollow) {
        // Shape + lightness cue, never hue alone: hollow disc with a dashed
        // error-token ring marks unresolved targets even in grayscale.
        ctx.fillStyle = scene.colors.hollowFill;
        ctx.fill();
        ctx.lineWidth = 1.5 / globalScale;
        ctx.strokeStyle = node.color;
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, radius + 2 / globalScale, 0, 2 * Math.PI, false);
        ctx.setLineDash(HOLLOW_RING_DASH);
        ctx.strokeStyle = scene.colors.hollowRing;
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = node.communityColor ?? node.color;
        ctx.fill();
        if (node.communityColor) {
          // Kind ring under community fill keeps the kind readable.
          ctx.lineWidth = 1.5 / globalScale;
          ctx.strokeStyle = node.color;
          ctx.stroke();
        }
      }

      if (globalScale > LABEL_SCALE_THRESHOLD || hovered || neighbor || matched) {
        const fontSize = 12 / globalScale;
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = scene.colors.label;
        ctx.fillText(node.label, x, y + radius + 2 / globalScale);
      }
      ctx.restore();
    },
    [interactionRef, nodeAlpha, scene],
  );

  const nodePointerAreaPaint = useCallback(
    (node: GraphNode, color: string, ctx: CanvasRenderingContext2D) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, nodeRadius(node) + 4, 0, 2 * Math.PI, false);
      ctx.fill();
    },
    [],
  );

  const linkColor = useCallback(
    (link: GraphLink): string => {
      const interaction = interactionRef.current;
      const sourceId = endpointId(link.source);
      const targetId = endpointId(link.target);
      if (
        interaction.hoverId &&
        (sourceId === interaction.hoverId || targetId === interaction.hoverId)
      ) {
        return scene.colors.linkHighlight;
      }
      if (interaction.search) {
        const sourceNode = sourceId ? nodesById.get(sourceId) : undefined;
        const targetNode = targetId ? nodesById.get(targetId) : undefined;
        const anyMatch =
          (sourceNode !== undefined && nodeMatchesSearch(sourceNode, interaction.search)) ||
          (targetNode !== undefined && nodeMatchesSearch(targetNode, interaction.search));
        if (!anyMatch) return scene.colors.linkDim;
      }
      return scene.colors.link;
    },
    [interactionRef, nodesById, scene],
  );

  const linkLineDash = useCallback(
    (link: GraphLink): number[] | null => (link.dashed ? CODE_EDGE_DASH : null),
    [],
  );

  const onEngineStop = useCallback(() => {
    if (fittedRef.current) return;
    fittedRef.current = true;
    fgRef.current?.zoomToFit(reducedMotion ? 0 : 400, 40);
  }, [reducedMotion]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const fg = fgRef.current;
      if (!fg) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        fg.zoom(fg.zoom() * ZOOM_STEP, motionMs);
        return;
      }
      if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        fg.zoom(fg.zoom() / ZOOM_STEP, motionMs);
        return;
      }
      if (!event.key.startsWith("Arrow")) return;
      event.preventDefault();
      const center = fg.centerAt() ?? { x: 0, y: 0 };
      const step = 60 / (fg.zoom() || 1);
      if (event.key === "ArrowLeft") fg.centerAt(center.x - step, center.y, motionMs);
      if (event.key === "ArrowRight") fg.centerAt(center.x + step, center.y, motionMs);
      if (event.key === "ArrowUp") fg.centerAt(center.x, center.y - step, motionMs);
      if (event.key === "ArrowDown") fg.centerAt(center.x, center.y + step, motionMs);
    },
    [motionMs],
  );

  return (
    <div
      role="application"
      aria-label={`Graph of ${scene.nodes.length} pages with ${scene.links.length} connections. Press plus or minus to zoom and arrow keys to pan.`}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="h-full w-full outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
    >
      <ForceGraph2D
        ref={fgRef}
        width={width}
        height={height}
        graphData={{ nodes: scene.nodes, links: scene.links }}
        nodeRelSize={NODE_REL_SIZE}
        nodeVal={(node: GraphNode) => node.val}
        nodeLabel={() => ""}
        nodeCanvasObject={nodeCanvasObject}
        nodePointerAreaPaint={nodePointerAreaPaint}
        linkColor={linkColor}
        linkLineDash={linkLineDash}
        autoPauseRedraw={false}
        warmupTicks={reducedMotion ? 200 : 80}
        cooldownTicks={reducedMotion ? 0 : 100}
        onNodeClick={onNodeClick}
        onNodeHover={onNodeHover}
        onEngineStop={onEngineStop}
      />
    </div>
  );
}

/**
 * Equality on the revision key alone (plus theme/size/motion): parent
 * re-renders with an unchanged scene must not reach the kapsule.
 */
const WikiForceGraph = memo(
  WikiForceGraphInner,
  (prev, next) =>
    prev.dataRevision === next.dataRevision &&
    prev.theme === next.theme &&
    prev.width === next.width &&
    prev.height === next.height &&
    prev.reducedMotion === next.reducedMotion,
);

export default WikiForceGraph;
