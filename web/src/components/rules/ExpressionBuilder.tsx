import { useState, useEffect, useCallback } from "react";
import "./ExpressionBuilder.css";

const VARIABLES = [
  "tool_name",
  "source",
  'tool_input.get("server_name")',
  'tool_input.get("tool_name")',
  'variables.get("claimed_tasks")',
  'variables.get("pre_existing_errors_triaged")',
];

const OPERATORS = ["==", "!=", "in", "not in"];

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

  const handleBuilderChange = useCallback(
    (v: string, op: string, opd: string) => {
      setVariable(v);
      setOperator(op);
      setOperand(opd);
      const expr = buildExpression(v, op, smartQuote(opd));
      onChange(expr);
    },
    [onChange],
  );

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
    <div className="expr-builder">
      <div className="expr-builder-toggle">
        <button
          type="button"
          className={`expr-builder-toggle-btn ${mode === "builder" ? "expr-builder-toggle-btn--active" : ""}`}
          onClick={() => switchMode("builder")}
          disabled={mode === "raw" && !canBuild}
          aria-pressed={mode === "builder"}
        >
          Builder
        </button>
        <button
          type="button"
          className={`expr-builder-toggle-btn ${mode === "raw" ? "expr-builder-toggle-btn--active" : ""}`}
          onClick={() => switchMode("raw")}
          aria-pressed={mode === "raw"}
        >
          Raw
        </button>
      </div>

      {mode === "builder" ? (
        <div className="expr-builder-row">
          <select
            aria-label="Select variable"
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
            className="rule-edit-input rule-edit-mono"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder='e.g. tool_name == "Edit"'
          />
          {!canBuild && value.trim() && (
            <span className="expr-builder-hint">
              Complex expression — edit in raw mode
            </span>
          )}
        </>
      )}
    </div>
  );
}
