import { useId, useMemo, useState } from 'react'
import {
  DetailActionButton,
  SecretField,
  SelectField,
  TextField,
} from '../../activity/fields'
import type { FieldOption } from '../../activity/fields'
import { Button } from '../../ui/Button'
import { SecretConfigField, Subsection } from './configFields'
import { SettingsSection } from './SettingsSection'
import type { SettingsSectionFields } from './SettingsSection'
import { useSettingsSectionContext } from './SettingsSectionContext'
import type { SecretInfo } from '../../../hooks/useConfiguration'

/**
 * Secrets & Auth settings section (audit IA section 13.2). Two surfaces live
 * here: the draft-backed service credentials (`ai.embeddings.api_key`,
 * `databases.qdrant.api_key`, `databases.falkordb.password`) saved through the
 * #17108 section footer with the rows masked via SecretConfigField; and the
 * `$secret:` store, a standalone `/api/config/secrets` CRUD surface threaded
 * through context like the rules-enforcement toggle and saved immediately by
 * its own controls rather than the config draft.
 *
 * Web-UI auth credentials are deliberately absent: `auth.*` is not a
 * registered runtime config surface, so a draft editor here could never
 * persist anything. Password resets stay CLI-only until #19650 lands an auth
 * management surface.
 */

const OWNED_PATHS: readonly string[] = [
  'ai.embeddings.api_key',
  'databases.qdrant.api_key',
  'databases.falkordb.password',
]

const DEFAULT_CATEGORY = 'general'

// Always offer `general`, then any server-known categories, de-duplicated and
// order-stable so the new-secret select never renders an empty option set.
function categoryOptions(categories: string[]): FieldOption[] {
  const seen = new Set<string>()
  const options: FieldOption[] = []
  for (const category of [DEFAULT_CATEGORY, ...categories]) {
    if (!category || seen.has(category)) continue
    seen.add(category)
    options.push({ value: category, label: category })
  }
  return options
}

function ServiceCredentialsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Service credentials"
      hint="API keys and passwords for the embedding and database backends. Stored encrypted and shown masked once set; reveal to confirm a freshly typed value."
    >
      <SecretConfigField
        fields={fields}
        path="ai.embeddings.api_key"
        label="Embeddings API key"
        ariaLabel="Embeddings API key"
        placeholder="Inherit from the embedding provider"
        nullable
      />
      <SecretConfigField
        fields={fields}
        path="databases.qdrant.api_key"
        label="Qdrant API key"
        ariaLabel="Qdrant API key"
        placeholder="Optional for local access"
        nullable
      />
      <SecretConfigField
        fields={fields}
        path="databases.falkordb.password"
        label="FalkorDB password"
        ariaLabel="FalkorDB password"
        placeholder="Optional Redis AUTH password"
        nullable
      />
    </Subsection>
  )
}

interface SecretStoreGroupProps {
  secrets: SecretInfo[]
  categories: string[]
  onSave?: (
    name: string,
    value: string,
    category?: string,
    description?: string,
  ) => Promise<boolean>
  onDelete?: (name: string) => Promise<boolean>
}

function SecretStoreGroup({
  secrets,
  categories,
  onSave,
  onDelete,
}: SecretStoreGroupProps) {
  const labelId = useId()
  const [open, setOpen] = useState(false)
  const [editingName, setEditingName] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [secretValue, setSecretValue] = useState('')
  const [category, setCategory] = useState(DEFAULT_CATEGORY)
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const options = useMemo(() => categoryOptions(categories), [categories])

  // The store is a standalone surface; without its write handlers there is
  // nothing actionable to render, so degrade to a notice (the auth and service
  // credential groups above still work).
  if (!onSave || !onDelete) {
    return (
      <Subsection
        title="Secret store"
        hint="Reusable $secret: references for MCP headers and environment variables."
      >
        <p className="settings-section__pending">
          The secret store is unavailable.
        </p>
      </Subsection>
    )
  }

  const resetForm = () => {
    setOpen(false)
    setEditingName(null)
    setName('')
    setSecretValue('')
    setCategory(DEFAULT_CATEGORY)
    setDescription('')
    setError(null)
  }

  const openAdd = () => {
    resetForm()
    setOpen(true)
  }

  const openEdit = (secret: SecretInfo) => {
    setEditingName(secret.name)
    setName(secret.name)
    setSecretValue('')
    setCategory(secret.category || DEFAULT_CATEGORY)
    setDescription(secret.description ?? '')
    setError(null)
    setOpen(true)
  }

  const canSubmit = name.trim() !== '' && secretValue.trim() !== ''

  const handleSubmit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      const ok = await onSave(
        name.trim(),
        secretValue,
        category,
        description.trim() || undefined,
      )
      if (ok) resetForm()
      else setError('Could not save the secret.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the secret.')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (secretName: string) => {
    if (
      !window.confirm(`Delete secret "${secretName}"? This cannot be undone.`)
    ) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      const ok = await onDelete(secretName)
      if (!ok) setError('Could not delete the secret.')
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'Could not delete the secret.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Subsection
      title="Secret store"
      hint="Reusable $secret:NAME references for MCP headers and environment variables. The daemon resolves them at connection time; agents never see raw values. Saved immediately, separate from the config draft above."
    >
      <div
        className="settings-field settings-typed-list"
        role="group"
        aria-labelledby={labelId}
      >
        <span id={labelId} className="settings-field__label">
          Stored secrets
        </span>
        {secrets.length > 0 ? (
          <ul className="settings-typed-list__items">
            {secrets.map((secret) => (
              <li key={secret.id} className="settings-typed-list__item">
                <div className="settings-typed-list__item-head">
                  <span className="settings-typed-list__item-title">
                    <code>{secret.name}</code>
                  </span>
                  <div className="settings-field__actions">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      aria-label={`Edit secret ${secret.name}`}
                      onClick={() => openEdit(secret)}
                    >
                      Edit
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      aria-label={`Delete secret ${secret.name}`}
                      onClick={() => {
                        void handleDelete(secret.name)
                      }}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
                <p className="settings-field__hint">
                  {secret.category}
                  {secret.description ? ` · ${secret.description}` : ''} · value
                  hidden
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="settings-field__empty">
            No secrets stored yet. Add API keys and sensitive values here.
          </p>
        )}
      </div>

      {open ? (
        <div className="settings-typed-list__item">
          <TextField
            label="Name"
            ariaLabel="Secret name"
            value={name}
            placeholder="e.g. anthropic_key"
            disabled={editingName !== null || busy}
            onChange={setName}
          />
          <SelectField
            label="Category"
            ariaLabel="Secret category"
            value={category}
            options={options}
            disabled={busy}
            onChange={setCategory}
          />
          <SecretField
            label="Value"
            ariaLabel="Secret value"
            value={secretValue}
            placeholder={editingName ? 'Enter a new value' : 'Secret value'}
            disabled={busy}
            onChange={setSecretValue}
          />
          <TextField
            label="Description"
            ariaLabel="Secret description"
            value={description}
            placeholder="Optional"
            disabled={busy}
            onChange={setDescription}
          />
          <div className="settings-field__actions">
            <DetailActionButton
              label="Cancel"
              variant="ghost"
              onClick={resetForm}
              disabled={busy}
            />
            <DetailActionButton
              label={
                busy
                  ? 'Saving…'
                  : editingName
                    ? 'Update secret'
                    : 'Save secret'
              }
              variant="accent"
              onClick={() => {
                void handleSubmit()
              }}
              disabled={busy || !canSubmit}
            />
          </div>
        </div>
      ) : (
        <div className="settings-field__actions">
          <DetailActionButton
            label="Add secret"
            variant="secondary"
            onClick={openAdd}
            disabled={busy}
          />
        </div>
      )}

      {error ? (
        <p className="settings-field__hint" role="alert">
          {error}
        </p>
      ) : null}
    </Subsection>
  )
}

export function SecretsAuthSection() {
  const { secrets, secretCategories, saveSecret, deleteSecret } =
    useSettingsSectionContext()
  return (
    <SettingsSection sectionId="secrets-auth" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <ServiceCredentialsGroup fields={fields} />
          <SecretStoreGroup
            secrets={secrets ?? []}
            categories={secretCategories ?? []}
            onSave={saveSecret}
            onDelete={deleteSecret}
          />
        </>
      )}
    </SettingsSection>
  )
}
