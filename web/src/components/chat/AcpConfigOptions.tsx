import type { AcpConfigOption } from "../../types/chat";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
} from "./ui/Select";

interface AcpConfigOptionsProps {
  disabled?: boolean;
  options: AcpConfigOption[];
  onChange: (configId: string, value: string) => void;
}

function optionLabel(option: AcpConfigOption): string {
  return option.options.find((value) => value.value === option.currentValue)?.name
    ?? option.currentValue;
}

export function AcpConfigOptions({
  disabled = false,
  options,
  onChange,
}: AcpConfigOptionsProps) {
  const selectOptions = options.filter((option) => option.type === "select");
  if (selectOptions.length === 0) return null;

  return (
    <div className="chat-input-model-controls" aria-label="ACP session options">
      {selectOptions.map((option) => (
        <Select
          key={option.id}
          value={option.currentValue}
          onValueChange={(value) => onChange(option.id, value)}
          disabled={disabled}
        >
          <SelectTrigger
            className="chat-input-select chat-input-select--acp-option !w-auto"
            aria-label={option.name}
            title={option.description ?? option.name}
          >
            <div className="chat-input-select__value">
              <span className="chat-input-select__text">
                {option.name}: {optionLabel(option)}
              </span>
            </div>
          </SelectTrigger>
          <SelectContent side="top" className="chat-input-select__content">
            <SelectGroup>
              <SelectLabel className="chat-input-select__label">
                {option.name}
              </SelectLabel>
              {option.options.map((value) => (
                <SelectItem key={value.value} value={value.value}>
                  <span title={value.description}>{value.name}</span>
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      ))}
    </div>
  );
}
