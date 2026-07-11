/**
 * §4.1 pure scene builder for the 2D force graph: filters node kinds and edge
 * layers, prunes orphans, caps by descending degree, and bakes design tokens
 * into node/link objects once per (payload, options, theme) build so the
 * canvas accessors never resolve CSS variables per frame.
 */

import { resolveCssVar } from "../../../lib/utils";
import {
  wikiNodeColorVar,
  wikiNodeVal,
  type WikiGraphPayload,
} from "./WikiTabModel";

/** Hard node budget — beyond it the scene keeps the top nodes by degree. */
export const MAX_GRAPH_NODES = 1500;

export type ResolveColor = (varName: string, alpha?: number) => string;

export interface WikiGraphSceneOptions {
  /** Show `source` and `citation` nodes (adds the trust/audit endpoints). */
  sources: boolean;
  /** Show `unresolved_target` nodes. */
  unresolved: boolean;
  /** Keep nodes with no visible edges. */
  orphans: boolean;
  /** `trust` edge layer (document → source). */
  trust: boolean;
  /** `audit` edge layer (citation → document); doubles the edge count. */
  audit: boolean;
  /** Code `imports`/`calls` layers, drawn dashed. */
  codeEdges: boolean;
  /** Cycle community colors over the chart-series tokens. */
  communities: boolean;
}

export interface WikiGraphSceneNode {
  id: string;
  path: string | null;
  label: string;
  kind: string;
  /** force-graph node size input (area ∝ val). */
  val: number;
  degree: number;
  color: string;
  hollow: boolean;
  communityColor: string | null;
  x?: number;
  y?: number;
}

export interface WikiGraphSceneLink {
  source: string;
  target: string;
  kind: string;
  dashed: boolean;
}

export interface WikiGraphSceneColors {
  link: string;
  linkDim: string;
  linkHighlight: string;
  label: string;
  hollowFill: string;
  hollowRing: string;
}

export interface WikiGraphScene {
  nodes: WikiGraphSceneNode[];
  links: WikiGraphSceneLink[];
  /** Undirected neighbor map over the kept edges, for hover highlighting. */
  adjacency: Map<string, Set<string>>;
  /** Visible node count before the cap was applied. */
  totalNodes: number;
  capped: boolean;
  communityCount: number;
  colors: WikiGraphSceneColors;
}

/** Interaction state read by canvas accessors every frame — never React state. */
export interface WikiGraphInteraction {
  hoverId: string | null;
  /** Lowercased search query; empty string means no filter. */
  search: string;
}

/** Callbacks routed through a stable ref so the memo wall never goes stale. */
export interface WikiGraphHandlers {
  onNodeClick: (node: WikiGraphSceneNode) => void;
}

const SOURCE_KINDS = new Set(["source", "citation"]);
const CODE_EDGE_KINDS = new Set(["imports", "calls"]);

function edgeLayerEnabled(kind: string, options: WikiGraphSceneOptions): boolean {
  if (kind === "links") return true;
  if (kind === "trust") return options.trust;
  if (kind === "audit") return options.audit;
  // `callers` mirrors `calls` in reverse and is never rendered.
  if (CODE_EDGE_KINDS.has(kind)) return options.codeEdges;
  return false;
}

/** `analytics.centrality` entries look like `{node: {id}, degree, score}`. */
function centralityDegrees(analytics: Record<string, unknown> | null): Map<string, number> {
  const degrees = new Map<string, number>();
  if (!analytics || !Array.isArray(analytics.centrality)) return degrees;
  for (const value of analytics.centrality) {
    if (typeof value !== "object" || value === null) continue;
    const record = value as Record<string, unknown>;
    const node = record.node;
    const id =
      typeof node === "object" && node !== null
        ? (node as Record<string, unknown>).id
        : null;
    if (typeof id === "string" && typeof record.degree === "number") {
      degrees.set(id, record.degree);
    }
  }
  return degrees;
}

/** `analytics.communities` entries look like `{id, nodes: [{id}], weight}`. */
function communityOrdinals(
  analytics: Record<string, unknown> | null,
): { byNode: Map<string, number>; count: number } {
  const byNode = new Map<string, number>();
  if (!analytics || !Array.isArray(analytics.communities)) return { byNode, count: 0 };
  let ordinal = 0;
  for (const value of analytics.communities) {
    if (typeof value !== "object" || value === null) continue;
    const members = (value as Record<string, unknown>).nodes;
    if (!Array.isArray(members)) continue;
    for (const member of members) {
      if (typeof member !== "object" || member === null) continue;
      const id = (member as Record<string, unknown>).id;
      if (typeof id === "string") byNode.set(id, ordinal);
    }
    ordinal += 1;
  }
  return { byNode, count: ordinal };
}

export function communityColorVar(ordinal: number): string {
  return `--chart-series-${(ordinal % 6) + 1}`;
}

export function nodeMatchesSearch(
  node: Pick<WikiGraphSceneNode, "label" | "path">,
  query: string,
): boolean {
  if (!query) return true;
  return (
    node.label.toLowerCase().includes(query) ||
    (node.path ?? "").toLowerCase().includes(query)
  );
}

export function buildGraphScene(
  payload: WikiGraphPayload,
  options: WikiGraphSceneOptions,
  resolveColor: ResolveColor = resolveCssVar,
): WikiGraphScene {
  const visible = payload.nodes.filter((node) => {
    if (SOURCE_KINDS.has(node.kind)) return options.sources;
    if (node.kind === "unresolved_target") return options.unresolved;
    return true;
  });
  const visibleIds = new Set(visible.map((node) => node.id));

  const layerEdges = payload.edges.filter(
    (edge) =>
      edgeLayerEnabled(edge.kind, options) &&
      visibleIds.has(edge.source) &&
      visibleIds.has(edge.target),
  );

  const localDegree = new Map<string, number>();
  for (const edge of layerEdges) {
    localDegree.set(edge.source, (localDegree.get(edge.source) ?? 0) + 1);
    localDegree.set(edge.target, (localDegree.get(edge.target) ?? 0) + 1);
  }

  const analyticDegree = centralityDegrees(payload.analytics);
  const degreeOf = (id: string) => analyticDegree.get(id) ?? localDegree.get(id) ?? 0;

  let kept = options.orphans
    ? visible
    : visible.filter((node) => (localDegree.get(node.id) ?? 0) > 0);

  const totalNodes = kept.length;
  const capped = kept.length > MAX_GRAPH_NODES;
  if (capped) {
    kept = [...kept]
      .sort((a, b) => degreeOf(b.id) - degreeOf(a.id))
      .slice(0, MAX_GRAPH_NODES);
  }
  const keptIds = new Set(kept.map((node) => node.id));

  const links: WikiGraphSceneLink[] = layerEdges
    .filter((edge) => keptIds.has(edge.source) && keptIds.has(edge.target))
    .map((edge) => ({
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      dashed: CODE_EDGE_KINDS.has(edge.kind),
    }));

  const adjacency = new Map<string, Set<string>>();
  const connect = (from: string, to: string) => {
    const set = adjacency.get(from);
    if (set) {
      set.add(to);
    } else {
      adjacency.set(from, new Set([to]));
    }
  };
  for (const link of links) {
    connect(link.source, link.target);
    connect(link.target, link.source);
  }

  const communities = options.communities
    ? communityOrdinals(payload.analytics)
    : { byNode: new Map<string, number>(), count: 0 };

  const kindColors = new Map<string, string>();
  const colorForKind = (kind: string) => {
    const cached = kindColors.get(kind);
    if (cached !== undefined) return cached;
    const color = resolveColor(wikiNodeColorVar(kind));
    kindColors.set(kind, color);
    return color;
  };

  const nodes: WikiGraphSceneNode[] = kept.map((node) => {
    const degree = degreeOf(node.id);
    const ordinal = communities.byNode.get(node.id);
    return {
      id: node.id,
      path: node.path,
      label: node.title ?? node.path ?? node.id,
      kind: node.kind,
      val: wikiNodeVal(degree),
      degree,
      color: colorForKind(node.kind),
      hollow: node.kind === "unresolved_target",
      communityColor:
        ordinal === undefined ? null : resolveColor(communityColorVar(ordinal)),
    };
  });

  return {
    nodes,
    links,
    adjacency,
    totalNodes,
    capped,
    communityCount: communities.count,
    colors: {
      link: resolveColor("--border"),
      linkDim: resolveColor("--border", 0.15),
      linkHighlight: resolveColor("--accent-soft"),
      label: resolveColor("--text-primary"),
      hollowFill: resolveColor("--bg-primary"),
      hollowRing: resolveColor("--color-destructive-foreground"),
    },
  };
}
