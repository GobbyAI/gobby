import { useId } from 'react'

export interface StringListFieldProps {
  label: string
  value: string[]
  onChange: (value: string[]) => void
  ariaLabel: string
  disabled?: boolean
  placeholder?: string
  addLabel?: string
}

/**
 * Ordered editable list of strings — the primitive for the audit's
 * `array<string>` "fix" rows (cors_origins, exclude_patterns, candidates, …)
 * that today fall back to a single free-text input.
 */
export function StringListField({
  label,
  value,
  onChange,
  ariaLabel,
  disabled,
  placeholder,
  addLabel = 'Add item',
}: StringListFieldProps) {
  const labelId = useId()

  function updateItem(index: number, next: string) {
    onChange(value.map((item, itemIndex) => (itemIndex === index ? next : item)))
  }

  function removeItem(index: number) {
    onChange(value.filter((_, itemIndex) => itemIndex !== index))
  }

  function addItem() {
    onChange([...value, ''])
  }

  return (
    <div className="settings-field settings-list-field" role="group" aria-labelledby={labelId}>
      <span id={labelId} className="settings-field__label">
        {label}
      </span>
      {value.length > 0 ? (
        <ul className="settings-list-field__items">
          {value.map((item, index) => (
            <li key={index} className="settings-list-field__item">
              <input
                type="text"
                className="settings-field__input"
                value={item}
                disabled={disabled}
                placeholder={placeholder}
                aria-label={`${ariaLabel} item ${index + 1}`}
                onChange={(event) => updateItem(index, event.target.value)}
              />
              <button
                type="button"
                className="btn btn-ghost btn-sm settings-list-field__remove"
                disabled={disabled}
                aria-label={`Remove ${ariaLabel} item ${index + 1}`}
                onClick={() => removeItem(index)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="settings-field__empty">No entries.</p>
      )}
      <div className="settings-field__actions">
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={disabled}
          onClick={addItem}
        >
          {addLabel}
        </button>
      </div>
    </div>
  )
}
