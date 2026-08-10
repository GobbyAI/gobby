/**
 * Quick-open overlay (plan wiki-obsidian-panel §3.1): panel-scoped Cmd+K
 * fuzzy jump over the node index with a server search fallback for terms
 * the local index misses.
 */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { Card } from "../../ui/Card";
import { Input } from "../../ui/Input";
import { fetchSearch, type WikiFetchScope } from "./WikiTabData";
import type { WikiNodeIndex, WikiPageMeta } from "./WikiTabModel";

const MAX_RESULTS = 50;
const SERVER_FALLBACK_THRESHOLD = 5;
const MIN_SERVER_QUERY = 2;

interface QuickOpenMatch {
  path: string;
  title: string;
  /** Lower ranks first. */
  rank: number;
}

/** Title-prefix beats title-substring beats path-substring. */
function scoreMatch(page: WikiPageMeta, query: string): number | null {
  const title = page.title.toLowerCase();
  const path = page.path.toLowerCase();
  if (title.startsWith(query)) return 0;
  if (title.includes(query)) return 1;
  if (path.includes(query)) return 2;
  return null;
}

interface WikiQuickOpenProps {
  scope: WikiFetchScope;
  pages: WikiPageMeta[];
  nodeIndex: WikiNodeIndex;
  onOpen: (path: string) => void;
  onClose: () => void;
}

export function WikiQuickOpen({ scope, pages, nodeIndex, onOpen, onClose }: WikiQuickOpenProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [serverMatches, setServerMatches] = useState<{
    key: string;
    matches: QuickOpenMatch[];
  } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const trimmed = query.trim().toLowerCase();

  const localMatches = useMemo<QuickOpenMatch[]>(() => {
    if (!trimmed) return [];
    const matches: QuickOpenMatch[] = [];
    for (const page of pages) {
      const rank = scoreMatch(page, trimmed);
      if (rank !== null) matches.push({ path: page.path, title: page.title, rank });
    }
    matches.sort((a, b) => a.rank - b.rank || a.title.localeCompare(b.title));
    return matches.slice(0, MAX_RESULTS);
  }, [pages, trimmed]);

  // Server fallback only when the local index comes up short — the node
  // index covers titles/paths; the server search also matches content.
  const wantServer = trimmed.length >= MIN_SERVER_QUERY && localMatches.length < SERVER_FALLBACK_THRESHOLD;

  useEffect(() => {
    if (!wantServer) return;
    let cancelled = false;
    fetchSearch(scope, trimmed, MAX_RESULTS)
      .then((result) => {
        if (cancelled) return;
        const matches: QuickOpenMatch[] = [];
        for (const hit of result.hits) {
          const path = hit.wikiPage ?? hit.sourcePath;
          if (!path || !nodeIndex.byPath.has(path)) continue;
          matches.push({
            path,
            title: hit.title ?? nodeIndex.byPath.get(path)?.title ?? path,
            rank: 3,
          });
        }
        setServerMatches({ key: trimmed, matches });
      })
      .catch(() => {
        if (!cancelled) setServerMatches({ key: trimmed, matches: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [nodeIndex, scope, trimmed, wantServer]);

  const matches = useMemo<QuickOpenMatch[]>(() => {
    const seen = new Set(localMatches.map((match) => match.path));
    const merged = [...localMatches];
    if (wantServer && serverMatches?.key === trimmed) {
      for (const match of serverMatches.matches) {
        if (!seen.has(match.path)) {
          seen.add(match.path);
          merged.push(match);
        }
      }
    }
    return merged.slice(0, MAX_RESULTS);
  }, [localMatches, serverMatches, trimmed, wantServer]);

  const clampedIndex = Math.min(activeIndex, Math.max(matches.length - 1, 0));

  const open = (path: string) => {
    onOpen(path);
    onClose();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => Math.min(index + 1, matches.length - 1));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => Math.max(index - 1, 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const match = matches[clampedIndex];
      if (match) open(match.path);
    }
  };

  return (
    <div
      className="absolute inset-0 z-20 flex items-start justify-center bg-background/60 pt-10"
      role="presentation"
      onMouseDown={(event) => {
        if (event.button === 0) onClose()
      }}
    >
      <Card
        role="dialog"
        aria-label="Quick open"
        className="w-full max-w-md shadow-lg"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <Input
          ref={inputRef}
          role="combobox"
          aria-expanded={matches.length > 0}
          aria-controls="wiki-quick-open-results"
          aria-activedescendant={
            matches[clampedIndex] ? `wiki-quick-open-option-${clampedIndex}` : undefined
          }
          aria-label="Quick open"
          value={query}
          placeholder="Jump to a page…"
          className="h-auto rounded-t-lg rounded-b-none border-x-0 border-t-0 px-3 py-2 text-sm"
          onChange={(event) => {
            setQuery(event.target.value);
            setActiveIndex(0);
          }}
          onKeyDown={handleKeyDown}
        />
        <ul
          id="wiki-quick-open-results"
          role="listbox"
          aria-label="Matching pages"
          className="flex max-h-72 list-none flex-col gap-1 overflow-y-auto p-1"
        >
          {trimmed && matches.length === 0 ? (
            <li className="px-2 py-1.5 text-xs text-muted-foreground">No matching pages</li>
          ) : null}
          {matches.map((match, index) => (
            <Card key={match.path} asChild>
              <li
                id={`wiki-quick-open-option-${index}`}
                role="option"
                aria-selected={index === clampedIndex}
                className="flex cursor-pointer items-baseline gap-2 bg-background px-2 py-1.5 text-sm text-foreground aria-selected:bg-muted hover:bg-muted"
                onMouseEnter={() => setActiveIndex(index)}
                onMouseDown={(event) => {
                  if (event.button === 0) open(match.path)
                }}
              >
                <span className="truncate">{match.title}</span>
                <span className="ml-auto truncate pl-2 font-mono text-2xs text-muted-foreground">
                  {match.path}
                </span>
              </li>
            </Card>
          ))}
        </ul>
      </Card>
    </div>
  );
}
