/**
 * Content view for an installed skill (moats cd58d186 + 5346419d): SKILL.md
 * plus attached reference files, each read-only by default behind the shared
 * Edit/Save/Cancel flow from EditableView.
 */

import { useCallback, useEffect, useState } from "react";

import { CodeBlock } from "../../shared/CodeBlock";
import { CodeMirrorEditor } from "../../shared/CodeMirrorEditor";
import { Button } from "../../ui/Button";
import { EditableViewActions } from "../../shared/EditableView";
import { detectLanguageFromPath, useEditableContent } from "../../shared/editableContent";
import { MarkdownBody } from "../../shared/MarkdownBody";
import { saveSkillFile } from "./SkillsTabActions";
import {
  loadSkillFileContent,
  loadSkillFiles,
  type ActivitySkill,
  type SkillFileMeta,
} from "./SkillsTabData";

const SKILL_CONTENT_PATH = "SKILL.md";
interface SkillContentViewProps {
  skill: ActivitySkill;
  /** Deleted skills stay readable but never editable. */
  disabled: boolean;
  onError: (message: string | null) => void;
  /** Persist new SKILL.md content through the detail draft; true on success. */
  onSaveContent: (next: string) => Promise<boolean>;
  confirmDiscardChanges: () => Promise<boolean>;
}

export function SkillContentView({
  skill,
  disabled,
  onError,
  onSaveContent,
  confirmDiscardChanges,
}: SkillContentViewProps) {
  const [files, setFiles] = useState<SkillFileMeta[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  // Adjust-during-render: switching skills resets the view without an effect.
  const [lastSkillId, setLastSkillId] = useState(skill.id);
  if (lastSkillId !== skill.id) {
    setLastSkillId(skill.id);
    setFiles([]);
    setSelectedPath(null);
    setFileContent(null);
    setFileError(null);
    setFileLoading(false);
  }

  useEffect(() => {
    let cancelled = false;
    loadSkillFiles(skill.id)
      .then((loaded) => {
        if (!cancelled) setFiles(loaded);
      })
      .catch(() => {
        // Fail open: the skill content itself is still viewable.
        if (!cancelled) setFiles([]);
      });
    return () => {
      cancelled = true;
    };
  }, [skill.id]);

  useEffect(() => {
    if (selectedPath === null) return;
    let cancelled = false;
    loadSkillFileContent(skill.id, selectedPath)
      .then((content) => {
        if (cancelled) return;
        setFileContent(content);
        setFileLoading(false);
      })
      .catch((loadError: unknown) => {
        if (cancelled) return;
        setFileError(
          loadError instanceof Error ? loadError.message : "Failed to load skill file",
        );
        setFileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [skill.id, selectedPath]);

  const activeContent = selectedPath === null ? (skill.content ?? "") : (fileContent ?? "");

  const handleSave = useCallback(
    async (next: string) => {
      if (selectedPath === null) {
        return onSaveContent(next);
      }
      try {
        onError(null);
        await saveSkillFile(skill.id, selectedPath, next);
        setFileContent(next);
        return true;
      } catch (saveError) {
        onError(saveError instanceof Error ? saveError.message : String(saveError));
        return false;
      }
    },
    [onError, onSaveContent, selectedPath, skill.id],
  );

  const editState = useEditableContent({ content: activeContent, onSave: handleSave });
  const { cancelEdit } = editState;

  const selectPath = useCallback(
    async (path: string | null) => {
      if (path === selectedPath) return;
      if (editState.dirty && !(await confirmDiscardChanges())) {
        return;
      }
      cancelEdit();
      setSelectedPath(path);
      setFileContent(null);
      setFileError(null);
      setFileLoading(path !== null);
    },
    [cancelEdit, confirmDiscardChanges, editState.dirty, selectedPath],
  );

  const activeLabel = selectedPath ?? SKILL_CONTENT_PATH;
  const language = selectedPath === null ? "markdown" : detectLanguageFromPath(selectedPath);
  const loading = selectedPath !== null && fileLoading;
  const loadError = selectedPath !== null ? fileError : null;

  const fileRow = (label: string, path: string | null) => {
    const selected = path === selectedPath;
    return (
      <Button
        key={label}
        type="button"
        variant="ghost"
        size="sm"
        dense
        aria-current={selected || undefined}
        title={label}
        className={`w-full justify-start truncate px-2 text-xs font-normal ${
          selected
            ? "bg-[var(--accent-soft)] text-foreground"
            : "text-muted-foreground"
        }`}
        onClick={() => void selectPath(path)}
      >
        {label}
      </Button>
    );
  };

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2 p-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium text-foreground">Skill content</span>
        <div className="flex min-w-0 shrink-0 items-center gap-2">
          <span className="truncate text-xs text-muted-foreground">{activeLabel}</span>
          <EditableViewActions
            isEditing={editState.isEditing}
            onEdit={editState.beginEdit}
            onSave={() => void editState.saveEdit()}
            onCancel={editState.cancelEdit}
            editDisabled={disabled || loading || Boolean(loadError)}
            saveDisabled={Boolean(loadError)}
            saving={editState.saving}
          />
        </div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 gap-2">
        {files.length > 0 && (
          <nav
            aria-label="Skill files"
            className="flex w-44 shrink-0 flex-col gap-0.5 overflow-y-auto rounded-md border border-border bg-[var(--bg-secondary)] p-1"
          >
            {fileRow(SKILL_CONTENT_PATH, null)}
            {files.map((file) => fileRow(file.path, file.path))}
          </nav>
        )}
        <div className="min-h-80 w-full min-w-0 flex-1 overflow-hidden rounded-md border border-border bg-[var(--bg-secondary)] [&_.codemirror-container]:h-full">
          {loading ? (
            <div className="p-3 text-xs text-muted-foreground">Loading...</div>
          ) : loadError ? (
            <div className="p-3 text-xs text-destructive-foreground" role="alert">
              {loadError}
            </div>
          ) : editState.isEditing ? (
            <CodeMirrorEditor
              content={editState.editContent}
              language={language}
              readOnly={false}
              ariaLabel={
                selectedPath === null ? "Skill content markdown" : `${selectedPath} content`
              }
              editorId="skill-content-editor"
              onChange={editState.setEditContent}
              onSave={() => void editState.saveEdit()}
            />
          ) : language === "markdown" ? (
            <div className="message-content h-full min-h-0 overflow-y-auto p-3 text-sm text-foreground">
              <MarkdownBody
                content={activeContent}
                id={`skill-content-${skill.id}-${activeLabel}`}
              />
            </div>
          ) : (
            <CodeBlock
              language={language}
              lineNumberMinWidth="3em"
              customStyle={{ margin: 0, borderRadius: 0, minHeight: "100%" }}
            >
              {activeContent}
            </CodeBlock>
          )}
        </div>
      </div>
    </div>
  );
}
