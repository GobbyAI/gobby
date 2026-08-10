/**
 * Backlinks section (plan wiki-obsidian-panel §3.1): collapsible "Linked
 * mentions" fed by GET /api/wiki/backlinks, plus "Unresolved mentions"
 * derived from graph links edges once the lazy graph payload is present.
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { fetchBacklinks, type WikiBacklink, type WikiFetchScope } from "./WikiTabData";
import type { WikiGraphPayload, WikiNodeIndex } from "./WikiTabModel";

type BacklinksState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; backlinks: WikiBacklink[] };

interface WikiBacklinksProps {
  scope: WikiFetchScope;
  /** Page whose mentions are listed (vault path with .md). */
  path: string;
  nodeIndex: WikiNodeIndex;
  /** Lazily fetched — unresolved mentions stay hidden until present. */
  graph: WikiGraphPayload | null;
  /** Fired on first expand so the host can start the lazy graph fetch. */
  onExpand: () => void;
  onOpen: (path: string) => void;
}

export function WikiBacklinks({
  scope,
  path,
  nodeIndex,
  graph,
  onExpand,
  onOpen,
}: WikiBacklinksProps) {
  const [expanded, setExpanded] = useState(false);
  const requestKey = `${scope.projectId ?? ""}:${scope.topic ?? ""}:${path}`;
  const [result, setResult] = useState<{
    key: string;
    state: Exclude<BacklinksState, { status: "loading" }>;
  } | null>(null);

  useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    fetchBacklinks(scope, path)
      .then((backlinks) => {
        if (!cancelled) setResult({ key: requestKey, state: { status: "ready", backlinks } });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message = error instanceof Error ? error.message : "Failed to load backlinks";
          setResult({ key: requestKey, state: { status: "error", message } });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, path, requestKey, scope]);

  const state: BacklinksState =
    result && result.key === requestKey ? result.state : { status: "loading" };

  const pathSansSuffix = path.replace(/\.md$/, "");
  const unresolvedMentions = useMemo(() => {
    if (!graph) return [];
    const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
    return graph.edges
      .filter((edge) => {
        if (edge.kind !== "links") return false;
        const target = nodeById.get(edge.target);
        if (target?.kind !== "unresolved_target") return false;
        const raw = (edge.rawTarget ?? target.title ?? "").split("|")[0] ?? "";
        return raw === pathSansSuffix || raw === path;
      })
      .map((edge) => nodeById.get(edge.source))
      .filter((node): node is NonNullable<typeof node> => Boolean(node?.path));
  }, [graph, path, pathSansSuffix]);

  const titleFor = (targetPath: string) =>
    nodeIndex.byPath.get(targetPath)?.title ?? targetPath.replace(/\.md$/, "");

  const toggle = () => {
    setExpanded((current) => {
      if (!current) onExpand();
      return !current;
    });
  };

  return (
    <section aria-label="Linked mentions" className="mt-6 border-t border-border pt-3">
      <Button
        type="button"
        aria-expanded={expanded}
        variant="ghost"
        size="sm"
        className={`${coarseHitAreaCls} w-full justify-start gap-1 px-0 text-left text-xs uppercase tracking-wide`}
        onClick={toggle}
      >
        <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        Linked mentions
      </Button>

      {expanded ? (
        <div className="mt-2 flex flex-col gap-3">
          {state.status === "loading" ? (
            <p role="status" className="text-xs text-muted-foreground">
              Loading mentions…
            </p>
          ) : null}
          {state.status === "error" ? (
            <p role="alert" className="text-xs text-muted-foreground">
              {state.message}
            </p>
          ) : null}
          {state.status === "ready" ? (
            state.backlinks.length > 0 ? (
              <ul className="flex list-none flex-col gap-0.5">
                {state.backlinks.map((backlink) => (
                  <li key={backlink.sourcePath}>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className={`${coarseHitAreaCls} w-full justify-start px-1.5 text-left text-sm text-foreground`}
                      onClick={() => onOpen(backlink.sourcePath)}
                    >
                      {titleFor(backlink.sourcePath)}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">No pages link here yet.</p>
            )
          ) : null}

          {graph && unresolvedMentions.length > 0 ? (
            <div>
              <h3 className="text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
                Unresolved mentions
              </h3>
              <ul className="mt-1 flex list-none flex-col gap-0.5">
                {unresolvedMentions.map((node) => (
                  <li key={node.id}>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className={`${coarseHitAreaCls} w-full justify-start px-1.5 text-left text-sm`}
                      onClick={() => node.path && onOpen(node.path)}
                    >
                      {node.title ?? node.path}
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
