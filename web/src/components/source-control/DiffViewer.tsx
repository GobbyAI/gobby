import { useMemo } from 'react'
import type { DiffResult } from '../../hooks/useSourceControl'
import { Heading } from '../shared/Heading'

interface Props {
  diff: DiffResult
}

function classForLine(line: string): string {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'sc-diff__line sc-diff__line--added'
  if (line.startsWith('-') && !line.startsWith('---')) return 'sc-diff__line sc-diff__line--removed'
  if (line.startsWith('@@')) return 'sc-diff__line sc-diff__line--hunk'
  if (line.startsWith('diff ')) return 'sc-diff__line sc-diff__line--header'
  return 'sc-diff__line'
}

export function DiffViewer({ diff }: Props) {
  const patchLines = useMemo(
    () => diff.patch ? diff.patch.split('\n').map((line, i) => ({ key: i, className: classForLine(line), text: line })) : [],
    [diff.patch],
  )

  return (
    <div className="sc-diff">
      {diff.files.length > 0 && (
        <div className="sc-diff__files">
          <Heading level={4} className="sc-diff__files-title">
            {diff.files.length} file{diff.files.length !== 1 ? 's' : ''} changed
          </Heading>
          <div className="sc-diff__file-list">
            {diff.files.map((f) => (
              <div key={f.path} className="sc-diff__file">
                <span className={`sc-diff__file-status sc-diff__file-status--${f.status}`}>
                  {f.status}
                </span>
                <span className="sc-diff__file-path">{f.path}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {patchLines.length > 0 && (
        <div className="sc-diff__patch">
          <pre className="sc-diff__patch-content">
            {patchLines.map((l) => (
              <div key={l.key} className={l.className}>
                {l.text}
              </div>
            ))}
          </pre>
        </div>
      )}

      {!diff.patch && diff.files.length === 0 && (
        <p className="sc-text-muted">No differences found</p>
      )}
    </div>
  )
}
