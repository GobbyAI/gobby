import { useState, useCallback, useEffect } from 'react'
import type { HubInfo, HubSkillResult } from '../../hooks/useSkills'

interface SkillHubBrowserProps {
  hubs: HubInfo[]
  hubResults: HubSkillResult[]
  hubErrors: Record<string, string>
  onFetchHubs: () => void
  onSearch: (query: string, hubName?: string) => void
  onInstall: (hubName: string, slug: string) => void
  installing: string | null
}

export function SkillHubBrowser({ hubs, hubResults, hubErrors, onFetchHubs, onSearch, onInstall, installing }: SkillHubBrowserProps) {
  const [selectedHub, setSelectedHub] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')

  useEffect(() => {
    onFetchHubs()
  }, [onFetchHubs])

  const handleSearch = useCallback(() => {
    if (searchQuery.trim()) {
      onSearch(searchQuery, selectedHub || undefined)
    }
  }, [searchQuery, selectedHub, onSearch])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleSearch()
  }, [handleSearch])

  return (
    <div className="skill-hub-browser">
      <div className="skill-hub-controls">
        <div className="skill-hub-tabs">
          <button
            className={`skill-hub-tab ${selectedHub === null ? 'skill-hub-tab--active' : ''}`}
            onClick={() => setSelectedHub(null)}
          >
            All Hubs
          </button>
          {hubs.map(hub => (
            <button
              key={hub.name}
              className={`skill-hub-tab ${selectedHub === hub.name ? 'skill-hub-tab--active' : ''}`}
              onClick={() => setSelectedHub(hub.name)}
              title={hub.type}
            >
              {hub.name}
            </button>
          ))}
        </div>

        <div className="skill-hub-search">
          <input
            className="skill-hub-search-input"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search skills in hubs..."
          />
          <button className="skill-hub-search-btn" onClick={handleSearch}>Search</button>
        </div>
      </div>

      {Object.keys(hubErrors).length > 0 && (
        <div className="skill-hub-errors">
          {Object.entries(hubErrors).map(([hub, error]) => (
            <div key={hub} className="skill-hub-error">
              <strong>{hub}:</strong> {error}
            </div>
          ))}
        </div>
      )}

      {hubs.length === 0 && (
        <div className="skill-hub-empty">
          <p>No skill hubs configured. Add hubs via the CLI: <code>gobby skills hub add</code></p>
        </div>
      )}

      {hubResults.length > 0 && (
        <div className="skill-hub-grid">
          {hubResults.map((result, i) => (
            <div key={`${result.hub_name}-${result.slug}-${i}`} className="skill-hub-card">
              <div className="skill-hub-card-header">
                <span className="skill-hub-card-name">{result.display_name || result.slug}</span>
                <span className="skill-hub-card-hub">{result.hub_name}</span>
              </div>
              <p className="skill-hub-card-desc">{result.description}</p>
              <div className="skill-hub-card-footer">
                {result.version && <span className="skill-hub-card-version">v{result.version}</span>}
                <button
                  className="skill-hub-install-btn"
                  onClick={() => onInstall(result.hub_name, result.slug)}
                  disabled={installing === `${result.hub_name}/${result.slug}`}
                >
                  {installing === `${result.hub_name}/${result.slug}` ? 'Installing...' : 'Install'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {hubResults.length === 0 && searchQuery && (
        <div className="skill-hub-empty">
          <p>No results found. Try a different search term.</p>
        </div>
      )}
    </div>
  )
}
