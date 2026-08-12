/**
 * Read-only-first editing (moat cd58d186): content renders read-only by
 * default; an explicit Edit click opens a buffered editor that persists on
 * Save and abandons on Cancel. FilesTab is the reference implementation;
 * skill content/reference files and the rule YAML view reuse this hook with
 * the EditableViewActions buttons from EditableView.tsx.
 */

import { useCallback, useState } from "react";

export const EXT_TO_LANG: Record<string, string> = {
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  py: "python",
  rb: "ruby",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  c: "c",
  cpp: "cpp",
  cs: "csharp",
  php: "php",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  xml: "xml",
  html: "html",
  css: "css",
  scss: "scss",
  less: "less",
  md: "markdown",
  sql: "sql",
  graphql: "graphql",
  dockerfile: "docker",
  makefile: "makefile",
};

export function detectLanguageFromPath(path: string): string {
  const name = path.split("/").pop()?.toLowerCase() ?? "";
  if (name === "dockerfile") return "docker";
  if (name === "makefile") return "makefile";
  const ext = name.split(".").pop() ?? "";
  return EXT_TO_LANG[ext] ?? "text";
}

interface UseEditableContentOptions {
  /** Canonical content shown in the read view and seeded into the editor. */
  content: string;
  /** Persist the buffer; resolve true on success (exits edit mode). */
  onSave: (next: string) => Promise<boolean>;
}

export interface UseEditableContentResult {
  isEditing: boolean;
  editContent: string;
  setEditContent: (value: string) => void;
  dirty: boolean;
  saving: boolean;
  beginEdit: () => void;
  cancelEdit: () => void;
  saveEdit: () => Promise<boolean>;
}

export function useEditableContent({
  content,
  onSave,
}: UseEditableContentOptions): UseEditableContentResult {
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  const beginEdit = useCallback(() => {
    setEditContent(content);
    setIsEditing(true);
  }, [content]);

  const cancelEdit = useCallback(() => {
    setIsEditing(false);
    setEditContent(content);
  }, [content]);

  const saveEdit = useCallback(async () => {
    setSaving(true);
    try {
      const ok = await onSave(editContent);
      if (ok) setIsEditing(false);
      return ok;
    } catch {
      return false;
    } finally {
      setSaving(false);
    }
  }, [editContent, onSave]);

  return {
    isEditing,
    editContent,
    setEditContent,
    dirty: isEditing && editContent !== content,
    saving,
    beginEdit,
    cancelEdit,
    saveEdit,
  };
}
