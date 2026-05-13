import { useState, useEffect } from 'react'
import type { PullRequest } from '../../hooks/useSourceControl'
import { StatusBadge } from './StatusBadge'
import { pullRequestLabelStyle } from './githubLabelStyles'

interface Props {
  prNumber: number
  summary: PullRequest | null
  fetchDetail: (number: number) => Promise<Record<string, unknown> | null>
  onClose: () => void
}

export function PullRequestDetail({ prNumber, summary, fetchDetail, onClose }: Props) {
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setDetail(null)
    fetchDetail(prNumber)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [prNumber, fetchDetail])

  const body = (detail?.body as string) || ''
  const htmlUrl = (detail?.html_url as string) || ''
  const reviewers = (detail?.requested_reviewers as Array<{ login: string }>) || []
  const labels = (detail?.labels as Array<{ name: string; color: string }>) || []

  return (
    <div className="sc-detail-panel">
      <div className="sc-detail-panel__header">
        <h3 className="sc-detail-panel__title">
          #{prNumber} {summary?.title || ''}
        </h3>
        <button className="sc-detail-panel__close" onClick={onClose} aria-label="Close">
          &times;
        </button>
      </div>

      {loading ? (
        <div className="sc-detail-panel__body">
          <p className="sc-text-muted">Loading...</p>
        </div>
      ) : error ? (
        <div className="sc-detail-panel__body">
          <p className="sc-text-muted" style={{ color: 'var(--color-error)' }}>
            Failed to load PR details: {error}
          </p>
        </div>
      ) : (
        <div className="sc-detail-panel__body">
          <div className="sc-pr-detail__meta">
            {summary && (
              <div className="sc-pr-detail__status-row">
                <StatusBadge status={summary.draft ? 'draft' : summary.state} />
                <span className="sc-text-muted">
                  {summary.author} &middot; {summary.head_branch} &rarr; {summary.base_branch}
                </span>
              </div>
            )}

            {labels.length > 0 && (
              <div className="sc-pr-detail__labels">
                {labels.map((l) => (
                  <span
                    key={l.name}
                    className="sc-pr-detail__label"
                    style={pullRequestLabelStyle(l.color)}
                  >
                    {l.name}
                  </span>
                ))}
              </div>
            )}

            {reviewers.length > 0 && (
              <div className="sc-pr-detail__reviewers">
                <span className="sc-text-muted">Reviewers:</span>{' '}
                {reviewers.map((r) => r.login).join(', ')}
              </div>
            )}
          </div>

          {body && (
            <div className="sc-pr-detail__description">
              <h4 className="sc-detail-panel__subtitle">Description</h4>
              <pre className="sc-pr-detail__body">{body}</pre>
            </div>
          )}

          {htmlUrl && (
            <div className="sc-pr-detail__links">
              <a
                href={htmlUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="sc-btn sc-btn--sm"
              >
                View on GitHub
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
