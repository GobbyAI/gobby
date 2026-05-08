import { useState, useCallback, useMemo } from 'react'
import { useIntegrations } from '../../hooks/useIntegrations'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import type { Channel, ChannelType } from '../../hooks/useIntegrations'
import { ChannelCard } from './ChannelCard'
import { CHANNEL_DISPLAY_NAMES } from './channelMetadata'
import { ChannelDetail } from './ChannelDetail'
import { ChannelForm } from './ChannelForm'
import { MessageList } from './MessageList'
import {
  EMPTY_CARD_CLS,
  FILTER_BAR_CLS,
  FILTER_CHIPS_CLS,
  FILTER_CHIP_ACTIVE_CLS,
  FILTER_CHIP_CLS,
  LOADING_CLS,
  TAB_ACTIVE_CLS,
  TAB_CLS,
  TABS_CLS,
} from './styles'
import { cn } from '../../lib/utils'

const CHANNEL_TYPES: ChannelType[] = ['slack', 'telegram', 'discord', 'teams', 'email', 'sms', 'gobby_chat']

const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden px-3 md:px-5'
const ERROR_TOAST_CLS =
  'app-toast bg-[var(--color-error)] text-[var(--text-on-error)] [box-shadow:var(--shadow-md)]'

const TOOLBAR_CLS = 'flex items-center justify-between gap-4 pb-3 pt-4 max-md:flex-col max-md:items-stretch'
const TOOLBAR_LEFT_CLS = 'flex items-center gap-3'
const TOOLBAR_TITLE_CLS = 'm-0 text-[length:var(--font-size-base)] font-semibold'
const TOOLBAR_RIGHT_CLS = 'flex items-center gap-2 max-md:justify-between'
const SEARCH_CLS =
  'w-[200px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] max-md:w-full pointer-coarse:min-h-11'
const NEW_BTN_CLS =
  'cursor-pointer rounded-md border-0 bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-opacity duration-150 hover:opacity-90 pointer-coarse:min-h-11'

const CHANNEL_GRID_CLS =
  'grid flex-1 gap-3 overflow-y-auto pb-5 [grid-template-columns:repeat(auto-fill,minmax(280px,1fr))] max-md:grid-cols-1'

const EMPTY_STATE_CLS =
  'flex flex-1 flex-col items-center justify-center gap-6 p-10 text-[var(--text-secondary)]'
const EMPTY_TITLE_CLS = 'm-0 text-[length:var(--font-size-base)] font-medium text-[var(--text-primary)]'
const EMPTY_SUBTITLE_CLS = 'm-0 text-[length:var(--text-sm)]'
const EMPTY_CARDS_CLS =
  'grid w-full max-w-[640px] gap-2.5 [grid-template-columns:repeat(auto-fill,minmax(140px,1fr))] max-md:grid-cols-2'

const CONTENT_CLS = 'flex flex-1 flex-col overflow-hidden'
export function IntegrationsPage() {
  const {
    channels,
    isLoading,
    searchText,
    setSearchText,
    channelTypeFilter,
    setChannelTypeFilter,
    createChannel,
    removeChannel,
    updateChannel,
    fetchChannelStatus,
    messages,
    messageFilters,
    setMessageFilters,
    fetchMessages,
  } = useIntegrations()

  const [activeTab, setActiveTab] = useState<'channels' | 'messages'>('channels')
  const [showAddForm, setShowAddForm] = useState(false)
  const [editingChannel, setEditingChannel] = useState<Channel | null>(null)
  const [selectedChannel, setSelectedChannel] = useState<Channel | null>(null)
  const [presetType, setPresetType] = useState<ChannelType | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const { confirm, ConfirmDialogElement } = useConfirmDialog()

  const showError = useCallback((msg: string) => {
    setErrorMessage(msg)
    setTimeout(() => setErrorMessage(null), 4000)
  }, [])

  const filteredChannels = useMemo(() => {
    let result = channels
    if (channelTypeFilter) {
      result = result.filter(c => c.channel_type === channelTypeFilter)
    }
    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(c => c.name.toLowerCase().includes(q))
    }
    return result
  }, [channels, channelTypeFilter, searchText])

  const handleToggleEnabled = useCallback(async (channel: Channel) => {
    const success = await updateChannel(channel.id, { enabled: !channel.enabled })
    if (!success) showError(`Failed to ${channel.enabled ? 'disable' : 'enable'} channel`)
  }, [updateChannel, showError])

  const handleRemove = useCallback(async (channel: Channel) => {
    const ok = await confirm({ title: 'Remove channel', description: `Remove channel "${channel.name}"? This cannot be undone.`, confirmLabel: 'Remove', destructive: true })
    if (!ok) return
    const success = await removeChannel(channel.id)
    if (!success) showError('Failed to remove channel')
  }, [confirm, removeChannel, showError])

  const handleEmptyCardClick = useCallback((type: ChannelType) => {
    setPresetType(type)
    setShowAddForm(true)
  }, [])

  if (isLoading) {
    return (
      <div className={PAGE_CLS}>
        <div className={LOADING_CLS}>Loading integrations...</div>
      </div>
    )
  }

  return (
    <div className={PAGE_CLS}>
      {errorMessage && (
        <button
          type="button"
          className={ERROR_TOAST_CLS}
          onClick={() => setErrorMessage(null)}
          aria-label={`Dismiss error: ${errorMessage}`}
        >
          {errorMessage}
        </button>
      )}

      {/* Toolbar */}
      <div className={TOOLBAR_CLS}>
        <div className={TOOLBAR_LEFT_CLS}>
          <h1 className={TOOLBAR_TITLE_CLS}>Integrations</h1>
        </div>
        <div className={TOOLBAR_RIGHT_CLS}>
          <input
            className={SEARCH_CLS}
            type="text"
            placeholder="Search"
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className={NEW_BTN_CLS}
            onClick={() => {
              setPresetType(null)
              setShowAddForm(true)
            }}
          >
            + Add Integration
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className={TABS_CLS}>
        <button
          className={cn(TAB_CLS, activeTab === 'channels' && TAB_ACTIVE_CLS)}
          onClick={() => setActiveTab('channels')}
        >
          Channels ({channels.length})
        </button>
        <button
          className={cn(TAB_CLS, activeTab === 'messages' && TAB_ACTIVE_CLS)}
          onClick={() => setActiveTab('messages')}
        >
          Messages
        </button>
      </div>

      {/* Content */}
      <div className={CONTENT_CLS}>
        {activeTab === 'channels' ? (
          <>
            {/* Filter chips */}
            <div className={FILTER_BAR_CLS}>
              <div className={FILTER_CHIPS_CLS}>
                <button
                  className={cn(FILTER_CHIP_CLS, !channelTypeFilter && FILTER_CHIP_ACTIVE_CLS)}
                  onClick={() => setChannelTypeFilter(null)}
                >
                  All
                </button>
                {CHANNEL_TYPES.map(type => (
                  <button
                    key={type}
                    className={cn(FILTER_CHIP_CLS, channelTypeFilter === type && FILTER_CHIP_ACTIVE_CLS)}
                    onClick={() => setChannelTypeFilter(channelTypeFilter === type ? null : type)}
                  >
                    {CHANNEL_DISPLAY_NAMES[type]}
                  </button>
                ))}
              </div>
            </div>

            {/* Channel grid or empty state */}
            {filteredChannels.length > 0 ? (
              <div className={CHANNEL_GRID_CLS}>
                {filteredChannels.map(channel => (
                  <ChannelCard
                    key={channel.id}
                    channel={channel}
                    onSelect={setSelectedChannel}
                    onEdit={setEditingChannel}
                    onToggleEnabled={handleToggleEnabled}
                    onRemove={handleRemove}
                  />
                ))}
              </div>
            ) : channels.length === 0 ? (
              <div className={EMPTY_STATE_CLS}>
                <h3 className={EMPTY_TITLE_CLS}>No integrations configured</h3>
                <p className={EMPTY_SUBTITLE_CLS}>Connect a messaging platform to get started</p>
                <div className={EMPTY_CARDS_CLS}>
                  {CHANNEL_TYPES.filter(t => t !== 'gobby_chat').map(type => (
                    <div
                      key={type}
                      className={EMPTY_CARD_CLS}
                      onClick={() => handleEmptyCardClick(type)}
                    >
                      <PlatformIcon type={type} />
                      {CHANNEL_DISPLAY_NAMES[type]}
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className={EMPTY_STATE_CLS}>
                <p className={EMPTY_SUBTITLE_CLS}>No channels match your filters</p>
              </div>
            )}
          </>
        ) : (
          <MessageList
            channels={channels}
            messages={messages}
            filters={messageFilters}
            onFiltersChange={setMessageFilters}
            onFetchMessages={fetchMessages}
          />
        )}
      </div>

      {/* Add/Edit modal */}
      {showAddForm && (
        <ChannelForm
          mode="add"
          presetType={presetType}
          onSubmit={async (type, name, config, secrets) => {
            const ok = await createChannel(type, name, config, secrets)
            if (!ok) showError('Failed to add channel')
            return ok
          }}
          onClose={() => setShowAddForm(false)}
        />
      )}
      {editingChannel && (
        <ChannelForm
          mode="edit"
          channel={editingChannel}
          onSubmit={async (_type, _name, config, _secrets) => {
            const updates: Record<string, unknown> = {}
            if (Object.keys(config).length > 0) updates.config = config
            const ok = await updateChannel(editingChannel.id, updates as { config?: Record<string, unknown> })
            if (!ok) showError('Failed to update channel')
            return ok
          }}
          onClose={() => setEditingChannel(null)}
        />
      )}

      {/* Detail slide-out */}
      <ChannelDetail
        channel={selectedChannel}
        onClose={() => setSelectedChannel(null)}
        onEdit={(ch) => { setSelectedChannel(null); setEditingChannel(ch) }}
        onToggleEnabled={(ch) => { handleToggleEnabled(ch); setSelectedChannel(null) }}
        onRemove={(ch) => { handleRemove(ch); setSelectedChannel(null) }}
        fetchStatus={fetchChannelStatus}
      />
      {ConfirmDialogElement}
    </div>
  )
}

export function PlatformIcon({ type, size = 16 }: { type: ChannelType; size?: number }) {
  const props = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  }

  switch (type) {
    case 'slack':
      return (
        <svg {...props}>
          <line x1="12" y1="2" x2="12" y2="22" />
          <line x1="2" y1="12" x2="22" y2="12" />
        </svg>
      )
    case 'telegram':
      return (
        <svg {...props}>
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" fill="none" />
        </svg>
      )
    case 'discord':
      return (
        <svg {...props}>
          <path d="M6 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2" />
          <path d="M18 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2" />
          <path d="M8 4c-2 0-4 1-5 3 4 8 6 13 9 13s5-5 9-13c-1-2-3-3-5-3" />
        </svg>
      )
    case 'teams':
      return (
        <svg {...props}>
          <rect x="3" y="3" width="8" height="8" rx="1" />
          <rect x="13" y="3" width="8" height="8" rx="1" />
          <rect x="3" y="13" width="8" height="8" rx="1" />
          <rect x="13" y="13" width="8" height="8" rx="1" />
        </svg>
      )
    case 'email':
      return (
        <svg {...props}>
          <rect x="2" y="4" width="20" height="16" rx="2" />
          <path d="M22 7l-10 7L2 7" />
        </svg>
      )
    case 'sms':
      return (
        <svg {...props}>
          <rect x="5" y="2" width="14" height="20" rx="2" />
          <line x1="12" y1="18" x2="12.01" y2="18" />
        </svg>
      )
    case 'gobby_chat':
      return (
        <svg {...props}>
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      )
  }
}
