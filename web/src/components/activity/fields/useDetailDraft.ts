import { useCallback, useEffect, useRef, useState } from "react";

import { useDirtyGuard } from "../dirtyGuard";

interface UseDetailDraftOptions<T extends object> {
  source: T | null;
  onSave: (draft: T) => Promise<boolean>;
}

export interface UseDetailDraftResult<T extends object> {
  draft: T | null;
  setField: <K extends keyof T>(key: K, value: T[K]) => void;
  dirty: boolean;
  saving: boolean;
  serverChanged: boolean;
  save: (overrideDraft?: T) => Promise<boolean>;
  discard: () => void;
  confirmIfDirty: (next: () => void) => void;
}

function cloneSource<T extends object>(source: T | null): T | null {
  return source === null ? null : { ...source };
}

/** Structural equality over the JSON-shaped values drafts hold. */
function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (
    left === null ||
    right === null ||
    typeof left !== "object" ||
    typeof right !== "object"
  ) {
    return false;
  }
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const leftEntries = Object.entries(left as Record<string, unknown>);
  const rightRecord = right as Record<string, unknown>;
  if (leftEntries.length !== Object.keys(rightRecord).length) return false;
  return leftEntries.every(
    ([key, value]) =>
      Object.prototype.hasOwnProperty.call(rightRecord, key) &&
      deepEqual(value, rightRecord[key]),
  );
}

const DISCARD_UNSAVED_CHANGES_MESSAGE = "Discard unsaved changes?";

export function useDetailDraft<T extends object>({
  source,
  onSave,
}: UseDetailDraftOptions<T>): UseDetailDraftResult<T> {
  const { registerDirtyGuard } = useDirtyGuard();
  const latestSourceRef = useRef<T | null>(source);
  const dirtyRef = useRef(false);
  const editedKeysRef = useRef<Set<keyof T>>(new Set());
  const [draft, setDraft] = useState<T | null>(() => cloneSource(source));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [serverChanged, setServerChanged] = useState(false);

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    const previous = latestSourceRef.current;
    latestSourceRef.current = source;
    if (dirtyRef.current) {
      // A refetch that returns identical values (post-save convergence, an
      // unrelated key changing elsewhere in the config) is not a server
      // change; only genuinely different owned values warrant the warning.
      if (!deepEqual(previous, source)) setServerChanged(true);
      return;
    }
    editedKeysRef.current.clear();
    dirtyRef.current = false;
    setServerChanged(false);
    setDraft(cloneSource(source));
  }, [source]);

  const setField = useCallback(<K extends keyof T>(key: K, value: T[K]) => {
    editedKeysRef.current.add(key);
    dirtyRef.current = true;
    setDirty(true);
    setDraft((current) => {
      if (current === null) return null;
      return { ...current, [key]: value };
    });
  }, []);

  const discard = useCallback(() => {
    editedKeysRef.current.clear();
    dirtyRef.current = false;
    setDirty(false);
    setServerChanged(false);
    setDraft(cloneSource(latestSourceRef.current));
  }, []);

  const save = useCallback(
    async (overrideDraft?: T) => {
      const target = overrideDraft ?? draft;
      if (target === null || target === undefined) return false;
      const latest = latestSourceRef.current ?? target;
      const merged = { ...latest };

      if (overrideDraft) {
        for (const key of Object.keys(overrideDraft) as Array<keyof T>) {
          merged[key] = overrideDraft[key];
        }
      } else {
        for (const key of editedKeysRef.current) {
          merged[key] = target[key];
        }
      }

      setSaving(true);
      try {
        const saved = await onSave(merged);
        if (!saved) return false;
        latestSourceRef.current = merged;
        editedKeysRef.current.clear();
        dirtyRef.current = false;
        setDraft(cloneSource(merged));
        setDirty(false);
        setServerChanged(false);
        return true;
      } finally {
        setSaving(false);
      }
    },
    [draft, onSave],
  );

  const confirmLeave = useCallback(async () => {
    if (!dirtyRef.current) return true;
    if (!window.confirm(DISCARD_UNSAVED_CHANGES_MESSAGE)) return false;
    discard();
    return true;
  }, [discard]);

  const confirmLeaveRef = useRef(confirmLeave);
  useEffect(() => {
    confirmLeaveRef.current = confirmLeave;
  }, [confirmLeave]);

  useEffect(
    () =>
      registerDirtyGuard({
        isDirty: () => dirtyRef.current,
        confirmLeave: () => confirmLeaveRef.current(),
      }),
    [registerDirtyGuard],
  );

  const confirmIfDirty = useCallback(
    (next: () => void) => {
      if (!dirtyRef.current) {
        next();
        return;
      }
      if (!window.confirm(DISCARD_UNSAVED_CHANGES_MESSAGE)) return;
      discard();
      next();
    },
    [discard],
  );

  return {
    draft,
    setField,
    dirty,
    saving,
    serverChanged,
    save,
    discard,
    confirmIfDirty,
  };
}
