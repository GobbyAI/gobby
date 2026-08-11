import type { ReactNode } from 'react'
import { Button } from '../../ui/Button'
import { FormField } from '../../ui/FormField'
import { Input } from '../../ui/Input'

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

export interface KeyValueMapRow<V> {
  storedKey: string
  displayKey: string
  value: V
}

export interface KeyValueRowsFieldProps<V = string>
  extends Omit<KeyValueMapFieldProps<V>, 'value' | 'onChange'> {
  value: KeyValueMapRow<V>[]
  onChange: (value: KeyValueMapRow<V>[]) => void
}

function defaultRenderValue(
  value: unknown,
  onValueChange: (next: string) => void,
  key: string,
): ReactNode {
  return (
    <Input
      type="text"
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
export function KeyValueRowsField<V = string>({
  label,
  value,
  onChange,
  ariaLabel,
  disabled,
  keyPlaceholder,
  addLabel = 'Add entry',
  renderValue,
  createValue,
}: KeyValueRowsFieldProps<V>) {
  const entries = value

  function commit(nextEntries: KeyValueMapRow<V>[]) {
    onChange(nextEntries)
  }

  function updateKey(index: number, nextKey: string) {
    commit(
      entries.map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, displayKey: nextKey } : entry,
      ),
    )
  }

  function updateValue(index: number, nextValue: V) {
    commit(
      entries.map((entry, entryIndex) =>
        entryIndex === index ? { ...entry, value: nextValue } : entry,
      ),
    )
  }

  function removeEntry(index: number) {
    commit(entries.filter((_, entryIndex) => entryIndex !== index))
  }

  function addEntry() {
    const seed = createValue ? createValue() : ('' as V)
    commit([...entries, { storedKey: '', displayKey: '', value: seed }])
  }

  return (
    <FormField label={label} group>
      {() => (
        <>
          {entries.length > 0 ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {entries.map((entry, index) => (
                <li key={index} className="flex flex-wrap items-center gap-2">
                  <Input
                    type="text"
                    wrapperClassName="flex-[1_1_8rem]"
                    value={entry.displayKey}
                    disabled={disabled}
                    placeholder={keyPlaceholder}
                    aria-label={`${ariaLabel} key ${index + 1}`}
                    onChange={(event) => updateKey(index, event.target.value)}
                  />
                  <div className="min-w-0 flex-[2_1_12rem]">
                    {(renderValue ?? defaultRenderValue)(
                      entry.value,
                      (next) => updateValue(index, next as V),
                      entry.displayKey,
                    )}
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="shrink-0"
                    disabled={disabled}
                    aria-label={
                      entry.displayKey
                        ? `Remove ${entry.displayKey}`
                        : `Remove ${ariaLabel} entry ${index + 1}`
                    }
                    onClick={() => removeEntry(index)}
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No entries.</p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" disabled={disabled} onClick={addEntry}>
              {addLabel}
            </Button>
          </div>
        </>
      )}
    </FormField>
  )
}

export function KeyValueMapField<V = string>(props: KeyValueMapFieldProps<V>) {
  const rows = Object.entries(props.value).map(([key, value]) => ({
    storedKey: key,
    displayKey: key,
    value,
  }))
  return (
    <KeyValueRowsField
      {...props}
      value={rows}
      onChange={(nextRows) =>
        props.onChange(
          Object.fromEntries(
            nextRows.map(({ displayKey, value }) => [displayKey, value]),
          ),
        )
      }
    />
  )
}
