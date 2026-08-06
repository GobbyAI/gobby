/**
 * Orchestration layer for wiki tab actions (plan wiki-obsidian-panel §2.2):
 * write flows over the §2.1 fetchers plus the kept useWiki action helpers
 * (refresh/attach/ingest/compileWiki/audit), with one busy/status/error
 * surface the shell renders as an aria-live line.
 */

import { useCallback, useMemo, useState } from "react";

import {
  deletePage,
  requestCodewikiRefresh,
  savePage,
  type WikiFetchScope,
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
  /** Navigation hook fired after delete (leave the deleted page). */
  onNavigateBack?: () => Promise<void> | void;
}

export interface WikiTabActions {
  status: WikiActionStatus;
  clearStatus: () => void;
  savePageAndRefresh: (request: WikiSaveRequest) => Promise<WikiSaveResult | null>;
  deletePageAndNavigateBack: (path: string) => Promise<boolean>;
  refreshIndex: () => Promise<void>;
  refreshCodewiki: () => Promise<void>;
  runCompile: () => Promise<void>;
  runAudit: () => Promise<void>;
  attachFile: (file: File) => Promise<void>;
  ingestUrl: (url: string) => Promise<void>;
}

const IDLE_STATUS: WikiActionStatus = { busy: null, message: null, error: null };

export function useWikiTabActions({
  scope,
  wiki,
  onRefetch,
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
          setStatus({
            busy: null,
            message: `${result.created ? "Created" : "Saved"} ${request.path}`,
            error: null,
          });
        } else {
          // Conflicts are caller-owned UX (§3.2 conflict panel / inline 409);
          // a parallel global error line would double-message them.
          setStatus(IDLE_STATUS);
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

  const refreshCodewiki = useCallback(async () => {
    await run(
      "codewiki",
      async () => {
        const outcome = await requestCodewikiRefresh(scope);
        // A not-accepted refresh means nothing will happen — surface the
        // server reason on the error line instead of a false success.
        if (!outcome.accepted) {
          throw new Error(outcome.reason ?? "Codewiki refresh was not scheduled");
        }
      },
      "Codewiki refresh scheduled",
    );
  }, [run, scope]);

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

  return useMemo(
    () => ({
      status,
      clearStatus,
      savePageAndRefresh,
      deletePageAndNavigateBack,
      refreshIndex,
      refreshCodewiki,
      runCompile,
      runAudit,
      attachFile,
      ingestUrl,
    }),
    [
      attachFile,
      clearStatus,
      deletePageAndNavigateBack,
      ingestUrl,
      refreshCodewiki,
      refreshIndex,
      runAudit,
      runCompile,
      savePageAndRefresh,
      status,
    ],
  );
}
