import { getProviderDisplayName, type ProviderModelOption, type ReasoningOption } from '../../lib/providerModels'
import { SourceIcon } from '../shared/SourceIcon'
import { BranchIndicator } from './BranchIndicator'
import { BrainIcon } from './ChatInputIcons'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from './ui/Select'

interface ChatInputModelControlsProps {
  currentBranch?: string | null
  disabled?: boolean
  effectiveProvider: string
  modelOptions: ProviderModelOption[]
  onModelSelect: (model: string) => void
  onProviderSelect: (provider: string) => void
  onReasoningSelect: (effort: string) => void
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void
  orderedProviders: string[]
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
  currentBranch,
  disabled = false,
  effectiveProvider,
  modelOptions,
  onModelSelect,
  onProviderSelect,
  onReasoningSelect,
  onWorktreeChange,
  orderedProviders,
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
  return (
    <div className="chat-input-controls">
      <div className="chat-input-model-controls">
        <Select
          value={effectiveProvider}
          onValueChange={onProviderSelect}
          disabled={selectionDisabled}
        >
          <SelectTrigger
            className="chat-input-select chat-input-select--provider chat-input-select--provider-icon !w-auto"
            aria-label="Select provider"
            title={providerPickerDisabledReason ?? getProviderDisplayName(effectiveProvider)}
          >
            <div className="chat-input-select__value">
              <SourceIcon source={effectiveProvider} size={14} />
            </div>
          </SelectTrigger>
          <SelectContent side="top" className="chat-input-select__content">
            <SelectGroup>
              <SelectLabel className="chat-input-select__label">Provider</SelectLabel>
              {orderedProviders.map((candidateProvider) => (
                <SelectItem key={candidateProvider} value={candidateProvider}>
                  <span className="chat-input-select__item">
                    <SourceIcon source={candidateProvider} size={14} />
                    <span>{getProviderDisplayName(candidateProvider)}</span>
                  </span>
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>

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
              <span className="chat-input-select__text">{resolvedModelLabel}</span>
            </div>
          </SelectTrigger>
          <SelectContent side="top" className="chat-input-select__content">
            <SelectGroup>
              <SelectLabel className="chat-input-select__label">Model</SelectLabel>
              {modelOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>

        <Select
          value={resolvedReasoning}
          onValueChange={onReasoningSelect}
          disabled={
            selectionDisabled ||
            (reasoningOptions.length === 1 &&
              Boolean(reasoningOptions[0]?.disabled))
          }
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

        {onWorktreeChange && (
          <BranchIndicator
            currentBranch={currentBranch ?? null}
            worktreePath={worktreePath ?? null}
            projectId={projectId ?? null}
            onWorktreeChange={onWorktreeChange}
            disabled={disabled || worktreePickerDisabled}
            variant="select"
          />
        )}
      </div>
    </div>
  )
}
