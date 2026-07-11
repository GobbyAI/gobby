/**
 * Shell state for the wiki activity tab (plan wiki-obsidian-panel §2.2):
 * persistence keys/helpers and the dirty-guarded navigation history hook.
 * Pure state — rendering lives in WikiTab.tsx, fetching in WikiTabData.ts.
 */

import { useCallback, useRef, useState } from "react";

import type { WikiGraphInclude, WikiMode } from "./WikiTabModel";

export const WIKI_TAB_KEYS = {
  mode: "gobby:wiki-tab:mode",
  topic: "gobby:wiki-tab:topic",
  treeWidth: "gobby:wiki-tab:tree-width",
  split: "gobby:wiki-tab:split",
  lastPageWiki: "gobby:wiki-tab:last-page:wiki",
  lastPageCode: "gobby:wiki-tab:last-page:code",
  graph: "gobby:wiki-tab:graph",
  /** sessionStorage, unlike the rest. */
  askHistory: "gobby:wiki-tab:ask-history",
} as const;

export const WIKI_MODES: readonly WikiMode[] = ["wiki", "code", "ask", "research"];

export function isWikiMode(value: unknown): value is WikiMode {
  return typeof value === "string" && (WIKI_MODES as readonly string[]).includes(value);
}

function defaultStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readStoredValue(key: string, storage?: Storage): string | null {
  const target = storage ?? defaultStorage();
  if (!target) return null;
  try {
    return target.getItem(key);
  } catch {
    return null;
  }
}

export function writeStoredValue(key: string, value: string | null, storage?: Storage): void {
  const target = storage ?? defaultStorage();
  if (!target) return;
  try {
    if (value === null) target.removeItem(key);
    else target.setItem(key, value);
  } catch {
    // Persistence is best-effort (private mode, quota).
  }
}

export function loadStoredMode(): WikiMode {
  const value = readStoredValue(WIKI_TAB_KEYS.mode);
  return isWikiMode(value) ? value : "wiki";
}

export function storeMode(mode: WikiMode): void {
  writeStoredValue(WIKI_TAB_KEYS.mode, mode);
}

export function loadStoredTopic(): string | null {
  const value = readStoredValue(WIKI_TAB_KEYS.topic);
  return value && value.trim() ? value : null;
}

export function storeTopic(topic: string | null): void {
  writeStoredValue(WIKI_TAB_KEYS.topic, topic && topic.trim() ? topic : null);
}

export type WikiBrowseMode = Extract<WikiMode, "wiki" | "code">;

function lastPageKey(mode: WikiBrowseMode): string {
  return mode === "code" ? WIKI_TAB_KEYS.lastPageCode : WIKI_TAB_KEYS.lastPageWiki;
}

export function loadLastPage(mode: WikiBrowseMode): string | null {
  return readStoredValue(lastPageKey(mode));
}

export function storeLastPage(mode: WikiBrowseMode, path: string): void {
  writeStoredValue(lastPageKey(mode), path);
}

/** Pages under code/ belong to code mode; everything else browses in wiki mode. */
export function modeForPath(path: string): WikiMode {
  return path.startsWith("code/") ? "code" : "wiki";
}

/** §4.1 graph view settings, persisted as one JSON blob. */
export interface WikiGraphSettings {
  include: WikiGraphInclude;
  sources: boolean;
  unresolved: boolean;
  orphans: boolean;
  trust: boolean;
  audit: boolean;
  codeEdges: boolean;
  communities: boolean;
}

export const DEFAULT_WIKI_GRAPH_SETTINGS: WikiGraphSettings = {
  include: "all",
  sources: false,
  unresolved: false,
  orphans: true,
  trust: true,
  audit: false,
  codeEdges: true,
  communities: false,
};

function boolOr(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function loadGraphSettings(): WikiGraphSettings {
  const raw = readStoredValue(WIKI_TAB_KEYS.graph);
  if (!raw) return { ...DEFAULT_WIKI_GRAPH_SETTINGS };
  try {
    const parsed = JSON.parse(raw) as Partial<WikiGraphSettings>;
    const defaults = DEFAULT_WIKI_GRAPH_SETTINGS;
    return {
      include:
        parsed.include === "knowledge" || parsed.include === "code" ? parsed.include : "all",
      sources: boolOr(parsed.sources, defaults.sources),
      unresolved: boolOr(parsed.unresolved, defaults.unresolved),
      orphans: boolOr(parsed.orphans, defaults.orphans),
      trust: boolOr(parsed.trust, defaults.trust),
      audit: boolOr(parsed.audit, defaults.audit),
      codeEdges: boolOr(parsed.codeEdges, defaults.codeEdges),
      communities: boolOr(parsed.communities, defaults.communities),
    };
  } catch {
    return { ...DEFAULT_WIKI_GRAPH_SETTINGS };
  }
}

export function storeGraphSettings(settings: WikiGraphSettings): void {
  writeStoredValue(WIKI_TAB_KEYS.graph, JSON.stringify(settings));
}

export const WIKI_NAV_HISTORY_CAP = 50;

export interface WikiNavEntry {
  path: string;
  mode: WikiMode;
}

export interface WikiNavOptions {
  /**
   * Dirty-guard runner every transition goes through — pass the ambient
   * useDirtyGuard().guardedRun so an editing child can veto navigation.
   */
  guardedRun: (action: () => void | Promise<void>) => Promise<void>;
  /** Fires after a transition commits (open, back, or forward). */
  onNavigate?: (entry: WikiNavEntry) => void;
}

export interface WikiNav {
  current: WikiNavEntry | null;
  canBack: boolean;
  canForward: boolean;
  openPage: (path: string, opts?: { mode?: WikiMode }) => Promise<void>;
  back: () => Promise<void>;
  forward: () => Promise<void>;
}

interface WikiNavState {
  entries: WikiNavEntry[];
  cursor: number;
}

const INITIAL_NAV_STATE: WikiNavState = { entries: [], cursor: -1 };

export function useWikiNav({ guardedRun, onNavigate }: WikiNavOptions): WikiNav {
  const [state, setState] = useState<WikiNavState>(INITIAL_NAV_STATE);
  // commit() is the only mutation path and updates the ref alongside setState,
  // so the ref stays current without a render-phase write.
  const stateRef = useRef<WikiNavState>(INITIAL_NAV_STATE);

  const commit = useCallback(
    (next: WikiNavState, entry: WikiNavEntry) => {
      setState(next);
      stateRef.current = next;
      onNavigate?.(entry);
    },
    [onNavigate],
  );

  const openPage = useCallback(
    async (path: string, opts?: { mode?: WikiMode }) => {
      const entry: WikiNavEntry = { path, mode: opts?.mode ?? modeForPath(path) };
      const { entries, cursor } = stateRef.current;
      const current = cursor >= 0 ? entries[cursor] : null;
      if (current && current.path === entry.path && current.mode === entry.mode) return;
      await guardedRun(() => {
        const { entries: latest, cursor: latestCursor } = stateRef.current;
        const next = [...latest.slice(0, latestCursor + 1), entry].slice(-WIKI_NAV_HISTORY_CAP);
        commit({ entries: next, cursor: next.length - 1 }, entry);
      });
    },
    [commit, guardedRun],
  );

  const back = useCallback(async () => {
    if (stateRef.current.cursor <= 0) return;
    await guardedRun(() => {
      const { entries, cursor } = stateRef.current;
      const target = entries[cursor - 1];
      if (!target) return;
      commit({ entries, cursor: cursor - 1 }, target);
    });
  }, [commit, guardedRun]);

  const forward = useCallback(async () => {
    const { entries, cursor } = stateRef.current;
    if (cursor >= entries.length - 1) return;
    await guardedRun(() => {
      const { entries: latest, cursor: latestCursor } = stateRef.current;
      const target = latest[latestCursor + 1];
      if (!target) return;
      commit({ entries: latest, cursor: latestCursor + 1 }, target);
    });
  }, [commit, guardedRun]);

  return {
    current: state.cursor >= 0 ? (state.entries[state.cursor] ?? null) : null,
    canBack: state.cursor > 0,
    canForward: state.cursor >= 0 && state.cursor < state.entries.length - 1,
    openPage,
    back,
    forward,
  };
}
