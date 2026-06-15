import { useEffect, useMemo, useState } from 'react'
import { useWorkflows, type WorkflowDetail } from '../../hooks/useWorkflows'
import { Switch } from '../ui/Switch'
import { TextField } from '../activity/fields'
import { parseVariableInput, variableDisplayValue } from './workflowVariables'

/**
 * Live editor for workflow variable defaults (`/api/workflows?workflow_type=
 * variable`), re-homed from the legacy configuration page so it survives that
 * page's deletion. Unlike the draft-backed config rows in the section, variable
 * add / toggle / delete write immediately through `useWorkflows`. Bundled
 * `template` variables are read-only; variables you add (`source: installed`)
 * can be toggled or deleted. The value-parsing helpers live in
 * `./workflowVariables` so this file can export only components.
 */

export function WorkflowVariablesEditor() {
  const {
    workflows,
    isLoading,
    fetchWorkflows,
    createWorkflow,
    toggleEnabled,
    deleteWorkflow,
  } = useWorkflows()
  const [showForm, setShowForm] = useState(false)
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => {
    void fetchWorkflows({ workflow_type: 'variable' })
  }, [fetchWorkflows])

  const variables = useMemo(
    () => workflows.filter((workflow) => workflow.workflow_type === 'variable'),
    [workflows],
  )

  function resetForm() {
    setName('')
    setValue('')
    setDescription('')
    setShowForm(false)
  }

  async function handleCreate() {
    const trimmedName = name.trim()
    if (!trimmedName) return
    const trimmedDescription = description.trim()
    const definitionJson = JSON.stringify({
      variable: trimmedName,
      value: parseVariableInput(value),
      description: trimmedDescription || undefined,
    })
    const created = await createWorkflow({
      name: trimmedName,
      definition_json: definitionJson,
      workflow_type: 'variable',
      description: trimmedDescription || undefined,
      enabled: true,
    })
    if (created) resetForm()
  }

  function handleDelete(variable: WorkflowDetail) {
    if (!window.confirm(`Delete variable "${variable.name}"?`)) return
    void deleteWorkflow(variable.id)
  }

  return (
    <div className="settings-variables">
      <div className="settings-variables__head">
        <span className="settings-field__label">Variable defaults</span>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          aria-expanded={showForm}
          onClick={() => setShowForm((open) => !open)}
        >
          Add variable
        </button>
      </div>
      <p className="settings-field__hint">
        Default session variable values. Bundled template variables ship with
        Gobby; variables you add can be toggled or deleted.
      </p>

      {showForm ? (
        <div className="settings-variables__form">
          <TextField
            label="Variable name"
            ariaLabel="Variable name"
            value={name}
            placeholder="my_custom_var"
            onChange={setName}
          />
          <TextField
            label="Default value"
            ariaLabel="Default value"
            value={value}
            placeholder="true, 42, or text"
            onChange={setValue}
          />
          <TextField
            label="Description"
            ariaLabel="Variable description"
            value={description}
            placeholder="Optional"
            onChange={setDescription}
          />
          <div className="settings-variables__form-actions">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={resetForm}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => void handleCreate()}
            >
              Save variable
            </button>
          </div>
        </div>
      ) : null}

      {isLoading ? (
        <p className="settings-field__empty">Loading variables…</p>
      ) : variables.length === 0 ? (
        <p className="settings-field__empty">No variable defaults yet.</p>
      ) : (
        <ul className="settings-typed-list__items">
          {variables.map((variable) => (
            <li
              key={variable.id}
              className="settings-typed-list__item settings-variables__row"
            >
              <div className="settings-typed-list__item-head">
                <code className="settings-variables__name">{variable.name}</code>
                <div className="settings-variables__controls">
                  <span className="settings-variables__source">
                    {variable.source}
                  </span>
                  <Switch
                    checked={variable.enabled}
                    aria-label={`Toggle ${variable.name}`}
                    onChange={() => void toggleEnabled(variable.id)}
                  />
                  {variable.source !== 'template' ? (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      aria-label={`Delete ${variable.name}`}
                      onClick={() => handleDelete(variable)}
                    >
                      Delete
                    </button>
                  ) : null}
                </div>
              </div>
              <div className="settings-variables__meta">
                <code className="settings-variables__value">
                  {variableDisplayValue(variable.definition_json)}
                </code>
                {variable.description ? (
                  <span className="settings-variables__desc">
                    {variable.description}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default WorkflowVariablesEditor
