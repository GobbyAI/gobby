import { createContext, useContext } from 'react'
import type { ArtifactType } from '../../../types/artifacts'

interface ArtifactContextValue {
  openCodeAsArtifact: (language: string, content: string, title?: string) => void
  openFileAsArtifact: (type: ArtifactType, language: string, content: string, title?: string) => void
}

export const ArtifactContext = createContext<ArtifactContextValue | null>(null)

// Provider-less consumers are legitimate (the wiki reader renders chat code
// blocks with the no-op fallback), so the dev diagnostic fires once — a wiki
// page full of code fences must not flood the console.
let warnedMissingProvider = false

function warnMissingProviderOnce() {
  if (process.env.NODE_ENV !== 'development' || warnedMissingProvider) return
  warnedMissingProvider = true
  console.warn('useArtifactContext: no ArtifactContext provider found, using no-op fallback')
}

export function useArtifactContext() {
  const ctx = useContext(ArtifactContext)
  if (!ctx) {
    warnMissingProviderOnce()
    return { openCodeAsArtifact: () => {}, openFileAsArtifact: () => {} }
  }
  return ctx
}
