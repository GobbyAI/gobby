export interface FilesTabProps {
  projectId?: string | null
  onAddToChat?: (filePath: string) => void
  layout?: 'stack' | 'responsive-split'
}

export interface FileEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
  extension?: string
}

export interface ContextMenuState {
  x: number
  y: number
  entry: FileEntry
}

export interface RenamingState {
  path: string
  name: string
}
