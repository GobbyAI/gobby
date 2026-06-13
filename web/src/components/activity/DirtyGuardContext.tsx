import type { ReactNode } from "react";

import { DirtyGuardContext, type DirtyGuardContextValue } from "./dirtyGuard";

export function DirtyGuardProvider({
  children,
  value,
}: {
  children: ReactNode;
  value: DirtyGuardContextValue;
}) {
  return (
    <DirtyGuardContext.Provider value={value}>
      {children}
    </DirtyGuardContext.Provider>
  );
}
