/**
 * Pure models and helpers for the wiki activity tab (plan wiki-obsidian-panel
 * §2.1). No fetching here — WikiTabData.ts owns the network layer. The tree
 * and wikilink resolution build on the lightweight `pages` listing; the graph
 * payload is only consumed by the lazy graph view.
 */

export type WikiMode = "wiki" | "code" | "ask" | "research";

export interface WikiPageMeta {
  /** Vault-relative path including the .md suffix. */
  path: string;
  title: string;
  tags: string[];
  contentHash: string | null;
  updatedAt: string | null;
  /** Frontmatter aliases — only known once a page body has been read. */
  aliases?: string[];
}

export interface WikiOutputMeta {
  path: string;
  size: number | null;
  modified: string | null;
}

export interface WikiGraphNode {
  id: string;
  kind: string;
  path: string | null;
  title: string | null;
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  kind: string;
  rawTarget: string | null;
}

export interface WikiGraphPayload {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
  degraded: boolean;
  degradedSources: string[];
  analytics: Record<string, unknown> | null;
}

export type WikiPageKind =
  | "concept"
  | "topic"
  | "source"
  | "recap"
  | "output"
  | "code"
  | "root"
  | "other";

const KIND_BY_PREFIX: ReadonlyArray<readonly [string, WikiPageKind]> = [
  ["knowledge/concepts/", "concept"],
  ["knowledge/topics/", "topic"],
  ["knowledge/sources/", "source"],
  ["recaps/", "recap"],
  ["outputs/", "output"],
  ["code/", "code"],
];

export function pageKindFromPath(path: string): WikiPageKind {
  for (const [prefix, kind] of KIND_BY_PREFIX) {
    if (path.startsWith(prefix)) return kind;
  }
  if (!path.includes("/")) return "root";
  return "other";
}

function stripMarkdownSuffix(segment: string): string {
  return segment.endsWith(".md") ? segment.slice(0, -3) : segment;
}

export interface BreadcrumbSegment {
  label: string;
  /** Cumulative path prefix; the leaf keeps the full page path. */
  prefix: string;
}

export function breadcrumbSegments(path: string): BreadcrumbSegment[] {
  const segments = path.split("/").filter(Boolean);
  return segments.map((segment, index) => {
    const isLeaf = index === segments.length - 1;
    return {
      label: isLeaf ? stripMarkdownSuffix(segment) : segment,
      prefix: isLeaf ? path : segments.slice(0, index + 1).join("/"),
    };
  });
}

const CODE_FILES_PREFIX = "code/files/";

/** `code/files/src/gobby/runner.py.md` → `src/gobby/runner.py`. */
export function codePathToSourcePath(path: string): string | null {
  if (!path.startsWith(CODE_FILES_PREFIX) || !path.endsWith(".md")) return null;
  const source = path.slice(CODE_FILES_PREFIX.length, -3);
  return source.length > 0 ? source : null;
}

export interface PageTreeNode {
  /** Display segment (leaf labels drop the .md suffix). */
  name: string;
  /** Folder nodes carry the cumulative prefix; leaves carry the page path. */
  path: string;
  kind: "folder" | "page" | "output";
  page: WikiPageMeta | null;
  output: WikiOutputMeta | null;
  children: PageTreeNode[];
}

interface TreeLeaf {
  path: string;
  page: WikiPageMeta | null;
  output: WikiOutputMeta | null;
}

function insertLeaf(roots: PageTreeNode[], leaf: TreeLeaf): void {
  const segments = leaf.path.split("/").filter(Boolean);
  let level = roots;
  let prefix = "";
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    const isLeaf = index === segments.length - 1;
    prefix = prefix ? `${prefix}/${segment}` : segment;
    if (isLeaf) {
      level.push({
        name: stripMarkdownSuffix(segment),
        path: leaf.path,
        kind: leaf.output ? "output" : "page",
        page: leaf.page,
        output: leaf.output,
        children: [],
      });
      return;
    }
    let folder = level.find((node) => node.kind === "folder" && node.name === segment);
    if (!folder) {
      folder = { name: segment, path: prefix, kind: "folder", page: null, output: null, children: [] };
      level.push(folder);
    }
    level = folder.children;
  }
}

function sortLevel(nodes: PageTreeNode[]): void {
  nodes.sort((a, b) => {
    const aFolder = a.kind === "folder" ? 0 : 1;
    const bFolder = b.kind === "folder" ? 0 : 1;
    if (aFolder !== bFolder) return aFolder - bFolder;
    return a.name.localeCompare(b.name);
  });
  for (const node of nodes) sortLevel(node.children);
}

/**
 * Group pages and pipeline outputs into a folder tree by path segment.
 * `rootFilter` scopes the tree for a mode (e.g. only `code/` in code mode);
 * the builder is otherwise data-driven and assumes nothing about vault roots.
 */
export function buildPageTree(
  pages: WikiPageMeta[],
  outputs: WikiOutputMeta[],
  rootFilter?: (path: string) => boolean,
): PageTreeNode[] {
  const roots: PageTreeNode[] = [];
  for (const page of pages) {
    if (rootFilter && !rootFilter(page.path)) continue;
    insertLeaf(roots, { path: page.path, page, output: null });
  }
  for (const output of outputs) {
    if (rootFilter && !rootFilter(output.path)) continue;
    insertLeaf(roots, { path: output.path, page: null, output });
  }
  sortLevel(roots);
  return roots;
}

export interface WikiNodeIndex {
  /** Exact page path (with .md) → metadata. */
  byPath: Map<string, WikiPageMeta>;
  /** Normalized title/alias → page path. */
  byTitle: Map<string, string>;
}

function normalizeTitleKey(value: string): string {
  return value.trim().toLowerCase();
}

export function buildNodeIndex(pages: WikiPageMeta[]): WikiNodeIndex {
  const byPath = new Map<string, WikiPageMeta>();
  const byTitle = new Map<string, string>();
  for (const page of pages) {
    byPath.set(page.path, page);
    if (page.title) {
      const key = normalizeTitleKey(page.title);
      if (!byTitle.has(key)) byTitle.set(key, page.path);
    }
    for (const alias of page.aliases ?? []) {
      const key = normalizeTitleKey(alias);
      if (!byTitle.has(key)) byTitle.set(key, page.path);
    }
  }
  return { byPath, byTitle };
}

/**
 * Resolve a wikilink/citation target to a page path: exact path first (with
 * or without the .md suffix), then normalized title/alias. Null when the
 * target has no page yet.
 */
export function resolveWikilinkTarget(index: WikiNodeIndex, target: string): string | null {
  const trimmed = target.trim();
  if (!trimmed) return null;
  if (index.byPath.has(trimmed)) return trimmed;
  const withSuffix = `${trimmed}.md`;
  if (index.byPath.has(withSuffix)) return withSuffix;
  return index.byTitle.get(normalizeTitleKey(trimmed)) ?? null;
}

/**
 * Graph node kind → design token var. Deutan-safe per .impeccable.md: state
 * is never hue-only (the graph view pairs color with node labels/shape), and
 * unresolved targets use the muted text token rather than a hue.
 */
export const WIKI_NODE_COLOR_VARS: Record<string, string> = {
  wiki_page: "--accent",
  code: "--color-info",
  document: "--color-success-foreground",
  source: "--color-warning-foreground",
  citation: "--color-review",
  unresolved_target: "--text-muted",
};

export function wikiNodeColorVar(kind: string): string {
  return WIKI_NODE_COLOR_VARS[kind] ?? "--text-muted";
}

const WIKI_NODE_VAL_MAX = 20;

/** Force-graph node size: 2 + 3·√degree, clamped to [2, 20]. */
export function wikiNodeVal(degree: number): number {
  const val = 2 + 3 * Math.sqrt(Math.max(degree, 0));
  return Math.min(val, WIKI_NODE_VAL_MAX);
}

const CREATE_PATH_PATTERN = /^[a-z0-9\-/_.]+$/;
const CREATE_PATH_RULE =
  "Paths must resolve under knowledge/, use a-z 0-9 - _ . /, and end in .md.";

/** Mirrors gwiki write confinement: knowledge/**, markdown file, no traversal. */
export function validateCreatePath(path: string): string | null {
  const segments = path.split("/");
  const name = segments[segments.length - 1] ?? "";
  const traversal = segments.some(
    (segment) => segment === "" || segment === "." || segment === "..",
  );
  const valid =
    CREATE_PATH_PATTERN.test(path) &&
    path.startsWith("knowledge/") &&
    !traversal &&
    name.endsWith(".md") &&
    name !== ".md";
  return valid ? null : CREATE_PATH_RULE;
}

/** Normalize a wikilink target or folder prefix into a create-form seed. */
export function seedCreatePath(target: string): string {
  const base = target.startsWith("knowledge/") ? target : `knowledge/${target}`;
  if (base.endsWith("/") || base.endsWith(".md")) return base;
  return `${base}.md`;
}
