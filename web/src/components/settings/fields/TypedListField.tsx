import type { ReactNode } from 'react'
import { Button } from '../../ui/Button'
import { FormField } from '../../ui/FormField'

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
    <FormField label={label} group>
      {() => (
        <>
          {value.length > 0 ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {value.map((item, index) => (
                <li
                  key={index}
                  className="flex flex-col gap-3 rounded-lg border border-border bg-muted px-3.5 py-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-muted-foreground">
                      {itemLabel ? itemLabel(item, index) : `${ariaLabel} ${index + 1}`}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="shrink-0"
                      disabled={disabled}
                      aria-label={`Remove ${ariaLabel} ${index + 1}`}
                      onClick={() => removeItem(index)}
                    >
                      Remove
                    </Button>
                  </div>
                  <div className="flex flex-col gap-3">
                    {renderItem(item, (next) => updateItem(index, next), index)}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No items.</p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" disabled={disabled} onClick={addItem}>
              {addLabel}
            </Button>
          </div>
        </>
      )}
    </FormField>
  )
}
