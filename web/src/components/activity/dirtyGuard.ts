import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
} from "react";

export interface DirtyGuard {
  isDirty: () => boolean;
  confirmLeave: () => Promise<boolean>;
}

export interface DirtyGuardContextValue {
  registerDirtyGuard: (guard: DirtyGuard) => () => void;
  guardedRun: (action: () => Promise<void> | void) => Promise<void>;
}

export const noopDirtyGuard: DirtyGuardContextValue = {
  registerDirtyGuard: () => () => {},
  guardedRun: async (action) => {
    await action();
  },
};

export const DirtyGuardContext =
  createContext<DirtyGuardContextValue>(noopDirtyGuard);

export function useDirtyGuard(): DirtyGuardContextValue {
  return useContext(DirtyGuardContext);
}

export function useDirtyGuardController(): DirtyGuardContextValue {
  const guardsRef = useRef<Set<DirtyGuard>>(new Set());

  const registerDirtyGuard = useCallback((guard: DirtyGuard) => {
    guardsRef.current.add(guard);
    return () => {
      guardsRef.current.delete(guard);
    };
  }, []);

  const guardedRun = useCallback(async (action: () => Promise<void> | void) => {
    const dirtyGuards = Array.from(guardsRef.current).filter((guard) =>
      guard.isDirty(),
    );
    try {
      if (dirtyGuards.length > 0) {
        for (const guard of dirtyGuards) {
          if (!(await guard.confirmLeave())) {
            return;
          }
        }
      }
      await action();
    } catch (error) {
      console.error("Dirty-guard confirmation failed", error);
      throw error;
    }
  }, []);

  return useMemo(
    () => ({ registerDirtyGuard, guardedRun }),
    [guardedRun, registerDirtyGuard],
  );
}
