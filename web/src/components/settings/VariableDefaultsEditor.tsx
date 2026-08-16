import { useEffect, useState } from "react";
import { useVariableDefs, type VariableDef } from "../../hooks/useVariableDefs";
import { Button } from "../ui/Button";
import { Switch } from "../ui/Switch";
import { TextField } from "../activity/fields";
import { parseVariableInput, variableDisplayValue } from "./workflowVariables";

/**
 * Live editor for session variable defaults (`/api/variables`). Add / toggle /
 * delete write immediately through `useVariableDefs`. Bundled `template`
 * variables are read-only; variables you add can be toggled or deleted.
 */

export function VariableDefaultsEditor() {
  const {
    variables,
    isLoading,
    fetchVariables,
    createVariable,
    toggleEnabled,
    deleteVariable,
  } = useVariableDefs();
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void fetchVariables().then((ok) => {
      if (active && !ok) setError("Could not load variable defaults.");
    });
    return () => {
      active = false;
    };
  }, [fetchVariables]);

  function resetForm() {
    setName("");
    setValue("");
    setDescription("");
    setShowForm(false);
  }

  async function handleCreate() {
    const trimmedName = name.trim();
    if (!trimmedName) return;
    const trimmedDescription = description.trim();
    setError(null);
    const created = await createVariable({
      name: trimmedName,
      value: parseVariableInput(value),
      description: trimmedDescription || undefined,
      enabled: true,
    });
    if (created) {
      resetForm();
    } else {
      setError("Could not save the variable.");
    }
  }

  async function handleToggle(variable: VariableDef) {
    setError(null);
    const updated = await toggleEnabled(variable.id);
    if (!updated) setError(`Could not update "${variable.name}".`);
  }

  async function handleDelete(variable: VariableDef) {
    if (!window.confirm(`Delete variable "${variable.name}"?`)) return;
    setError(null);
    const deleted = await deleteVariable(variable.id);
    if (!deleted) setError(`Could not delete "${variable.name}".`);
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-base leading-[1.3] font-medium text-foreground">
          Variable defaults
        </span>
        <Button
          type="button"
          size="sm"
          aria-expanded={showForm}
          onClick={() => setShowForm((open) => !open)}
        >
          Add variable
        </Button>
      </div>
      <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
        Default session variable values. Bundled template variables ship with
        Gobby; variables you add can be toggled or deleted.
      </p>

      {showForm ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-muted p-3.5">
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
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={resetForm}>
              Cancel
            </Button>
            <Button type="button" size="sm" onClick={() => void handleCreate()}>
              Save variable
            </Button>
          </div>
        </div>
      ) : null}

      {error ? (
        <p
          className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground"
          role="alert"
        >
          {error}
        </p>
      ) : null}

      {isLoading ? (
        <p className="text-sm leading-[1.4] text-foreground-muted">
          Loading variables…
        </p>
      ) : error && variables.length === 0 ? null : variables.length === 0 ? (
        <p className="text-sm leading-[1.4] text-foreground-muted">
          No variable defaults yet.
        </p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {variables.map((variable) => (
            <li
              key={variable.id}
              className="flex flex-col gap-3 rounded-lg border border-border bg-muted px-3.5 py-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <code className="min-w-0 text-base leading-[1.6] font-medium break-all text-foreground">
                  {variable.name}
                </code>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="rounded-md border border-border bg-surface-secondary px-2 py-0.5 text-sm leading-[1.6] text-muted-foreground lowercase">
                    {variable.source}
                  </span>
                  <Switch
                    checked={variable.enabled}
                    aria-label={`Toggle ${variable.name}`}
                    onChange={() => void handleToggle(variable)}
                  />
                  {variable.source !== "template" ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      aria-label={`Delete ${variable.name}`}
                      onClick={() => void handleDelete(variable)}
                    >
                      Delete
                    </Button>
                  ) : null}
                </div>
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <code className="text-sm leading-[1.6] break-all text-muted-foreground">
                  {variableDisplayValue(variable.default_value)}
                </code>
                {variable.description ? (
                  <span className="text-sm leading-[1.6] text-foreground-muted">
                    {variable.description}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default VariableDefaultsEditor;
