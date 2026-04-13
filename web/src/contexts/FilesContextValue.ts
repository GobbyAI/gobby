import { createContext } from 'react'
import { useFiles } from '../hooks/useFiles'

export type FilesContextValue = ReturnType<typeof useFiles>

export const FilesContextValue = createContext<FilesContextValue | null>(null)
