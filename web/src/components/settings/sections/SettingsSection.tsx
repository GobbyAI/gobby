import { useEffect, useMemo, useRef } from 'react'
import type { ReactNode } from 'react'
import { DetailActionButton } from '../../activity/fields'
import { useDetailDraft } from '../../activity/fields/useDetailDraft'
import { getSettingsSection } from '../sections'
import type { SettingsSectionId } from '../sections'
import { useSettingsSectionContext } from './SettingsSectionContext'

/**
 * The render surface handed to a section body. Sections read and write only
 * their owned config dotted-paths through these helpers; everything is scoped
 * to the section's per-load draft, so edits never leak across sections.
 */
export interface SettingsSectionFields {
  getValue: (path: string) => unknown
  setValue: (path: string, value: unknown) => void
  schema: Record<string, unknown> | null
  secretKeys: string[]
  isLoading: boolean
}

export interface SettingsSectionProps {
  sectionId: SettingsSectionId
  /** Config dotted-paths this section owns. The draft is scoped to these. */
  ownedPaths: readonly string[]
  saveDisabled?: boolean
  children: (fields: SettingsSectionFields) => ReactNode
}

type SectionDraft = Record<string, unknown>

/**
 * Read a value out of the nested config object by dotted path. `/api/config/values`
 * returns config as a nested object (not flat dotted keys), so a dotted owned
 * path must be walked segment by segment.
 */
function getByPath(source: Record<string, unknown>, path: string): unknown {
  let current: unknown = source
  for (const segment of path.split('.')) {
    if (current === null || typeof current !== 'object') return undefined
    current = (current as Record<string, unknown>)[segment]
  }
  return current
}

/**
 * Build the section's draft source: a flat record keyed by each owned dotted
 * path, with values pulled from the nested config. The draft stays flat-keyed so
 * the section addresses fields by dotted path; the backend re-flattens on save.
 */
function pickPaths(
  configValues: Record<string, unknown>,
  paths: readonly string[],
): SectionDraft {
  const slice: SectionDraft = {}
  for (const path of paths) {
    slice[path] = getByPath(configValues, path)
  }
  return slice
}

/**
 * Shared shell for every settings section: derives a draft scoped to the
 * section's owned paths, registers a live dirty predicate with the overlay so
 * leaving prompts once, and renders a Save/Discard footer. Section components
 * supply only their fields through the render-prop child.
 */
export function SettingsSection({
  sectionId,
  ownedPaths,
  saveDisabled = false,
  children,
}: SettingsSectionProps) {
  const { configValues, schema, secretKeys, isLoading, saveConfig, registerDirtyGuard } =
    useSettingsSectionContext()
  const section = getSettingsSection(sectionId)

  const source = useMemo(
    () => pickPaths(configValues, ownedPaths),
    [configValues, ownedPaths],
  )

  const { draft, setField, dirty, saving, serverChanged, save, discard } =
    useDetailDraft<SectionDraft>({
      source,
      onSave: async (merged) => {
        const result = await saveConfig(merged)
        return result.ok
      },
    })

  // A ref keeps the registered predicate reading live dirty state without
  // re-registering on every keystroke. Updated in an effect (never during
  // render) so the predicate the overlay calls on exit reflects current state.
  const dirtyRef = useRef(dirty)
  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])

  useEffect(
    () => registerDirtyGuard(sectionId, () => dirtyRef.current),
    [registerDirtyGuard, sectionId],
  )

  const fields: SettingsSectionFields = {
    getValue: (path) => draft?.[path],
    setValue: (path, value) => setField(path, value),
    schema,
    secretKeys,
    isLoading,
  }

  const hasFields = ownedPaths.length > 0

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-7 py-6">
        <div className="flex flex-col gap-1.5 border-b border-border pb-5">
          <h3 className="text-lg font-semibold leading-[1.2] text-foreground">
            {section.label}
          </h3>
          <p className="max-w-[60ch] text-base leading-[1.5] text-muted-foreground">
            {section.description}
          </p>
        </div>
        <div className="flex flex-col gap-6">{children(fields)}</div>
      </div>
      {hasFields ? (
        <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border bg-surface-secondary px-5 py-3.5">
          {serverChanged ? (
            <span
              className="mr-auto max-w-[40ch] text-sm leading-[1.3] text-muted-foreground"
              role="status"
            >
              Changed elsewhere — saving overwrites the newer value.
            </span>
          ) : null}
          <DetailActionButton
            label="Discard"
            variant="ghost"
            onClick={discard}
            disabled={!dirty || saving}
          />
          <DetailActionButton
            label={saving ? 'Saving…' : 'Save'}
            variant="accent"
            onClick={() => {
              void save()
            }}
            disabled={!dirty || saving || saveDisabled}
          />
        </footer>
      ) : null}
    </div>
  )
}
