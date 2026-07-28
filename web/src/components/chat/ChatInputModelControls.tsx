import { useState } from 'react'

import {
  formatModelDisplayLabel,
  getProviderDisplayName,
  type ProviderModelOption,
  type ReasoningOption,
} from '../../lib/providerModels'
import { SourceIcon } from '../shared/SourceIcon'
import { BranchIndicator } from './BranchIndicator'
import { BrainIcon } from './ChatInputIcons'
import { ProviderPicker } from './ProviderPicker'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from '../ui/Select'

interface ChatInputModelControlsProps {
  availableProviders: string[]
  compact?: boolean
  currentBranch?: string | null
  disabled?: boolean
  effectiveProvider: string
  hasMessages: boolean
  hideBranch?: boolean
  modelOptions: ProviderModelOption[]
  onCatalogSelect: (provider: string, model: string) => void
  onModelSelect: (model: string) => void
  onProviderSelect: (provider: string) => void
  onReasoningSelect: (effort: string) => void
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void
  projectId?: string | null
  providerPickerDisabledReason?: string | null
  reasoningOptions: ReasoningOption[]
  resolvedModelLabel: string
  resolvedModelValue: string
  resolvedReasoning: string
  selectionDisabled: boolean
  worktreePath?: string | null
  worktreePickerDisabled?: boolean
}

export function ChatInputModelControls({
  availableProviders,
  compact = false,
  currentBranch,
  disabled = false,
  effectiveProvider,
  hasMessages,
  hideBranch = false,
  modelOptions,
  onCatalogSelect,
  onModelSelect,
  onProviderSelect,
  onReasoningSelect,
  onWorktreeChange,
  projectId,
  providerPickerDisabledReason = null,
  reasoningOptions,
  resolvedModelLabel,
  resolvedModelValue,
  resolvedReasoning,
  selectionDisabled,
  worktreePath,
  worktreePickerDisabled = false,
}: ChatInputModelControlsProps) {
  const [providerPickerOpen, setProviderPickerOpen] = useState(false)
  const reasoningOnlyDisabled =
    reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled)
  const formattedModelLabel = formatModelDisplayLabel(resolvedModelLabel)
  const displayedModelLabel = compact && formattedModelLabel.length > 15
    ? `${formattedModelLabel.slice(0, 15)}...`
    : formattedModelLabel
  return (
    <div className="chat-input-controls">
      <div className="chat-input-model-controls">
        <button
          type="button"
          className="chat-input-select chat-input-select--provider chat-input-select--provider-icon !w-auto"
          disabled={selectionDisabled}
          aria-label="Select provider"
          title={providerPickerDisabledReason ?? getProviderDisplayName(effectiveProvider)}
          onClick={() => setProviderPickerOpen(true)}
        >
          <div className="chat-input-select__value">
            <SourceIcon source={effectiveProvider} size={14} />
          </div>
        </button>
        <ProviderPicker
          open={providerPickerOpen}
          onClose={() => setProviderPickerOpen(false)}
          currentProvider={effectiveProvider}
          currentModel={resolvedModelValue}
          availableProviders={availableProviders}
          onModelChange={onModelSelect}
          onProviderChange={onProviderSelect}
          onSelect={onCatalogSelect}
          hasMessages={hasMessages}
        />

        <Select
          value={resolvedModelValue}
          onValueChange={onModelSelect}
          disabled={selectionDisabled}
        >
          <SelectTrigger
            className="chat-input-select chat-input-select--model !w-auto"
            aria-label="Select model"
            title={providerPickerDisabledReason ?? 'Select model'}
          >
            <div className="chat-input-select__value">
              <span className="chat-input-select__text">{displayedModelLabel}</span>
            </div>
          </SelectTrigger>
          <SelectContent side="top" className="chat-input-select__content">
            <SelectGroup>
              <SelectLabel className="chat-input-select__label">Model</SelectLabel>
              {modelOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {formatModelDisplayLabel(option.label)}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>

        {!reasoningOnlyDisabled && (
        <Select
          value={resolvedReasoning}
          onValueChange={onReasoningSelect}
          disabled={selectionDisabled}
        >
          <SelectTrigger
            className="chat-input-select chat-input-select--reasoning !w-auto"
            aria-label="Select reasoning effort"
            title={providerPickerDisabledReason ?? 'Select reasoning effort'}
          >
            <div className="chat-input-select__value">
              <BrainIcon />
              <span className="chat-input-select__text">
                {reasoningOptions.find((option) => option.value === resolvedReasoning)?.label ??
                  'Auto'}
              </span>
            </div>
          </SelectTrigger>
          <SelectContent side="top" className="chat-input-select__content">
            <SelectGroup>
              <SelectLabel className="chat-input-select__label">Effort</SelectLabel>
              {reasoningOptions.map((option) => (
                <SelectItem
                  key={option.value}
                  value={option.value}
                  disabled={option.disabled}
                >
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        )}

        {onWorktreeChange && !hideBranch && (
          <BranchIndicator
            currentBranch={currentBranch ?? null}
            worktreePath={worktreePath ?? null}
            projectId={projectId ?? null}
            onWorktreeChange={onWorktreeChange}
            disabled={disabled || worktreePickerDisabled}
            variant="select"
            compact={compact}
          />
        )}
      </div>
    </div>
  )
}
