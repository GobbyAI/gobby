import type { ReactNode } from 'react'
import { useFiles } from '../hooks/useFiles'
import { FilesContextValue } from './FilesContextValue'

export function FilesProvider({ children }: { children: ReactNode }) {
  const files = useFiles()
  return <FilesContextValue.Provider value={files}>{children}</FilesContextValue.Provider>
}
