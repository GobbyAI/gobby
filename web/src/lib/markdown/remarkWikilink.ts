/**
 * Obsidian-style wikilink support for the shared markdown pipeline
 * (plan wiki-obsidian-panel §2.3). Pure remark: splits `text` nodes on
 * `[[target|alias]]` / `![[target]]` syntax and inserts standard `link`
 * nodes with a `wikilink:` URL scheme — no rehype, no HTML parsing.
 * Embeds degrade to the same plain links.
 */

import type { Link, Parent, PhrasingContent, Root, Text } from "mdast";

export interface WikilinkResolution {
  path: string;
}

export interface RemarkWikilinkOptions {
  /**
   * Resolver backed by the wiki node index (§2.1 `resolveWikilinkTarget`):
   * return null to mark the target unresolved. Anchors are stripped before
   * resolution — the index has no heading entries. Without a resolver,
   * links render resolved-optimistic.
   */
  resolve?: (target: string) => WikilinkResolution | null;
}

const WIKILINK_PATTERN = /(!?)\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g;

/** Parents whose text children must not become nested links. */
const NON_TRANSFORM_PARENTS = new Set(["link", "linkReference"]);

/** Label for an alias-less target: last path segment sans `.md`, anchor kept. */
function deriveLabel(target: string): string {
  const [pagePart, ...anchorParts] = target.split("#");
  const segments = (pagePart ?? "").split("/");
  const last = segments[segments.length - 1] ?? "";
  const base = last.endsWith(".md") ? last.slice(0, -3) : last;
  const anchor = anchorParts.length > 0 ? `#${anchorParts.join("#")}` : "";
  return `${base}${anchor}` || target;
}

function buildLink(
  target: string,
  alias: string | undefined,
  resolve: RemarkWikilinkOptions["resolve"],
): Link {
  const pagePart = target.split("#")[0] ?? "";
  const resolved = resolve ? resolve(pagePart) !== null : true;
  const hProperties: Record<string, string> = {
    className: resolved ? "wikilink" : "wikilink wikilink--unresolved",
    "data-wiki-target": target,
  };
  if (!resolved) {
    hProperties["aria-description"] = "Page not created yet";
  }
  return {
    type: "link",
    url: `wikilink:${encodeURIComponent(target)}`,
    children: [{ type: "text", value: alias ?? deriveLabel(target) }],
    data: { hProperties },
  };
}

function splitTextNode(
  node: Text,
  resolve: RemarkWikilinkOptions["resolve"],
): PhrasingContent[] | null {
  const { value } = node;
  WIKILINK_PATTERN.lastIndex = 0;
  const out: PhrasingContent[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = WIKILINK_PATTERN.exec(value)) !== null) {
    if (match.index > last) {
      out.push({ type: "text", value: value.slice(last, match.index) });
    }
    out.push(buildLink(match[2] ?? "", match[3], resolve));
    last = match.index + match[0].length;
  }
  if (out.length === 0) return null;
  if (last < value.length) {
    out.push({ type: "text", value: value.slice(last) });
  }
  return out;
}

function walk(parent: Parent, resolve: RemarkWikilinkOptions["resolve"]): void {
  const next: Parent["children"] = [];
  let changed = false;
  for (const child of parent.children) {
    if (child.type === "text") {
      const split = splitTextNode(child, resolve);
      if (split) {
        changed = true;
        next.push(...split);
        continue;
      }
      next.push(child);
      continue;
    }
    if ("children" in child && !NON_TRANSFORM_PARENTS.has(child.type)) {
      walk(child, resolve);
    }
    next.push(child);
  }
  if (changed) {
    parent.children = next;
  }
}

/**
 * Plain plugin function (no `this` usage) so it works both as a
 * react-markdown/unified plugin and as a directly-callable transformer
 * factory in tests.
 */
export function remarkWikilink(
  options: RemarkWikilinkOptions = {},
): (tree: Root) => void {
  const { resolve } = options;
  return (tree: Root): void => {
    walk(tree, resolve);
  };
}
