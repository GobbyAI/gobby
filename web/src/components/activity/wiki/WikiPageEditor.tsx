/**
 * §3.2 page editor: the edit toggle and the recipe-compliant create form over
 * `CodeMirrorEditor`, with draft state in `useDetailDraft` (shell dirty-guard
 * registration, Cmd+S) and the revision contract — the editor holds the base
 * `content_hash`, revalidates it on window focus / manual refresh / right
 * before save, and a mismatch or 412 opens the reload/overwrite conflict
 * panel. Silent last-write-wins is never allowed.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useConfirmDialog } from "../../../hooks/useConfirmDialog";
import { CodeMirrorEditor } from "../../shared/CodeMirrorEditor";
import { Card } from "../../ui/Card";
import { Input } from "../../ui/Input";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import { DetailActionButton, DetailPaneHeader, useDetailDraft } from "../fields";
import { fetchPage, type WikiFetchScope } from "./WikiTabData";
import { validateCreatePath } from "./WikiTabModel";
import type { WikiTabActions } from "./WikiTabActions";

export type WikiEditorIntent =
  | { kind: "edit"; path: string }
  | { kind: "create"; seed: string };

interface WikiDraft {
  path: string;
  content: string;
}

const CREATE_TEMPLATE = '---\ntitle: ""\ntags: []\n---\n\n';

interface WikiPageEditorProps {
  scope: WikiFetchScope;
  intent: WikiEditorIntent;
  actions: WikiTabActions;
  /** Exit back to the reader without saving. */
  onClose: () => void;
  /**
   * Fired after a successful save/create with the final path — once the draft
   * is already marked clean, so host navigation passes the dirty guard.
   */
  onSaved: (path: string) => void;
}

export function WikiPageEditor({ scope, intent, actions, onClose, onSaved }: WikiPageEditorProps) {
  // The host keys this component by intent, so `intent` is immutable for a
  // mounted editor and plain (unkeyed) state is safe across its lifetime.
  const [base, setBase] = useState<{ content: string; hash: string | null } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{ message: string | null } | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  // Synchronous mirror of the base hash: revalidate() and save() compare
  // against this ref so back-to-back async steps never race a re-render.
  const baseHashRef = useRef<string | null>(null);
  // Final path of the last successful save, consumed by runSave → onSaved.
  const savedPathRef = useRef<string | null>(null);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();

  const adoptBase = useCallback((content: string, hash: string | null) => {
    baseHashRef.current = hash;
    setBase({ content, hash });
  }, []);

  useEffect(() => {
    if (intent.kind !== "edit") return;
    let cancelled = false;
    fetchPage(scope, { path: intent.path })
      .then((detail) => {
        if (cancelled) return;
        if (detail.status && detail.status !== "found") {
          setLoadError(`“${intent.path}” could not be loaded for editing.`);
          return;
        }
        adoptBase(detail.content, detail.contentHash);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : "Failed to load page");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [adoptBase, intent, scope]);

  /**
   * Re-read the page and compare hashes. A change adopts the fresh content as
   * the new base — `useDetailDraft` keeps a dirty draft and raises
   * `serverChanged`, or refreshes a clean editor in place.
   */
  const revalidate = useCallback(async (): Promise<{
    hash: string | null;
    changed: boolean;
  } | null> => {
    if (intent.kind !== "edit") return null;
    try {
      const detail = await fetchPage(scope, { path: intent.path });
      if (detail.status && detail.status !== "found") return null;
      const changed = detail.contentHash !== baseHashRef.current;
      if (changed) adoptBase(detail.content, detail.contentHash);
      return { hash: detail.contentHash, changed };
    } catch {
      return null;
    }
  }, [adoptBase, intent, scope]);

  const source = useMemo<WikiDraft | null>(() => {
    if (intent.kind === "create") return { path: intent.seed, content: CREATE_TEMPLATE };
    return base ? { path: intent.path, content: base.content } : null;
  }, [base, intent]);

  const handleSave = useCallback(
    async (merged: WikiDraft): Promise<boolean> => {
      if (intent.kind === "create") {
        const validation = validateCreatePath(merged.path);
        if (validation) {
          setFormError(validation);
          return false;
        }
        const result = await actions.savePageAndRefresh({
          path: merged.path,
          content: merged.content,
          mode: "create",
        });
        if (!result) return false;
        if (!result.ok) {
          setFormError(result.message);
          return false;
        }
        savedPathRef.current = result.path ?? merged.path;
        return true;
      }
      if (base && merged.content === base.content) {
        // Nothing diverged from the base — treat Cmd+S as a no-op save.
        savedPathRef.current = intent.path;
        return true;
      }
      // Revalidate immediately before writing: a changed hash means another
      // writer landed, so open the conflict panel instead of overwriting.
      const check = await revalidate();
      if (check?.changed) {
        setConflict({ message: null });
        return false;
      }
      const result = await actions.savePageAndRefresh({
        path: intent.path,
        content: merged.content,
        mode: "upsert",
        expectedHash: check ? check.hash : baseHashRef.current,
      });
      if (!result) return false;
      if (!result.ok) {
        setConflict({ message: result.message });
        return false;
      }
      savedPathRef.current = intent.path;
      return true;
    },
    [actions, base, intent, revalidate],
  );

  const { draft, setField, dirty, saving, serverChanged, save, discard, confirmIfDirty } =
    useDetailDraft<WikiDraft>({ source, onSave: handleSave });

  const runSave = useCallback(async () => {
    const saved = await save();
    if (saved && savedPathRef.current) onSaved(savedPathRef.current);
  }, [onSaved, save]);

  useEffect(() => {
    if (intent.kind !== "edit") return;
    const handler = () => {
      void revalidate();
    };
    window.addEventListener("focus", handler);
    return () => window.removeEventListener("focus", handler);
  }, [intent.kind, revalidate]);

  const handleReload = useCallback(async () => {
    // Drop the local draft first; the fresh base then flows into the clean
    // editor through useDetailDraft's source reset.
    discard();
    setConflict(null);
    await revalidate();
  }, [discard, revalidate]);

  const handleOverwrite = useCallback(async () => {
    if (intent.kind !== "edit") return;
    const confirmed = await confirm({
      title: "Overwrite page",
      description: `Replace “${intent.path}” on disk with your draft? Changes from the other writer are lost.`,
      confirmLabel: "Overwrite",
      destructive: true,
    });
    if (!confirmed) return;
    // Adopt the freshest hash, then re-enter the save flow against it.
    await revalidate();
    setConflict(null);
    await runSave();
  }, [confirm, intent, revalidate, runSave]);

  const title = intent.kind === "create" ? "New page" : intent.path;
  const headerTitle = (
    <span className="flex min-w-0 items-center gap-2">
      <span className="truncate">{title}</span>
      {dirty ? (
        <span className="flex shrink-0 items-center gap-1 text-xs font-normal text-[var(--color-warning-foreground)]">
          <span aria-hidden="true">●</span>
          Unsaved
        </span>
      ) : null}
    </span>
  );

  const headerActions =
    intent.kind === "create" ? (
      <>
        <DetailActionButton label="Cancel" variant="ghost" onClick={() => confirmIfDirty(onClose)} />
        <DetailActionButton label="Create" variant="accent" disabled={saving} onClick={() => void runSave()} />
      </>
    ) : (
      <>
        <DetailActionButton label="Refresh" variant="ghost" disabled={saving} onClick={() => void revalidate()} />
        <DetailActionButton label="Close" variant="ghost" onClick={() => confirmIfDirty(onClose)} />
      </>
    );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailPaneHeader
        title={headerTitle}
        dirty={intent.kind === "edit" && dirty}
        saving={saving}
        serverChanged={serverChanged}
        onSave={() => void runSave()}
        onDiscard={discard}
        actions={headerActions}
      />

      {intent.kind === "create" ? (
        <div className="flex flex-col gap-1 border-b border-border px-3 py-2">
          <div className="flex items-center gap-2">
            <label htmlFor="wiki-create-path" className="shrink-0 text-xs text-muted-foreground">
              Page path
            </label>
            <Input
              id="wiki-create-path"
              value={draft?.path ?? ""}
              onChange={(event) => {
                setFormError(null);
                setField("path", event.target.value);
              }}
              spellCheck={false}
              placeholder="knowledge/concepts/example.md"
              wrapperClassName="min-w-0 flex-1"
              className="h-7 px-2 font-mono text-xs"
            />
          </div>
          {formError ? (
            <p role="alert" className="text-xs text-destructive-foreground">
              {formError}
            </p>
          ) : null}
        </div>
      ) : null}

      {conflict ? (
        <div role="alert" className="border-b border-border bg-[var(--color-warning-soft)] px-3 py-2">
          <p className="text-xs font-medium text-[var(--color-warning-foreground)]">
            Page changed on disk
          </p>
          <p className="mt-0.5 max-w-[65ch] text-xs text-muted-foreground">
            Another writer updated this page since your editing baseline.
            {conflict.message ? ` ${conflict.message}.` : ""}
          </p>
          <div className="mt-2 flex items-center gap-2">
            <DetailActionButton label="Reload" variant="secondary" onClick={() => void handleReload()} />
            <DetailActionButton label="Overwrite" variant="destructive" onClick={() => void handleOverwrite()} />
            <DetailActionButton label="Keep editing" variant="ghost" onClick={() => setConflict(null)} />
          </div>
        </div>
      ) : null}

      <div className="min-h-0 flex-1">
        {loadError ? (
          <ActivityPanelEmpty heading="Editor unavailable" body={loadError} />
        ) : draft ? (
          <CodeMirrorEditor
            content={draft.content}
            language="markdown"
            onChange={(value) => setField("content", value)}
            onSave={() => void runSave()}
            ariaLabel="Page editor"
          />
        ) : (
          <Card
            role="status"
            aria-label="Loading editor"
            className="mx-4 my-6 h-24 animate-pulse bg-muted/30"
          />
        )}
      </div>

      {ConfirmDialogElement}
    </div>
  );
}
