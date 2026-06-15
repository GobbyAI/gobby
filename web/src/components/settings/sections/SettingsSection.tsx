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
  children: (fields: SettingsSectionFields) => ReactNode
}

type SectionDraft = Record<string, unknown>

function pickPaths(
  configValues: Record<string, unknown>,
  paths: readonly string[],
): SectionDraft {
  const slice: SectionDraft = {}
  for (const path of paths) {
    slice[path] = configValues[path]
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
    <div className="settings-section">
      <div className="settings-section__scroll">
        <div className="settings-section__head">
          <h3 className="settings-section__title">{section.label}</h3>
          <p className="settings-section__desc">{section.description}</p>
        </div>
        <div className="settings-section__body">{children(fields)}</div>
      </div>
      {hasFields ? (
        <footer className="settings-section__footer">
          {serverChanged ? (
            <span className="settings-section__server-changed" role="status">
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
            disabled={!dirty || saving}
          />
        </footer>
      ) : null}
    </div>
  )
}
