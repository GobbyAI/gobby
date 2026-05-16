import { useState } from 'react'
import type { Issue, IssueDetail } from '../../hooks/useSourceControl'
import { StatusBadge } from './StatusBadge'
import { GitHubUnavailable } from './GitHubUnavailable'
import { issueLabelStyle } from './githubLabelStyles'
import { Heading } from '../shared/Heading'

interface Props {
  issues: Issue[]
  githubAvailable: boolean
  fetchIssues: (state?: string) => Promise<void>
  fetchIssueDetail: (number: number) => Promise<IssueDetail | null>
}

type IssueFilter = 'open' | 'closed' | 'all'

export function IssuesView({ issues, githubAvailable, fetchIssues, fetchIssueDetail }: Props) {
  const [filter, setFilter] = useState<IssueFilter>('open')
  const [selectedIssue, setSelectedIssue] = useState<number | null>(null)
  const [issueDetail, setIssueDetail] = useState<IssueDetail | null>(null)
  const [issueDetailLoading, setIssueDetailLoading] = useState(false)
  const [fetchError, setFetchError] = useState<string | null>(null)
  const [filterLoading, setFilterLoading] = useState(false)

  if (!githubAvailable) {
    return <GitHubUnavailable />
  }

  const handleFilterChange = (f: IssueFilter) => {
    setFilter(f)
    setSelectedIssue(null)
    setIssueDetail(null)
    setFetchError(null)
    setFilterLoading(true)
    fetchIssues(f === 'all' ? 'all' : f)
      .catch((e) => {
        setFetchError(e instanceof Error ? e.message : 'Failed to fetch issues')
      })
      .finally(() => setFilterLoading(false))
  }

  const handleSelectIssue = async (number: number) => {
    if (selectedIssue === number) {
      setSelectedIssue(null)
      setIssueDetail(null)
      return
    }
    setSelectedIssue(number)
    setIssueDetail(null)
    setIssueDetailLoading(true)
    try {
      const detail = await fetchIssueDetail(number)
      setIssueDetail(detail)
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : 'Failed to fetch issue detail')
      setIssueDetail(null)
    } finally {
      setIssueDetailLoading(false)
    }
  }

  return (
    <div className="sc-issues">
      {fetchError && (
        <p className="sc-text-muted" style={{ color: 'var(--color-error)', padding: '8px 0' }}>{fetchError}</p>
      )}
      <div className="sc-issues__main">
        <div className="sc-filter-chips">
          {(['open', 'closed', 'all'] as IssueFilter[]).map((f) => (
            <button
              key={f}
              className={`sc-filter-chip ${filter === f ? 'sc-filter-chip--active' : ''}`}
              onClick={() => handleFilterChange(f)}
              disabled={filterLoading}
            >
              {f}
            </button>
          ))}
        </div>

        {issues.length === 0 ? (
          <p className="sc-text-muted sc-issues__empty">No issues found</p>
        ) : (
          <table className="sc-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Title</th>
                <th>Author</th>
                <th>Labels</th>
                <th>Status</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {issues.map((issue) => (
                <tr
                  key={issue.number}
                  className={`sc-table__row ${selectedIssue === issue.number ? 'sc-table__row--selected' : ''}`}
                  onClick={() => handleSelectIssue(issue.number)}
                >
                  <td className="sc-text-muted">{issue.number}</td>
                  <td className="sc-table__cell--name">{issue.title}</td>
                  <td className="sc-text-muted">{issue.author}</td>
                  <td>
                    <div className="sc-issues__labels">
                      {issue.labels.slice(0, 3).map((lbl) => (
                        <span
                          key={lbl.name}
                          className="sc-issues__label"
                          style={issueLabelStyle(lbl.color)}
                        >
                          {lbl.name}
                        </span>
                      ))}
                      {issue.labels.length > 3 && (
                        <span className="sc-text-muted">+{issue.labels.length - 3}</span>
                      )}
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={issue.state} />
                  </td>
                  <td className="sc-text-muted">
                    {(() => {
                      const d = new Date(issue.updated_at)
                      return isNaN(d.getTime()) ? '-' : d.toLocaleDateString()
                    })()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selectedIssue !== null && issueDetailLoading && (
        <div className="sc-issues__detail">
          <p className="sc-text-muted" style={{ padding: '16px' }}>Loading issue details...</p>
        </div>
      )}
      {selectedIssue !== null && issueDetail && (
        <div className="sc-issues__detail">
          <div className="sc-issues__detail-header">
            <Heading level={3}>#{selectedIssue}: {issueDetail.title || ''}</Heading>
            <button className="sc-issues__detail-close" onClick={() => { setSelectedIssue(null); setIssueDetail(null) }}>Close</button>
          </div>
          <div className="sc-issues__detail-body">
            {issueDetail.body ? (
              <pre className="sc-issues__detail-text">{issueDetail.body}</pre>
            ) : (
              <p className="sc-text-muted">No description provided.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
