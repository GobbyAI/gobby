import { useState, useCallback, useMemo } from 'react'
import { useIntegrations } from '../../hooks/useIntegrations'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import type { Channel, ChannelType } from '../../hooks/useIntegrations'
import { ChannelCard } from './ChannelCard'
import { CHANNEL_DISPLAY_NAMES } from './channelMetadata'
import { ChannelDetail } from './ChannelDetail'
import { ChannelForm } from './ChannelForm'
import { MessageList } from './MessageList'
import './IntegrationsPage.css'

const CHANNEL_TYPES: ChannelType[] = ['slack', 'telegram', 'discord', 'teams', 'email', 'sms', 'gobby_chat']

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
      <div className="intg-page">
        <div className="intg-loading">Loading integrations...</div>
      </div>
    )
  }

  return (
    <div className="intg-page">
      {errorMessage && (
        <div className="intg-error-toast" onClick={() => setErrorMessage(null)}>
          {errorMessage}
        </div>
      )}

      {/* Toolbar */}
      <div className="intg-toolbar">
        <div className="intg-toolbar-left">
          <h2 className="intg-toolbar-title">Integrations</h2>
        </div>
        <div className="intg-toolbar-right">
          <input
            className="intg-search"
            type="text"
            placeholder="Search channels..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className="intg-new-btn"
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
      <div className="intg-tabs">
        <button
          className={`intg-tab ${activeTab === 'channels' ? 'intg-tab--active' : ''}`}
          onClick={() => setActiveTab('channels')}
        >
          Channels ({channels.length})
        </button>
        <button
          className={`intg-tab ${activeTab === 'messages' ? 'intg-tab--active' : ''}`}
          onClick={() => setActiveTab('messages')}
        >
          Messages
        </button>
      </div>

      {/* Content */}
      <div className="intg-content">
        {activeTab === 'channels' ? (
          <>
            {/* Filter chips */}
            <div className="intg-filter-bar">
              <div className="intg-filter-chips">
                <button
                  className={`intg-filter-chip ${!channelTypeFilter ? 'intg-filter-chip--active' : ''}`}
                  onClick={() => setChannelTypeFilter(null)}
                >
                  All
                </button>
                {CHANNEL_TYPES.map(type => (
                  <button
                    key={type}
                    className={`intg-filter-chip ${channelTypeFilter === type ? 'intg-filter-chip--active' : ''}`}
                    onClick={() => setChannelTypeFilter(channelTypeFilter === type ? null : type)}
                  >
                    {CHANNEL_DISPLAY_NAMES[type]}
                  </button>
                ))}
              </div>
            </div>

            {/* Channel grid or empty state */}
            {filteredChannels.length > 0 ? (
              <div className="intg-channel-grid">
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
              <div className="intg-empty-state">
                <h3 className="intg-empty-title">No integrations configured</h3>
                <p className="intg-empty-subtitle">Connect a messaging platform to get started</p>
                <div className="intg-empty-cards">
                  {CHANNEL_TYPES.filter(t => t !== 'gobby_chat').map(type => (
                    <div
                      key={type}
                      className="intg-empty-card"
                      onClick={() => handleEmptyCardClick(type)}
                    >
                      <PlatformIcon type={type} />
                      {CHANNEL_DISPLAY_NAMES[type]}
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="intg-empty-state">
                <p className="intg-empty-subtitle">No channels match your filters</p>
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
