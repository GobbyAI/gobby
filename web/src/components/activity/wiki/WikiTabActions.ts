/**
 * Orchestration layer for wiki tab actions (plan wiki-obsidian-panel §2.2):
 * write flows over the §2.1 fetchers plus the kept useWiki action helpers
 * (refresh/attach/ingest/compileWiki/audit), with one busy/status/error
 * surface the shell renders as an aria-live line.
 */

import { useCallback, useMemo, useState } from "react";

import {
  createPage,
  deletePage,
  launchResearch,
  savePage,
  type WikiFetchScope,
  type WikiResearchLaunch,
  type WikiSaveRequest,
  type WikiSaveResult,
} from "./WikiTabData";

/** The kept useWiki action helpers this layer builds on. */
export interface WikiActionHelpers {
  refresh: () => Promise<void>;
  attach: (file: File) => Promise<unknown>;
  ingest: (request: { urls?: string[]; paths?: string[]; path?: string }) => Promise<unknown>;
  compileWiki: () => Promise<unknown>;
  audit: () => Promise<unknown>;
}

export interface WikiActionStatus {
  /** Action key currently running, null when idle. */
  busy: string | null;
  /** Last success line for the aria-live status surface. */
  message: string | null;
  /** Last failure line; cleared when the next action starts. */
  error: string | null;
}

export interface UseWikiTabActionsOptions {
  scope: WikiFetchScope;
  wiki: WikiActionHelpers;
  /** Refetch hook fired after successful writes (page/pages/backlinks). */
  onRefetch?: () => Promise<void> | void;
  /** Navigation hook fired after create (open the new page). */
  onNavigate?: (path: string) => Promise<void> | void;
  /** Navigation hook fired after delete (leave the deleted page). */
  onNavigateBack?: () => Promise<void> | void;
}

export interface WikiTabActions {
  status: WikiActionStatus;
  clearStatus: () => void;
  savePageAndRefresh: (request: WikiSaveRequest) => Promise<WikiSaveResult | null>;
  createPageAndNavigate: (path: string, content: string) => Promise<boolean>;
  deletePageAndNavigateBack: (path: string) => Promise<boolean>;
  refreshIndex: () => Promise<void>;
  runCompile: () => Promise<void>;
  runAudit: () => Promise<void>;
  attachFile: (file: File) => Promise<void>;
  ingestUrl: (url: string) => Promise<void>;
  launchResearchRun: (inputs: Record<string, unknown>) => Promise<WikiResearchLaunch | null>;
}

const IDLE_STATUS: WikiActionStatus = { busy: null, message: null, error: null };

export function useWikiTabActions({
  scope,
  wiki,
  onRefetch,
  onNavigate,
  onNavigateBack,
}: UseWikiTabActionsOptions): WikiTabActions {
  const [status, setStatus] = useState<WikiActionStatus>(IDLE_STATUS);

  const run = useCallback(
    async <T>(key: string, action: () => Promise<T>, successMessage?: string): Promise<T | null> => {
      setStatus({ busy: key, message: null, error: null });
      try {
        const result = await action();
        setStatus({ busy: null, message: successMessage ?? null, error: null });
        return result;
      } catch (error) {
        setStatus({
          busy: null,
          message: null,
          error: error instanceof Error ? error.message : String(error),
        });
        return null;
      }
    },
    [],
  );

  const clearStatus = useCallback(() => setStatus(IDLE_STATUS), []);

  const savePageAndRefresh = useCallback(
    async (request: WikiSaveRequest): Promise<WikiSaveResult | null> => {
      setStatus({ busy: "save", message: null, error: null });
      try {
        const result = await savePage(scope, request);
        if (result.ok) {
          await onRefetch?.();
          setStatus({ busy: null, message: `Saved ${request.path}`, error: null });
        } else {
          setStatus({ busy: null, message: null, error: result.message });
        }
        return result;
      } catch (error) {
        setStatus({
          busy: null,
          message: null,
          error: error instanceof Error ? error.message : String(error),
        });
        return null;
      }
    },
    [onRefetch, scope],
  );

  const createPageAndNavigate = useCallback(
    async (path: string, content: string): Promise<boolean> => {
      const result = await run(
        "create",
        async () => {
          const created = await createPage(scope, path, content);
          if (!created.ok) throw new Error(created.message);
          await onRefetch?.();
          await onNavigate?.(created.path ?? path);
          return created;
        },
        `Created ${path}`,
      );
      return result !== null;
    },
    [onNavigate, onRefetch, run, scope],
  );

  const deletePageAndNavigateBack = useCallback(
    async (path: string): Promise<boolean> => {
      const result = await run(
        "delete",
        async () => {
          const deleted = await deletePage(scope, path);
          await onRefetch?.();
          await onNavigateBack?.();
          return deleted;
        },
        `Deleted ${path}`,
      );
      return result !== null;
    },
    [onNavigateBack, onRefetch, run, scope],
  );

  const refreshIndex = useCallback(async () => {
    await run("refresh", () => wiki.refresh(), "Wiki index refreshed");
  }, [run, wiki]);

  const runCompile = useCallback(async () => {
    await run("compile", () => wiki.compileWiki(), "Compile started");
  }, [run, wiki]);

  const runAudit = useCallback(async () => {
    await run("audit", () => wiki.audit(), "Audit started");
  }, [run, wiki]);

  const attachFile = useCallback(
    async (file: File) => {
      await run(
        "attach",
        async () => {
          await wiki.attach(file);
          await wiki.refresh();
        },
        `Attached ${file.name}`,
      );
    },
    [run, wiki],
  );

  const ingestUrl = useCallback(
    async (url: string) => {
      await run(
        "ingest",
        async () => {
          await wiki.ingest({ urls: [url] });
          await wiki.refresh();
        },
        `Ingest started for ${url}`,
      );
    },
    [run, wiki],
  );

  const launchResearchRun = useCallback(
    async (inputs: Record<string, unknown>): Promise<WikiResearchLaunch | null> =>
      run("research", () => launchResearch(scope, inputs), "Research run launched"),
    [run, scope],
  );

  return useMemo(
    () => ({
      status,
      clearStatus,
      savePageAndRefresh,
      createPageAndNavigate,
      deletePageAndNavigateBack,
      refreshIndex,
      runCompile,
      runAudit,
      attachFile,
      ingestUrl,
      launchResearchRun,
    }),
    [
      attachFile,
      clearStatus,
      createPageAndNavigate,
      deletePageAndNavigateBack,
      ingestUrl,
      launchResearchRun,
      refreshIndex,
      runAudit,
      runCompile,
      savePageAndRefresh,
      status,
    ],
  );
}
