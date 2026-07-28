import { useId } from 'react'
import type { ReactNode } from 'react'
import { Button } from '../../ui/Button'

export interface KeyValueMapFieldProps<V = string> {
  label: string
  value: Record<string, V>
  onChange: (value: Record<string, V>) => void
  ariaLabel: string
  disabled?: boolean
  keyPlaceholder?: string
  addLabel?: string
  /**
   * Renders the editor for one entry's value. Defaults to a text input, which
   * covers `map<string>` rows; pass a custom renderer for `map<number>`,
   * `map<array>`, or `map<object>` rows.
   */
  renderValue?: (
    value: V,
    onValueChange: (next: V) => void,
    key: string,
  ) => ReactNode
  /** Seed value for a freshly added entry. Defaults to an empty string. */
  createValue?: () => V
}

function defaultRenderValue(
  value: unknown,
  onValueChange: (next: string) => void,
  key: string,
): ReactNode {
  return (
    <input
      type="text"
      className="settings-field__input"
      value={typeof value === 'string' ? value : String(value ?? '')}
      aria-label={`Value for ${key || 'new entry'}`}
      onChange={(event) => onValueChange(event.target.value)}
    />
  )
}

/**
 * Editable key/value map — the primitive for the audit's `map<*>` "fix" rows.
 * Order is preserved by rebuilding from entries on every change, so renaming a
 * key does not reshuffle the map.
 */
export function KeyValueMapField<V = string>({
  label,
  value,
  onChange,
  ariaLabel,
  disabled,
  keyPlaceholder,
  addLabel = 'Add entry',
  renderValue,
  createValue,
}: KeyValueMapFieldProps<V>) {
  const labelId = useId()
  const entries = Object.entries(value) as Array<[string, V]>

  function commit(nextEntries: Array<[string, V]>) {
    onChange(Object.fromEntries(nextEntries) as Record<string, V>)
  }

  function updateKey(index: number, nextKey: string) {
    commit(
      entries.map((entry, entryIndex) =>
        entryIndex === index ? [nextKey, entry[1]] : entry,
      ),
    )
  }

  function updateValue(index: number, nextValue: V) {
    commit(
      entries.map((entry, entryIndex) =>
        entryIndex === index ? [entry[0], nextValue] : entry,
      ),
    )
  }

  function removeEntry(index: number) {
    commit(entries.filter((_, entryIndex) => entryIndex !== index))
  }

  function addEntry() {
    const seed = createValue ? createValue() : ('' as V)
    commit([...entries, ['', seed]])
  }

  return (
    <div className="settings-field settings-map-field" role="group" aria-labelledby={labelId}>
      <span id={labelId} className="settings-field__label">
        {label}
      </span>
      {entries.length > 0 ? (
        <ul className="settings-map-field__items">
          {entries.map(([key, entryValue], index) => (
            <li key={index} className="settings-map-field__row">
              <input
                type="text"
                className="settings-field__input settings-map-field__key"
                value={key}
                disabled={disabled}
                placeholder={keyPlaceholder}
                aria-label={`${ariaLabel} key ${index + 1}`}
                onChange={(event) => updateKey(index, event.target.value)}
              />
              <div className="settings-map-field__value">
                {(renderValue ?? defaultRenderValue)(
                  entryValue,
                  (next) => updateValue(index, next as V),
                  key,
                )}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="settings-map-field__remove"
                disabled={disabled}
                aria-label={key ? `Remove ${key}` : `Remove ${ariaLabel} entry ${index + 1}`}
                onClick={() => removeEntry(index)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="settings-field__empty">No entries.</p>
      )}
      <div className="settings-field__actions">
        <Button
          type="button"
          size="sm"
          disabled={disabled}
          onClick={addEntry}
        >
          {addLabel}
        </Button>
      </div>
    </div>
  )
}
