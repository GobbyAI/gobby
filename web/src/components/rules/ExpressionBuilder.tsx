import { useState, useEffect } from "react";
import { cn } from "../../lib/utils";

const VARIABLES = [
  "tool_name",
  "source",
  'tool_input.get("server_name")',
  'tool_input.get("tool_name")',
  'variables.get("claimed_tasks")',
  'variables.get("pre_existing_errors_triaged")',
];

const OPERATORS = ["==", "!=", "in", "not in"];

const TOGGLE_BTN_BASE_CLS =
  'cursor-pointer border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-0.5 text-[length:var(--text-xs)] font-medium text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11 pointer-coarse:px-3'
const TOGGLE_BTN_ACTIVE_CLS =
  'bg-[var(--bg-tertiary)] font-semibold text-[var(--text-primary)]'

const FIELD_INPUT_CLS =
  'min-w-0 rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none transition-colors duration-150 focus:border-[var(--accent)] focus:[box-shadow:0_0_0_2px_color-mix(in_srgb,var(--accent)_20%,transparent)] pointer-coarse:min-h-11'

interface ParsedExpression {
  variable: string;
  operator: string;
  operand: string;
}

function parseExpression(expr: string): ParsedExpression | null {
  const trimmed = expr.trim();
  if (!trimmed) return null;

  // Try "not in" first (two-word operator)
  const notInMatch = trimmed.match(
    /^(.+?)\s+not\s+in\s+(.+)$/,
  );
  if (notInMatch) {
    return {
      variable: notInMatch[1].trim(),
      operator: "not in",
      operand: notInMatch[2].trim(),
    };
  }

  // Try "in"
  const inMatch = trimmed.match(/^(.+?)\s+in\s+(.+)$/);
  if (inMatch) {
    return {
      variable: inMatch[1].trim(),
      operator: "in",
      operand: inMatch[2].trim(),
    };
  }

  // Try == and !=
  const cmpMatch = trimmed.match(/^(.+?)\s*(==|!=)\s*(.+)$/);
  if (cmpMatch) {
    return {
      variable: cmpMatch[1].trim(),
      operator: cmpMatch[2],
      operand: cmpMatch[3].trim(),
    };
  }

  return null;
}

function buildExpression(
  variable: string,
  operator: string,
  operand: string,
): string {
  if (!variable || !operand) return "";
  return `${variable} ${operator} ${operand}`;
}

/** Unquote a string value for display in the input field. */
function unquote(s: string): string {
  if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
    return s.slice(1, -1);
  }
  return s;
}

const SPECIAL_LITERALS = new Set(["True", "False", "None"]);
const NUMBER_RE = /^\d+(\.\d+)?$/;
const IDENT_OR_PATH_RE = /^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$/;
const FUNCTION_CALL_RE = /^[A-Za-z_]\w*\(.*\)$/;

/** Quote a value for the expression string if it looks like a plain string. */
function smartQuote(s: string): string {
  const trimmed = s.trim();
  if (!trimmed) return '""';
  // Already quoted
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed;
  }
  // Looks like a list, boolean, number, dotted variable ref, or function call — don't quote
  if (trimmed.startsWith("[") || SPECIAL_LITERALS.has(trimmed) || NUMBER_RE.test(trimmed) || IDENT_OR_PATH_RE.test(trimmed) || FUNCTION_CALL_RE.test(trimmed)) {
    return trimmed;
  }
  return `"${trimmed}"`;
}

interface ExpressionBuilderProps {
  value: string;
  onChange: (value: string) => void;
}

export function ExpressionBuilder({ value, onChange }: ExpressionBuilderProps) {
  const parsed = parseExpression(value);
  const canBuild = value === "" || parsed !== null;

  const [mode, setMode] = useState<"builder" | "raw">(canBuild ? "builder" : "raw");
  const [variable, setVariable] = useState(parsed?.variable ?? "");
  const [operator, setOperator] = useState(parsed?.operator ?? "==");
  const [operand, setOperand] = useState(parsed ? unquote(parsed.operand) : "");

  // When the external value changes (e.g. switching rules), re-sync builder state
  useEffect(() => {
    const p = parseExpression(value);
    if (p) {
      setVariable(p.variable);
      setOperator(p.operator);
      setOperand(unquote(p.operand));
      if (mode === "raw" && value === "") setMode("builder");
    } else if (value === "") {
      setVariable("");
      setOperator("==");
      setOperand("");
      setMode("builder");
    } else {
      setMode("raw");
    }
  // `mode` intentionally omitted — including it would re-trigger builder/raw
  // switching whenever the external `value` syncs, causing unwanted mode flips.
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleBuilderChange(v: string, op: string, opd: string) {
    setVariable(v);
    setOperator(op);
    setOperand(opd);
    const expr = buildExpression(v, op, smartQuote(opd));
    onChange(expr);
  }

  const switchMode = (newMode: "builder" | "raw") => {
    if (newMode === "builder") {
      const p = parseExpression(value);
      if (!p && value.trim()) return; // can't switch to builder for complex expr
      if (p) {
        setVariable(p.variable);
        setOperator(p.operator);
        setOperand(unquote(p.operand));
      }
    }
    setMode(newMode);
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex self-end">
        <button
          type="button"
          className={cn(
            TOGGLE_BTN_BASE_CLS,
            'rounded-l-md',
            mode === "builder" && TOGGLE_BTN_ACTIVE_CLS,
          )}
          onClick={() => switchMode("builder")}
          disabled={mode === "raw" && !canBuild}
          aria-pressed={mode === "builder"}
        >
          Builder
        </button>
        <button
          type="button"
          className={cn(
            TOGGLE_BTN_BASE_CLS,
            'rounded-r-md border-l-0',
            mode === "raw" && TOGGLE_BTN_ACTIVE_CLS,
          )}
          onClick={() => switchMode("raw")}
          aria-pressed={mode === "raw"}
        >
          Raw
        </button>
      </div>

      {mode === "builder" ? (
        <div className="flex items-center gap-1.5">
          <select
            aria-label="Select variable"
            className={cn(FIELD_INPUT_CLS, 'flex-[1_1_40%]')}
            value={variable}
            onChange={(e) =>
              handleBuilderChange(e.target.value, operator, operand)
            }
          >
            <option value="">variable...</option>
            {VARIABLES.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
            {variable && !VARIABLES.includes(variable) && (
              <option value={variable}>{variable}</option>
            )}
          </select>
          <select
            aria-label="Select operator"
            className={cn(FIELD_INPUT_CLS, 'flex-[0_0_auto] min-w-[80px]')}
            value={operator}
            onChange={(e) =>
              handleBuilderChange(variable, e.target.value, operand)
            }
          >
            {OPERATORS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
          <input
            aria-label="Operand value"
            className={cn(FIELD_INPUT_CLS, 'flex-1')}
            value={operand}
            onChange={(e) =>
              handleBuilderChange(variable, operator, e.target.value)
            }
            placeholder="value"
          />
        </div>
      ) : (
        <>
          <input
            className={cn(FIELD_INPUT_CLS, 'w-full text-[length:var(--text-sm)]')}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder='e.g. tool_name == "Edit"'
          />
          {!canBuild && value.trim() && (
            <span className="text-[length:var(--text-xs)] italic text-[var(--text-muted)]">
              Complex expression — edit in raw mode
            </span>
          )}
        </>
      )}
    </div>
  );
}
