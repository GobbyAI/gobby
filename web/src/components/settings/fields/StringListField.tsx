import { Button } from "../../ui/Button";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";

export interface StringListFieldProps {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
  ariaLabel: string;
  disabled?: boolean;
  placeholder?: string;
  addLabel?: string;
}

/**
 * Ordered editable list of strings — the primitive for the audit's
 * `array<string>` "fix" rows (cors_origins, candidates, path lists, …)
 * that today fall back to a single free-text input.
 */
export function StringListField({
  label,
  value,
  onChange,
  ariaLabel,
  disabled,
  placeholder,
  addLabel = "Add item",
}: StringListFieldProps) {
  function updateItem(index: number, next: string) {
    onChange(
      value.map((item, itemIndex) => (itemIndex === index ? next : item)),
    );
  }

  function removeItem(index: number) {
    onChange(value.filter((_, itemIndex) => itemIndex !== index));
  }

  function addItem() {
    onChange([...value, ""]);
  }

  return (
    <FormField label={label} group>
      {() => (
        <>
          {value.length > 0 ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {value.map((item, index) => (
                <li key={index} className="flex items-center gap-2">
                  <Input
                    type="text"
                    value={item}
                    disabled={disabled}
                    placeholder={placeholder}
                    aria-label={`${ariaLabel} item ${index + 1}`}
                    onChange={(event) => updateItem(index, event.target.value)}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="shrink-0"
                    disabled={disabled}
                    aria-label={`Remove ${ariaLabel} item ${index + 1}`}
                    onClick={() => removeItem(index)}
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
            <Button
              type="button"
              size="sm"
              disabled={disabled}
              onClick={addItem}
            >
              {addLabel}
            </Button>
          </div>
        </>
      )}
    </FormField>
  );
}
