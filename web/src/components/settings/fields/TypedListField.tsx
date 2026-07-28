import { useId } from 'react'
import type { ReactNode } from 'react'
import { Button } from '../../ui/Button'

export interface TypedListFieldProps<T> {
  label: string
  value: T[]
  onChange: (value: T[]) => void
  /** Renders one item's editor. Receives the item, an update callback, and its index. */
  renderItem: (
    item: T,
    onItemChange: (next: T) => void,
    index: number,
  ) => ReactNode
  /** Builds a blank item when the user adds a row. */
  createItem: () => T
  ariaLabel: string
  disabled?: boolean
  addLabel?: string
  /** Optional per-item heading (e.g. a name field) shown above its editor. */
  itemLabel?: (item: T, index: number) => string
}

/**
 * Editable list of structured items — the primitive for the audit's
 * `array<object>` "fix" rows (tool_approval.policies, wiki.roots, webhook
 * endpoints, …). Each item's shape and editor are supplied by the caller.
 */
export function TypedListField<T>({
  label,
  value,
  onChange,
  renderItem,
  createItem,
  ariaLabel,
  disabled,
  addLabel = 'Add item',
  itemLabel,
}: TypedListFieldProps<T>) {
  const labelId = useId()

  function updateItem(index: number, next: T) {
    onChange(value.map((item, itemIndex) => (itemIndex === index ? next : item)))
  }

  function removeItem(index: number) {
    onChange(value.filter((_, itemIndex) => itemIndex !== index))
  }

  function addItem() {
    onChange([...value, createItem()])
  }

  return (
    <div className="settings-field settings-typed-list" role="group" aria-labelledby={labelId}>
      <span id={labelId} className="settings-field__label">
        {label}
      </span>
      {value.length > 0 ? (
        <ul className="settings-typed-list__items">
          {value.map((item, index) => (
            <li key={index} className="settings-typed-list__item">
              <div className="settings-typed-list__item-head">
                <span className="settings-typed-list__item-title">
                  {itemLabel ? itemLabel(item, index) : `${ariaLabel} ${index + 1}`}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="settings-typed-list__remove"
                  disabled={disabled}
                  aria-label={`Remove ${ariaLabel} ${index + 1}`}
                  onClick={() => removeItem(index)}
                >
                  Remove
                </Button>
              </div>
              <div className="settings-typed-list__item-body">
                {renderItem(item, (next) => updateItem(index, next), index)}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="settings-field__empty">No items.</p>
      )}
      <div className="settings-field__actions">
        <Button
          type="button"
          size="sm"
          disabled={disabled}
          onClick={addItem}
        >
          {addLabel}
        </Button>
      </div>
    </div>
  )
}
