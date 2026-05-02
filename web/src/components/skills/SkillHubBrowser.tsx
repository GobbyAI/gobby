import { useState, useCallback, useEffect } from 'react'
import type { HubInfo, HubSkillResult } from '../../hooks/useSkills'
import { cn } from '../../lib/utils'

const BROWSER_CLS = 'flex flex-col gap-4 py-3'
const CONTROLS_CLS = 'flex flex-col gap-2.5'

const TABS_CLS = 'flex flex-wrap gap-1'
const TAB_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--text-muted)] pointer-coarse:min-h-11'
const TAB_ACTIVE_CLS = 'border-[var(--accent)] bg-[var(--accent)] text-[var(--bg-primary)]'

const SEARCH_CLS = 'flex gap-1.5'
const SEARCH_INPUT_CLS =
  'flex-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-2 font-[inherit] text-[length:var(--text-base)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const SEARCH_BTN_CLS =
  'cursor-pointer rounded-md border-0 bg-[var(--accent)] px-4 py-2 text-[length:var(--text-base)] font-medium text-[var(--bg-primary)] hover:opacity-85 pointer-coarse:min-h-11'

const ERRORS_CLS = 'px-3 py-2'
const ERROR_CLS =
  'mb-1.5 rounded-md border border-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] bg-[var(--color-warning-soft)] px-2.5 py-1.5 text-[length:var(--text-base)] text-[var(--text-primary)] [&_strong]:text-[var(--color-warning-foreground)]'

const EMPTY_CLS =
  'p-8 text-center text-[var(--text-muted)] [&_code]:rounded-sm [&_code]:bg-[var(--bg-secondary)] [&_code]:px-1 [&_code]:py-px [&_code]:text-[length:var(--text-base)]'

const GRID_CLS = 'grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]'
const CARD_CLS =
  'flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4 transition-all duration-150 hover:-translate-y-px hover:border-[var(--text-muted)]'
const CARD_HEADER_CLS = 'flex items-center justify-between gap-2'
const CARD_NAME_CLS = 'text-[length:var(--text-base)] font-medium text-[var(--text-primary)]'
const CARD_HUB_CLS =
  'rounded bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-sm)] text-[var(--text-muted)]'
const CARD_DESC_CLS =
  'm-0 line-clamp-2 overflow-hidden text-[length:var(--text-sm)] text-[var(--text-secondary)]'
const CARD_FOOTER_CLS = 'mt-auto flex items-center justify-between'
const CARD_VERSION_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'

const INSTALL_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-3 py-1 text-[length:var(--text-sm)] font-medium text-[var(--bg-primary)] transition-opacity duration-150 hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

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
    <div className={BROWSER_CLS}>
      <div className={CONTROLS_CLS}>
        <div className={TABS_CLS}>
          <button
            className={cn(TAB_CLS, selectedHub === null && TAB_ACTIVE_CLS)}
            onClick={() => setSelectedHub(null)}
          >
            All Hubs
          </button>
          {hubs.map(hub => (
            <button
              key={hub.name}
              className={cn(TAB_CLS, selectedHub === hub.name && TAB_ACTIVE_CLS)}
              onClick={() => setSelectedHub(hub.name)}
              title={hub.type}
            >
              {hub.name}
            </button>
          ))}
        </div>

        <div className={SEARCH_CLS}>
          <input
            className={SEARCH_INPUT_CLS}
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search skills in hubs..."
          />
          <button className={SEARCH_BTN_CLS} onClick={handleSearch}>Search</button>
        </div>
      </div>

      {Object.keys(hubErrors).length > 0 && (
        <div className={ERRORS_CLS}>
          {Object.entries(hubErrors).map(([hub, error]) => (
            <div key={hub} className={ERROR_CLS}>
              <strong>{hub}:</strong> {error}
            </div>
          ))}
        </div>
      )}

      {hubs.length === 0 && (
        <div className={EMPTY_CLS}>
          <p>No skill hubs configured. Add hubs via the CLI: <code>gobby skills hub add</code></p>
        </div>
      )}

      {hubResults.length > 0 && (
        <div className={GRID_CLS}>
          {hubResults.map((result, i) => (
            <div key={`${result.hub_name}-${result.slug}-${i}`} className={CARD_CLS}>
              <div className={CARD_HEADER_CLS}>
                <span className={CARD_NAME_CLS}>{result.display_name || result.slug}</span>
                <span className={CARD_HUB_CLS}>{result.hub_name}</span>
              </div>
              <p className={CARD_DESC_CLS}>{result.description}</p>
              <div className={CARD_FOOTER_CLS}>
                {result.version && <span className={CARD_VERSION_CLS}>v{result.version}</span>}
                <button
                  className={INSTALL_BTN_CLS}
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
        <div className={EMPTY_CLS}>
          <p>No results found. Try a different search term.</p>
        </div>
      )}
    </div>
  )
}
