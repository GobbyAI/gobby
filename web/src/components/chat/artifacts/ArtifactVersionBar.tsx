import type { Artifact } from '../../../types/artifacts'
import { Button } from '../ui/Button'

interface ArtifactVersionBarProps {
  artifact: Artifact
  onSetVersion: (index: number) => void
}

export function ArtifactVersionBar({ artifact, onSetVersion }: ArtifactVersionBarProps) {
  const { versions, currentVersionIndex } = artifact
  if (versions.length <= 1) return null

  return (
    <div className="flex items-center gap-2 px-3 py-1.5 border-t border-border text-xs text-muted-foreground">
      <Button
        size="sm"
        variant="ghost"
        onClick={() => onSetVersion(currentVersionIndex - 1)}
        disabled={currentVersionIndex <= 0}
        className="h-6 w-6 p-0 pointer-coarse:h-11 pointer-coarse:w-11"
        aria-label="Previous version"
      >
        <ChevronLeftIcon />
      </Button>
      <span>
        Version {currentVersionIndex + 1} of {versions.length}
      </span>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => onSetVersion(currentVersionIndex + 1)}
        disabled={currentVersionIndex >= versions.length - 1}
        className="h-6 w-6 p-0 pointer-coarse:h-11 pointer-coarse:w-11"
        aria-label="Next version"
      >
        <ChevronRightIcon />
      </Button>
    </div>
  )
}

function ChevronLeftIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  )
}

function ChevronRightIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}
