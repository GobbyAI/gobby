import {
  memo,
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  type CSSProperties,
} from "react";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { useDialogFocus } from "../../hooks/useDialogFocus";
import { useIsMobile } from "../../hooks/useIsMobile";
import { ResizeHandle } from "../shared/ResizeHandle";
import { CodeBlock } from "../shared/CodeBlock";
import { MarkdownBody, markdownBodyClassName } from "../shared/MarkdownBody";
import { CodeMirrorEditor } from "../shared/CodeMirrorEditor";
import { EditableViewActions } from "../shared/EditableView";
import {
  detectLanguageFromPath,
  useEditableContent,
} from "../shared/editableContent";
import {
  NO_CHECKOUT_MESSAGE,
  isNoCheckoutErrorText,
  responseReportsNoCheckout,
} from "../../lib/projectCheckout";
import { cn } from "../../lib/utils";
import { ActivityPanelEmpty, FilesEmptyIcon } from "./ActivityPanelEmpty";
import {
  FilesTabContextMenu,
  FilesTabMoveDialog,
} from "./FilesTabActionSurfaces";
import type {
  ContextMenuState,
  FileEntry,
  FilesTabProps,
  RenamingState,
} from "./FilesTab.types";
import { FilesTabTree } from "./FilesTabTree";

const FILES_TAB_LEFT_WIDTH_KEY = "gobby:files-tab:left-width";
const FILES_TAB_LEFT_WIDTH_DEFAULT = 320;
const FILES_TAB_LEFT_WIDTH_MIN = 200;
const FILES_TAB_LEFT_WIDTH_MAX = 600;

function readPersistedLeftWidth(): number {
  if (typeof window === "undefined") return FILES_TAB_LEFT_WIDTH_DEFAULT;
  try {
    const stored = window.localStorage.getItem(FILES_TAB_LEFT_WIDTH_KEY);
    if (!stored) return FILES_TAB_LEFT_WIDTH_DEFAULT;
    const parsed = Number.parseInt(stored, 10);
    if (Number.isNaN(parsed)) return FILES_TAB_LEFT_WIDTH_DEFAULT;
    return Math.max(
      FILES_TAB_LEFT_WIDTH_MIN,
      Math.min(FILES_TAB_LEFT_WIDTH_MAX, parsed),
    );
  } catch {
    return FILES_TAB_LEFT_WIDTH_DEFAULT;
  }
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

const FILE_LOAD_FAILED = "Failed to load file";

/** A read failure whose message is safe to show in the viewer. */
class FileLoadError extends Error {}

const FilesTabProject = memo(function FilesTabProject({
  projectId,
  onAddToChat,
  layout = "stack",
}: FilesTabProps) {
  const isMobile = useIsMobile();
  const useHorizontal = layout === "responsive-split" && !isMobile;
  const [rootEntries, setRootEntries] = useState<FileEntry[]>([]);
  // Why the tree is empty when the API refused it (no checkout here).
  const [treeNotice, setTreeNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [childrenMap, setChildrenMap] = useState<Map<string, FileEntry[]>>(
    new Map(),
  );
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(40);
  const [leftWidth, setLeftWidth] = useState<number>(() =>
    layout === "responsive-split"
      ? readPersistedLeftWidth()
      : FILES_TAB_LEFT_WIDTH_DEFAULT,
  );

  useEffect(() => {
    if (layout !== "responsive-split") return;
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(FILES_TAB_LEFT_WIDTH_KEY, String(leftWidth));
    } catch {
      // Ignore storage write failures (quota, private mode, etc.)
    }
  }, [layout, leftWidth]);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [ctxMenu, setCtxMenu] = useState<ContextMenuState | null>(null);
  const ctxMenuRef = useRef<HTMLDivElement>(null);
  const [renaming, setRenaming] = useState<RenamingState | null>(null);
  const [moving, setMoving] = useState<FileEntry | null>(null);
  const [movePath, setMovePath] = useState("");
  const [moveError, setMoveError] = useState<string | null>(null);
  const [gitStatus, setGitStatus] = useState<Record<string, string>>({});
  const renameInputRef = useRef<HTMLInputElement>(null);
  const moveDialogRef = useRef<HTMLFormElement>(null);
  const childRequestControllers = useRef(new Set<AbortController>());
  const openFileController = useRef<AbortController | null>(null);
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  useDialogFocus({
    ref: moveDialogRef,
    isOpen: moving !== null,
    onClose: () => setMoving(null),
  });

  const saveFileContent = useCallback(
    async (next: string) => {
      if (!projectId || !selectedFile || fileError) return false;
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/files/write`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: projectId,
            path: selectedFile,
            content: next,
          }),
        });
        if (!response.ok) {
          console.error(
            `Save failed (${response.status}):`,
            await response.text().catch(() => ""),
          );
          return false;
        }
        setFileContent(next);
        return true;
      } catch (error) {
        console.error("Save failed:", error);
        return false;
      }
    },
    [projectId, selectedFile, fileError],
  );

  const editState = useEditableContent({
    content: fileContent ?? "",
    onSave: saveFileContent,
  });
  const { cancelEdit } = editState;

  useEffect(
    () => () => {
      childRequestControllers.current.forEach((controller) =>
        controller.abort(),
      );
      childRequestControllers.current.clear();
      openFileController.current?.abort();
    },
    [],
  );

  // Fetch git status
  useEffect(() => {
    if (!projectId) return;
    const baseUrl = getBaseUrl();
    const controller = new AbortController();
    fetch(
      `${baseUrl}/api/files/git-status?project_id=${encodeURIComponent(projectId)}`,
      {
        signal: controller.signal,
      },
    )
      .then((res) => (res.ok ? res.json() : { files: {} }))
      .then((data) => {
        if (!controller.signal.aborted) setGitStatus(data.files ?? {});
      })
      .catch(() => {
        if (!controller.signal.aborted) setGitStatus({});
      });
    return () => controller.abort();
  }, [projectId]);

  // Fetch root directory
  useEffect(() => {
    if (!projectId) {
      setRootEntries([]);
      setTreeNotice(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    const baseUrl = getBaseUrl();
    const controller = new AbortController();
    fetch(
      `${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=`,
      {
        signal: controller.signal,
      },
    )
      .then(async (res) => {
        if (res.ok) return { entries: await res.json(), notice: null };
        return {
          entries: [],
          notice: (await responseReportsNoCheckout(res))
            ? NO_CHECKOUT_MESSAGE
            : null,
        };
      })
      .then(({ entries, notice }) => {
        if (controller.signal.aborted) return;
        setRootEntries(Array.isArray(entries) ? entries : []);
        setTreeNotice(notice);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setRootEntries([]);
        setTreeNotice(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId]);

  const loadChildren = useCallback(
    (dirPath: string) => {
      if (!projectId || childrenMap.has(dirPath)) return;
      const baseUrl = getBaseUrl();
      const controller = new AbortController();
      childRequestControllers.current.add(controller);
      fetch(
        `${baseUrl}/api/files/tree?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(dirPath)}`,
        {
          signal: controller.signal,
        },
      )
        .then((res) => (res.ok ? res.json() : []))
        .then((data) => {
          if (controller.signal.aborted) return;
          if (dirPath === "") {
            setRootEntries(Array.isArray(data) ? data : []);
            return;
          }
          setChildrenMap((prev) => {
            const next = new Map(prev);
            next.set(dirPath, Array.isArray(data) ? data : []);
            return next;
          });
        })
        .catch(() => {
          if (controller.signal.aborted) return;
          setChildrenMap((prev) => {
            const next = new Map(prev);
            next.set(dirPath, []);
            return next;
          });
        })
        .finally(() => childRequestControllers.current.delete(controller));
    },
    [projectId, childrenMap],
  );

  const toggleDir = useCallback(
    (path: string) => {
      setExpandedPaths((prev) => {
        const next = new Set(prev);
        if (next.has(path)) {
          next.delete(path);
        } else {
          next.add(path);
          loadChildren(path);
        }
        return next;
      });
    },
    [loadChildren],
  );

  const openFile = useCallback(
    (path: string) => {
      if (!projectId) return;
      setSelectedFile(path);
      setFileLoading(true);
      setFileContent(null);
      setFileError(null);
      cancelEdit();
      const baseUrl = getBaseUrl();
      openFileController.current?.abort();
      const controller = new AbortController();
      openFileController.current = controller;
      fetch(
        `${baseUrl}/api/files/read?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(path)}`,
        {
          signal: controller.signal,
        },
      )
        .then(async (res) => {
          if (res.ok) return res.json();
          throw new FileLoadError(
            (await responseReportsNoCheckout(res))
              ? NO_CHECKOUT_MESSAGE
              : FILE_LOAD_FAILED,
          );
        })
        .then((data) => {
          if (controller.signal.aborted) return;
          if (typeof data.content !== "string")
            throw new Error("File response has no content");
          setFileContent(data.content);
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return;
          setFileContent(null);
          setFileError(
            error instanceof FileLoadError ? error.message : FILE_LOAD_FAILED,
          );
        })
        .finally(() => {
          if (!controller.signal.aborted) setFileLoading(false);
          if (openFileController.current === controller)
            openFileController.current = null;
        });
    },
    [projectId, cancelEdit],
  );

  // Context menu actions
  const handleContextMenu = useCallback(
    (e: React.MouseEvent, entry: FileEntry) => {
      e.preventDefault();
      e.stopPropagation();
      setCtxMenu({ x: e.clientX, y: e.clientY, entry });
    },
    [],
  );

  const handleActionsMenu = useCallback(
    (e: React.MouseEvent<HTMLButtonElement>, entry: FileEntry) => {
      e.stopPropagation();
      const rect = e.currentTarget.getBoundingClientRect();
      setCtxMenu({ x: rect.left, y: rect.bottom, entry });
    },
    [],
  );

  const closeCtxMenu = useCallback(() => setCtxMenu(null), []);

  const handleDelete = useCallback(
    async (entry: FileEntry) => {
      closeCtxMenu();
      if (!projectId) return;
      const ok = await confirm({
        title: "Delete file",
        description: `Delete "${entry.name}"?`,
        confirmLabel: "Delete",
        destructive: true,
      });
      if (!ok) return;
      const baseUrl = getBaseUrl();
      const response = await fetch(`${baseUrl}/api/files/delete`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, path: entry.path }),
      });
      if (!response.ok) {
        console.error(
          `Delete failed (${response.status}):`,
          await response.text().catch(() => ""),
        );
        return;
      }
      // Refresh parent directory
      const parentPath = entry.path.includes("/")
        ? entry.path.substring(0, entry.path.lastIndexOf("/"))
        : "";
      setChildrenMap((prev) => {
        const next = new Map(prev);
        next.delete(parentPath);
        return next;
      });
      loadChildren(parentPath);
      if (selectedFile === entry.path) {
        setSelectedFile(null);
        setFileContent(null);
      }
    },
    [projectId, confirm, closeCtxMenu, loadChildren, selectedFile],
  );

  const handleRename = useCallback(
    (entry: FileEntry) => {
      closeCtxMenu();
      setRenaming({ path: entry.path, name: entry.name });
      requestAnimationFrame(() => renameInputRef.current?.focus());
    },
    [closeCtxMenu],
  );

  const submitRename = useCallback(async () => {
    if (!renaming || !projectId) return;
    const newName = renameInputRef.current?.value?.trim();
    if (!newName || newName === renaming.name) {
      setRenaming(null);
      return;
    }
    const baseUrl = getBaseUrl();
    const parentPath = renaming.path.includes("/")
      ? renaming.path.substring(0, renaming.path.lastIndexOf("/"))
      : "";
    const newPath = parentPath ? `${parentPath}/${newName}` : newName;
    const response = await fetch(`${baseUrl}/api/files/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId,
        path: renaming.path,
        new_path: newPath,
      }),
    });
    if (!response.ok) {
      console.error(
        `Rename failed (${response.status}):`,
        await response.text().catch(() => ""),
      );
      setRenaming(null);
      return;
    }
    setRenaming(null);
    setChildrenMap((prev) => {
      const next = new Map(prev);
      next.delete(parentPath);
      return next;
    });
    loadChildren(parentPath);
  }, [renaming, projectId, loadChildren]);

  const handleMove = useCallback(
    async (entry: FileEntry) => {
      closeCtxMenu();
      if (!projectId) return;
      setMoving(entry);
      setMovePath(entry.path);
      setMoveError(null);
    },
    [projectId, closeCtxMenu],
  );

  const submitMove = useCallback(async () => {
    if (!projectId || !moving) return;
    const newPath = movePath.trim();
    if (!newPath || newPath === moving.path) return;
    const baseUrl = getBaseUrl();
    const response = await fetch(`${baseUrl}/api/files/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId,
        path: moving.path,
        new_path: newPath,
      }),
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      console.error(`Move failed (${response.status}):`, detail);
      setMoveError(
        isNoCheckoutErrorText(detail)
          ? NO_CHECKOUT_MESSAGE
          : detail || `Move failed (${response.status})`,
      );
      return;
    }
    const parentPath = moving.path.includes("/")
      ? moving.path.substring(0, moving.path.lastIndexOf("/"))
      : "";
    setMoving(null);
    setMovePath("");
    setMoveError(null);
    setChildrenMap((prev) => {
      const next = new Map(prev);
      next.delete(parentPath);
      return next;
    });
    loadChildren(parentPath);
  }, [projectId, moving, movePath, loadChildren]);

  const handleDuplicate = useCallback(
    async (entry: FileEntry) => {
      closeCtxMenu();
      if (!projectId || entry.is_dir) return;
      const baseUrl = getBaseUrl();
      const readRes = await fetch(
        `${baseUrl}/api/files/read?project_id=${encodeURIComponent(projectId)}&path=${encodeURIComponent(entry.path)}`,
      );
      if (!readRes.ok) {
        console.error(
          `Read failed (${readRes.status}):`,
          await readRes.text().catch(() => ""),
        );
        return;
      }
      const { content } = await readRes.json();
      const dotIdx = entry.name.lastIndexOf(".");
      const newName =
        dotIdx > 0
          ? `${entry.name.substring(0, dotIdx)} copy${entry.name.substring(dotIdx)}`
          : `${entry.name} copy`;
      const parentPath = entry.path.includes("/")
        ? entry.path.substring(0, entry.path.lastIndexOf("/"))
        : "";
      const newPath = parentPath ? `${parentPath}/${newName}` : newName;
      const writeRes = await fetch(`${baseUrl}/api/files/write`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, path: newPath, content }),
      });
      if (!writeRes.ok) {
        console.error(
          `Duplicate failed (${writeRes.status}):`,
          await writeRes.text().catch(() => ""),
        );
        return;
      }
      setChildrenMap((prev) => {
        const next = new Map(prev);
        next.delete(parentPath);
        return next;
      });
      loadChildren(parentPath);
    },
    [projectId, closeCtxMenu, loadChildren],
  );

  // Close context menu on outside click
  useEffect(() => {
    if (!ctxMenu) return;
    const handler = () => setCtxMenu(null);
    window.addEventListener("click", handler);
    return () => window.removeEventListener("click", handler);
  }, [ctxMenu]);

  useEffect(() => {
    ctxMenuRef.current
      ?.querySelector<HTMLButtonElement>('[role="menuitem"]')
      ?.focus();
  }, [ctxMenu]);

  // Hooks must be declared before any early returns (rules-of-hooks).
  const treePaneStyle = useMemo<CSSProperties | undefined>(() => {
    if (!selectedFile) {
      return undefined;
    }
    return useHorizontal
      ? { flex: "none", width: `${leftWidth}px` }
      : { flex: "none", height: `${topHeight}%` };
  }, [selectedFile, useHorizontal, leftWidth, topHeight]);

  if (loading) return <ActivityPanelEmpty body="Loading files…" />;
  if (!projectId) return <ActivityPanelEmpty body="No project selected" />;

  const language = selectedFile ? detectLanguageFromPath(selectedFile) : "text";

  return (
    <div className={`flex h-full ${useHorizontal ? "flex-row" : "flex-col"}`}>
      {/* File tree */}
      <div
        className={`overflow-y-auto ${
          selectedFile
            ? useHorizontal
              ? "border-r border-border"
              : "border-b border-border"
            : "flex-1"
        }`}
        style={treePaneStyle}
      >
        {rootEntries.length === 0 ? (
          <ActivityPanelEmpty
            icon={<FilesEmptyIcon />}
            heading="Files"
            body={
              treeNotice ?? "Project files appear here once a project is loaded"
            }
          />
        ) : (
          <FilesTabTree
            entries={rootEntries}
            expandedPaths={expandedPaths}
            selectedFile={selectedFile}
            childrenMap={childrenMap}
            renaming={renaming}
            renameInputRef={renameInputRef}
            contextMenu={ctxMenu}
            gitStatus={gitStatus}
            onToggleDirectory={toggleDir}
            onOpenFile={openFile}
            onContextMenu={handleContextMenu}
            onActionsMenu={handleActionsMenu}
            onSubmitRename={() => void submitRename()}
            onCancelRename={() => setRenaming(null)}
          />
        )}
      </div>

      {/* Resize handle */}
      {selectedFile &&
        (useHorizontal ? (
          <ResizeHandle
            direction="horizontal"
            horizontalAnchor="left"
            onResize={setLeftWidth}
            panelWidth={leftWidth}
            minWidth={FILES_TAB_LEFT_WIDTH_MIN}
            maxWidth={FILES_TAB_LEFT_WIDTH_MAX}
          />
        ) : (
          <ResizeHandle
            direction="vertical"
            onResize={setTopHeight}
            panelHeight={topHeight}
            minHeight={15}
            maxHeight={80}
          />
        ))}

      {/* File viewer */}
      {selectedFile && (
        <div
          className={`flex-1 ${useHorizontal ? "min-w-0" : ""} flex min-h-0 flex-col`}
        >
          <div className="[container-type:inline-size] flex items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 [container-name:files-viewer]">
            <span className="min-w-0 truncate text-[length:var(--text-sm)] text-[var(--text-secondary)]">
              {selectedFile}
            </span>
            <div className="flex shrink-0 items-center gap-1.5">
              <EditableViewActions
                isEditing={editState.isEditing}
                onEdit={editState.beginEdit}
                onSave={() => void editState.saveEdit()}
                onCancel={editState.cancelEdit}
                editDisabled={fileLoading || Boolean(fileError)}
                saveDisabled={Boolean(fileError)}
                buttonClassName="shrink-0"
                labelClassName="@max-[360px]/files-viewer:hidden mobile:hidden"
              />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto bg-[var(--bg-primary)] [&_pre]:not-italic [&_pre>code]:not-italic [&>div]:min-h-full">
            {fileLoading ? (
              <div className="p-3 text-xs text-muted-foreground">
                Loading...
              </div>
            ) : fileError ? (
              <div
                className="p-3 text-xs text-destructive-foreground"
                role="alert"
              >
                {fileError}
              </div>
            ) : editState.isEditing ? (
              <CodeMirrorEditor
                content={editState.editContent}
                language={language}
                readOnly={false}
                onChange={editState.setEditContent}
                onSave={() => void editState.saveEdit()}
              />
            ) : language === "markdown" ? (
              <div
                className={cn(
                  "message-content p-4 px-6 text-[length:var(--text-base)] leading-[1.7] [overflow-wrap:anywhere] text-[var(--text-primary)] not-italic",
                  markdownBodyClassName,
                )}
              >
                <MarkdownBody
                  content={fileContent ?? ""}
                  id={`files-tab-md-${selectedFile ?? "none"}`}
                />
              </div>
            ) : (
              <CodeBlock
                language={language}
                lineNumberMinWidth="3em"
                customStyle={{
                  margin: 0,
                  borderRadius: 0,
                  minHeight: "100%",
                }}
              >
                {fileContent ?? ""}
              </CodeBlock>
            )}
          </div>
        </div>
      )}

      <FilesTabContextMenu
        contextMenu={ctxMenu}
        menuRef={ctxMenuRef}
        onAddToChat={onAddToChat}
        onClose={closeCtxMenu}
        onDuplicate={(entry) => void handleDuplicate(entry)}
        onRename={handleRename}
        onMove={(entry) => void handleMove(entry)}
        onDelete={(entry) => void handleDelete(entry)}
      />
      <FilesTabMoveDialog
        moving={moving}
        formRef={moveDialogRef}
        movePath={movePath}
        error={moveError}
        onMovePathChange={(path) => {
          setMovePath(path);
          setMoveError(null);
        }}
        onClose={() => setMoving(null)}
        onSubmit={() => void submitMove()}
      />
      {ConfirmDialogElement}
    </div>
  );
});

export const FilesTab = memo(function FilesTab(props: FilesTabProps) {
  return <FilesTabProject key={props.projectId ?? "no-project"} {...props} />;
});
