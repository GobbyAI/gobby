/**
 * Wiki page reader (plan wiki-obsidian-panel §3.1): breadcrumb strip with
 * history controls, frontmatter header, and the markdown body rendered
 * through MarkdownBody with wikilink navigation and mermaid diagrams.
 */

import {
  useEffect,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import type { Components } from "react-markdown";
import type { PluggableList } from "unified";

import { Anchor } from "../../chat/CodeBlockRenderers";
import { MarkdownBody, markdownBodyClassName } from "../../shared/MarkdownBody";
import { MermaidBlock } from "../../shared/MermaidBlock";
import { Button } from "../../ui/Button";
import { Card } from "../../ui/Card";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { remarkWikilink } from "../../../lib/markdown/remarkWikilink";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import {
  fetchPage,
  type WikiFetchScope,
  type WikiPageDetail,
} from "./WikiTabData";
import {
  breadcrumbSegments,
  codePathToSourcePath,
  resolveWikilinkTarget,
  seedCreatePath,
  type WikiGraphPayload,
  type WikiNodeIndex,
} from "./WikiTabModel";
import type { WikiNav } from "./WikiTabState";

const WIKILINK_PREFIX = "wikilink:";

type ReadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; detail: WikiPageDetail };

function Chevron({ direction }: { direction: "left" | "right" }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      {direction === "left" ? (
        <path d="M10 3 5 8l5 5" />
      ) : (
        <path d="M6 3l5 5-5 5" />
      )}
    </svg>
  );
}

/** The header already renders the title as an H1 — drop a duplicate leading
 * `# Title` line from compiled page bodies. */
function stripLeadingTitleHeading(body: string, title: string): string {
  const match = /^\s*#\s+(.+?)\s*\r?\n/.exec(body);
  if (!match) return body;
  if (match[1]?.trim().toLowerCase() !== title.trim().toLowerCase())
    return body;
  return body.slice(match[0].length).replace(/^\r?\n/, "");
}

/** Wikilink targets under a `## Citations` heading, in document order. */
function citationsFromBody(
  body: string,
): Array<{ target: string; label: string }> {
  const section = /^##\s+Citations\s*$/m.exec(body);
  if (!section) return [];
  const rest = body.slice(section.index + section[0].length);
  const end = rest.search(/^##\s+/m);
  const scoped = end === -1 ? rest : rest.slice(0, end);
  const citations: Array<{ target: string; label: string }> = [];
  for (const match of scoped.matchAll(/\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]/g)) {
    const target = match[1]?.trim();
    if (!target) continue;
    citations.push({ target, label: match[2]?.trim() || target });
  }
  return citations;
}

interface WikiPageReaderProps {
  scope: WikiFetchScope;
  path: string;
  nav: WikiNav;
  nodeIndex: WikiNodeIndex;
  /** Lazily loaded — trust-edge sources appear once present. */
  graph: WikiGraphPayload | null;
  onOpenGraph: () => void;
  onDelete?: (path: string) => void;
  /** Edit toggle — hidden for read-only pages or when the host doesn't wire it. */
  onToggleEdit?: () => void;
  /** Create-form opener; receives the prefilled path seed. */
  onCreate?: (seed: string) => void;
  /** Rendered below the body — the backlinks section slot. */
  footer?: ReactNode;
}

export function WikiPageReader({
  scope,
  path,
  nav,
  nodeIndex,
  graph,
  onOpenGraph,
  onDelete,
  onToggleEdit,
  onCreate,
  footer,
}: WikiPageReaderProps) {
  // Results are keyed by the request they answered; a key mismatch during
  // render means the current request is in flight, so "loading" is derived
  // instead of set synchronously in the effect.
  const requestKey = `${scope.projectId ?? ""}:${scope.topic ?? ""}:${path}`;
  const [result, setResult] = useState<{
    key: string;
    state: Exclude<ReadState, { status: "loading" }>;
  } | null>(null);
  // Keyed to the request so a navigation clears the notice by derivation —
  // no reset effect (react-hooks/set-state-in-effect).
  const [missing, setMissing] = useState<{
    key: string;
    target: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPage(scope, { path })
      .then((detail) => {
        if (!cancelled)
          setResult({ key: requestKey, state: { status: "ready", detail } });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const message =
            error instanceof Error ? error.message : "Failed to read page";
          setResult({ key: requestKey, state: { status: "error", message } });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [path, requestKey, scope]);

  const state: ReadState =
    result && result.key === requestKey ? result.state : { status: "loading" };
  const missingTarget =
    missing && missing.key === requestKey ? missing.target : null;

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
      code: MermaidBlock as Components["code"],
      a: (props) => {
        const { href, children, node: _node, ...rest } = props;
        if (!href?.startsWith(WIKILINK_PREFIX)) {
          return (
            <Anchor href={href} {...rest}>
              {children}
            </Anchor>
          );
        }
        const rawTarget = decodeURIComponent(
          href.slice(WIKILINK_PREFIX.length),
        );
        const pagePart = rawTarget.split("#")[0] ?? "";
        const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
          event.preventDefault();
          const resolved = resolveWikilinkTarget(nodeIndex, pagePart);
          if (resolved) void nav.openPage(resolved);
          else setMissing({ key: requestKey, target: pagePart });
        };
        const targetMeta = nodeIndex.byPath.get(
          resolveWikilinkTarget(nodeIndex, pagePart) ?? "",
        );
        return (
          <a
            href={href}
            {...rest}
            title={
              targetMeta ? `${targetMeta.title} — ${targetMeta.path}` : pagePart
            }
            onClick={handleClick}
          >
            {children}
          </a>
        );
      },
    }),
    [nav, nodeIndex, requestKey],
  );

  const detail = state.status === "ready" ? state.detail : null;
  const frontmatterEntries = detail ? Object.entries(detail.frontmatter) : [];
  const sourceKind =
    detail && typeof detail.frontmatter.source_kind === "string"
      ? detail.frontmatter.source_kind
      : null;
  const tags =
    detail && Array.isArray(detail.frontmatter.tags)
      ? detail.frontmatter.tags.filter(
          (tag): tag is string => typeof tag === "string",
        )
      : [];
  const crumbs = breadcrumbSegments(path);
  const title =
    (detail &&
      typeof detail.frontmatter.title === "string" &&
      detail.frontmatter.title) ||
    detail?.title ||
    crumbs[crumbs.length - 1]?.label ||
    path;

  const citations = useMemo(
    () => (detail ? citationsFromBody(detail.body) : []),
    [detail],
  );
  const trustSources = useMemo(() => {
    if (!graph || !detail?.path) return [];
    const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
    const pageNodeIds = new Set(
      graph.nodes
        .filter((node) => node.path === detail.path)
        .map((node) => node.id),
    );
    return graph.edges
      .filter((edge) => edge.kind === "trust" && pageNodeIds.has(edge.source))
      .map((edge) => nodeById.get(edge.target))
      .filter((node): node is NonNullable<typeof node> => Boolean(node?.title));
  }, [detail, graph]);

  const sourcePath = codePathToSourcePath(path);
  // §4.2 code page reader affordances: mermaid fences render as diagrams and
  // other fences highlighted (MermaidBlock → CodeBlockInner above), the kebab
  // gains "Copy source path" from codePathToSourcePath, and generated code/**
  // pages stay read-only — no edit, delete, or create affordances (the
  // backend rejects such writes anyway).
  const readOnly = path.startsWith("code/");
  const kebabItems: QuickMenuItem[] = [
    { label: "Open in graph", onSelect: onOpenGraph },
    ...(onCreate && !readOnly
      ? [
          {
            label: "New page",
            onSelect: () => onCreate(path.replace(/[^/]*$/, "")),
          },
        ]
      : []),
    {
      label: "Copy path",
      onSelect: () => void navigator.clipboard?.writeText(path),
    },
    ...(sourcePath
      ? [
          {
            label: "Copy source path",
            onSelect: () => void navigator.clipboard?.writeText(sourcePath),
          },
        ]
      : []),
    ...(onDelete && !readOnly
      ? [{ label: "Delete", destructive: true, onSelect: () => onDelete(path) }]
      : []),
  ];

  const segments = crumbs;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-border px-2">
        <Button
          type="button"
          aria-label="Back"
          variant="ghost"
          size="icon"
          className={coarseHitAreaCls}
          disabled={!nav.canBack}
          onClick={() => void nav.back()}
        >
          <Chevron direction="left" />
        </Button>
        <Button
          type="button"
          aria-label="Forward"
          variant="ghost"
          size="icon"
          className={coarseHitAreaCls}
          disabled={!nav.canForward}
          onClick={() => void nav.forward()}
        >
          <Chevron direction="right" />
        </Button>
        <nav
          aria-label="Breadcrumbs"
          className="flex min-w-0 items-center gap-1 px-1"
        >
          {segments.map((segment, index) => {
            const isLeaf = index === segments.length - 1;
            const isMiddle = !isLeaf && index > 0 && segments.length > 3;
            return (
              <span
                key={segment.prefix}
                className="flex min-w-0 items-center gap-1 text-xs"
              >
                {index > 0 ? (
                  <span className="text-muted-foreground">/</span>
                ) : null}
                <span
                  className={`truncate ${
                    isLeaf ? "text-foreground" : "text-muted-foreground"
                  } ${isMiddle ? "max-w-16" : "max-w-40"}`}
                >
                  {segment.label}
                </span>
              </span>
            );
          })}
        </nav>
        <div className="ml-auto flex items-center gap-1">
          {onToggleEdit && !readOnly ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={coarseHitAreaCls}
              title="Edit page"
              onClick={onToggleEdit}
            >
              Edit
            </Button>
          ) : null}
          <QuickMenu
            items={kebabItems}
            menuLabel="Page actions"
            triggerLabel="Page actions"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {state.status === "loading" ? (
          <Card
            role="status"
            aria-label="Loading page"
            className="mx-4 my-6 h-24 max-w-[70ch] animate-pulse bg-muted/30"
          />
        ) : null}

        {state.status === "error" ? (
          <ActivityPanelEmpty heading="Page unavailable" body={state.message} />
        ) : null}

        {detail && detail.status === "not_found" ? (
          <ActivityPanelEmpty
            heading="Page not found"
            body={`“${path}” has not been created yet.`}
            footer={
              onCreate && !readOnly ? (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className={coarseHitAreaCls}
                  onClick={() => onCreate(seedCreatePath(path))}
                >
                  Create this page
                </Button>
              ) : undefined
            }
          />
        ) : null}

        {detail && detail.status === "ambiguous" ? (
          <div className="px-4 py-6">
            <p className="text-sm text-foreground">
              Multiple pages match this reference:
            </p>
            <ul className="mt-2 flex list-none flex-col gap-1">
              {detail.candidates.map((candidate) => (
                <li key={candidate.path}>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className={`${coarseHitAreaCls} justify-start text-left text-sm`}
                    onClick={() => void nav.openPage(candidate.path)}
                  >
                    <span>{candidate.title ?? candidate.path}</span>
                    <span className="ml-2 font-mono text-2xs text-muted-foreground">
                      {candidate.path}
                    </span>
                  </Button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {detail &&
        detail.status !== "not_found" &&
        detail.status !== "ambiguous" ? (
          <div className="max-w-[70ch] px-4 py-4">
            <header className="mb-3 flex flex-col gap-2">
              <h1 className="text-2xl font-semibold text-foreground">
                {title}
              </h1>
              {sourceKind || tags.length > 0 ? (
                <div className="flex flex-wrap items-center gap-1.5">
                  {sourceKind ? (
                    <span className="rounded-md border border-border px-1.5 py-0.5 text-2xs font-medium text-foreground">
                      {sourceKind}
                    </span>
                  ) : null}
                  {tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-md bg-muted px-1.5 py-0.5 text-2xs text-muted-foreground"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              ) : null}
              {frontmatterEntries.length > 0 ? (
                <details className="text-xs text-muted-foreground">
                  <summary className="cursor-pointer select-none">
                    Details
                  </summary>
                  <pre className="mt-1 overflow-x-auto rounded-md bg-muted/50 p-2 font-mono text-2xs">
                    {frontmatterEntries
                      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
                      .join("\n")}
                  </pre>
                </details>
              ) : null}
            </header>

            {missingTarget ? (
              <p
                role="status"
                className="mb-3 flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 px-2 py-1.5 text-xs text-muted-foreground"
              >
                <span>“{missingTarget}” has not been created yet.</span>
                {onCreate ? (
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className={coarseHitAreaCls}
                    onClick={() => onCreate(seedCreatePath(missingTarget))}
                  >
                    Create this page
                  </Button>
                ) : null}
              </p>
            ) : null}

            <div
              className={`message-content text-sm text-foreground ${markdownBodyClassName}`}
            >
              <MarkdownBody
                content={stripLeadingTitleHeading(detail.body, title)}
                id={`wiki-${path}`}
                remarkPlugins={remarkPlugins}
                components={components}
              />
            </div>

            {citations.length > 0 || trustSources.length > 0 ? (
              <section
                aria-label="Sources"
                className="mt-6 border-t border-border pt-3"
              >
                <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                  Sources
                </h2>
                <ul className="mt-2 flex list-none flex-wrap gap-1.5">
                  {citations.map((citation) => (
                    <li key={citation.target}>
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        className={coarseHitAreaCls}
                        onClick={() => {
                          const resolved = resolveWikilinkTarget(
                            nodeIndex,
                            citation.target,
                          );
                          if (resolved) void nav.openPage(resolved);
                          else
                            setMissing({
                              key: requestKey,
                              target: citation.target,
                            });
                        }}
                      >
                        {citation.label}
                      </Button>
                    </li>
                  ))}
                  {trustSources
                    .filter(
                      (node) => !citations.some((c) => c.label === node.title),
                    )
                    .map((node) => (
                      <li key={node.id}>
                        <span className="rounded-md border border-border px-1.5 py-0.5 text-xs text-muted-foreground">
                          {node.title}
                        </span>
                      </li>
                    ))}
                </ul>
              </section>
            ) : null}

            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}
