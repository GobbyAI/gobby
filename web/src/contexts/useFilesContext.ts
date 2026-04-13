import { useContext } from 'react'
import {
  FilesContextValue as FilesContext,
  type FilesContextValue,
} from './FilesContextValue'

export function useFilesContext(): FilesContextValue {
  const ctx = useContext(FilesContext)
  if (!ctx) throw new Error('useFilesContext must be used within FilesProvider')
  return ctx
}
