import { useState } from "react";
import type { ToolCall, ToolResult } from "../../types/chat";
import { cn } from "../../lib/utils";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { TOOL_CARD_SPACING } from "../shared/spacing";
import { getToolDisplayName } from "./ToolCallCard.helpers";
import { ToolArgumentsContent } from "./ToolCallCardContent";

interface AskUserOption {
  label: string;
  description: string;
}

interface AskUserQuestionItem {
  question: string;
  header: string;
  options: AskUserOption[];
  multiSelect: boolean;
}

export function ToolApprovalCard({
  call,
  onRespondToApproval,
}: {
  call: ToolCall;
  onRespondToApproval?: (
    toolCallId: string,
    decision: "approve" | "reject" | "approve_always",
  ) => boolean | void;
}): JSX.Element {
  const displayName = getToolDisplayName(call);
  const isLive = onRespondToApproval && call.status === "pending_approval";
  const [decided, setDecided] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  const handleDecision = (
    decision: "approve" | "reject" | "approve_always",
  ): void => {
    if (decided) return;
    const sent = onRespondToApproval?.(call.id, decision);
    if (sent === false) {
      setSendError("Disconnected — reconnecting...");
    } else {
      setSendError(null);
      setDecided(true);
    }
  };

  if (!isLive) {
    const wasApproved = call.status === "completed";
    const wasError = call.status === "error";
    return (
      <div className="my-1.5 overflow-hidden rounded-lg border border-border/30 bg-muted/5 opacity-75">
        <div className={cn(TOOL_CARD_SPACING.headerDense, "text-sm")}>
          <span className="font-mono text-foreground">{displayName}</span>
          {wasApproved && <Badge variant="success">Approved</Badge>}
          {wasError && <Badge variant="error">Rejected</Badge>}
          {!wasApproved && !wasError && (
            <Badge variant="warning">Pending</Badge>
          )}
        </div>
        {call.arguments && Object.keys(call.arguments).length > 0 && (
          <div className={cn(TOOL_CARD_SPACING.bodyCompact, "text-xs")}>
            <ToolArgumentsContent args={call.arguments} callId={call.id} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="my-1.5 overflow-hidden rounded-lg border border-warning-foreground/30 bg-warning/20">
      <div className={cn(TOOL_CARD_SPACING.headerDense, "text-sm")}>
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          className="shrink-0 text-warning-foreground"
        >
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        <span className="font-mono text-foreground">{displayName}</span>
        <Badge variant="warning">Approval Required</Badge>
      </div>
      {call.arguments && Object.keys(call.arguments).length > 0 && (
        <div className={cn(TOOL_CARD_SPACING.bodyCompact, "text-xs")}>
          <ToolArgumentsContent args={call.arguments} callId={call.id} />
        </div>
      )}
      <div className="flex items-center gap-2 px-3 pb-2">
        <Button
          size="sm"
          variant="accent"
          onClick={() => handleDecision("approve")}
          disabled={decided}
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => handleDecision("approve_always")}
          disabled={decided}
        >
          Always Approve
        </Button>
        <Button
          size="sm"
          variant="destructive"
          onClick={() => handleDecision("reject")}
          disabled={decided}
        >
          Reject
        </Button>
      </div>
      {sendError && (
        <div
          className={cn(
            TOOL_CARD_SPACING.bodyCompact,
            "text-xs text-warning-foreground",
          )}
        >
          {sendError}
        </div>
      )}
    </div>
  );
}

function normalizeAnsweredValues(
  value: unknown,
): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;

  const answers: Record<string, string> = {};
  for (const [question, answer] of Object.entries(value)) {
    if (typeof answer === "string") {
      answers[question] = answer;
      continue;
    }

    const serialized =
      Array.isArray(answer) && answer.every((item) => typeof item === "string")
        ? answer.join(", ")
        : JSON.stringify(answer);
    if (serialized !== undefined) answers[question] = serialized;
  }
  return answers;
}

function parseAnsweredValues(
  result: ToolResult | undefined,
): Record<string, string> | null {
  if (!result?.content) return null;
  if (result.kind === "json") {
    if (typeof result.content !== "object" || Array.isArray(result.content))
      return null;
    const object = result.content as Record<string, unknown>;
    return normalizeAnsweredValues(object.answers ?? object);
  }
  if (result.kind === "text") {
    if (typeof result.content !== "string") return null;
    const text = result.content;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") {
        const answers = normalizeAnsweredValues(parsed.answers ?? parsed);
        if (answers) return answers;
      }
    } catch {
      // Fall back to treating content as a plain string.
    }
    if (text.trim()) return { _raw: text };
  }
  return null;
}

export function AskUserQuestionCard({
  call,
  onRespond,
}: {
  call: ToolCall;
  onRespond?: (
    toolCallId: string,
    answers: Record<string, string>,
  ) => boolean | void;
}): JSX.Element | null {
  const args = call.arguments as
    { questions?: AskUserQuestionItem[] } | undefined;
  const questions = args?.questions;
  const [selectedOptions, setSelectedOptions] = useState<
    Record<number, string[]>
  >({});
  const [otherTexts, setOtherTexts] = useState<Record<number, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  if (!questions || !Array.isArray(questions)) return null;

  const isLive = onRespond && call.status === "calling";
  if (!isLive) {
    const answered = parseAnsweredValues(call.result);
    return (
      <div className="my-1.5 overflow-hidden rounded-lg border border-border/30 bg-muted/5 p-3 opacity-75">
        {questions.map((question, questionIndex) => {
          const answer = answered?.[question.question];
          const answerLabels = answer ? answer.split(", ") : [];
          return (
            <div key={questionIndex} className="mb-3 last:mb-0">
              <div className="mb-1.5 flex items-center gap-2">
                <Badge variant="info">{question.header}</Badge>
                {answer ? (
                  <Badge variant="success">Answered</Badge>
                ) : (
                  <Badge variant="default">No response</Badge>
                )}
              </div>
              <div className="mb-2 text-sm text-foreground">
                {question.question}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {question.options.map((option, optionIndex) => {
                  const wasSelected = answerLabels.includes(option.label);
                  return (
                    <div
                      key={optionIndex}
                      className={cn(
                        "rounded-md border px-3 py-1.5 text-left text-sm",
                        wasSelected
                          ? "border-accent bg-accent/20 text-foreground"
                          : "border-border/50 text-muted-foreground/50",
                      )}
                    >
                      <div className="font-medium">{option.label}</div>
                    </div>
                  );
                })}
              </div>
              {answer &&
                !question.options.some((option) =>
                  answerLabels.includes(option.label),
                ) && (
                  <div className="mt-1.5 text-sm text-foreground italic">
                    &ldquo;{answer}&rdquo;
                  </div>
                )}
            </div>
          );
        })}
      </div>
    );
  }

  const handleOptionClick = (
    questionIndex: number,
    label: string,
    multiSelect: boolean,
  ): void => {
    if (submitted) return;
    setSelectedOptions((previous) => {
      const current = previous[questionIndex] || [];
      if (label === "__other__") {
        if (current.includes("__other__")) {
          return {
            ...previous,
            [questionIndex]: current.filter(
              (currentLabel) => currentLabel !== "__other__",
            ),
          };
        }
        return multiSelect
          ? { ...previous, [questionIndex]: [...current, "__other__"] }
          : { ...previous, [questionIndex]: ["__other__"] };
      }
      if (multiSelect) {
        return current.includes(label)
          ? {
              ...previous,
              [questionIndex]: current.filter(
                (currentLabel) => currentLabel !== label,
              ),
            }
          : {
              ...previous,
              [questionIndex]: [
                ...current.filter(
                  (currentLabel) => currentLabel !== "__other__",
                ),
                label,
              ],
            };
      }
      return { ...previous, [questionIndex]: [label] };
    });
  };

  const handleSubmit = (): void => {
    if (!onRespond || submitted) return;
    const hasEmptyOther = questions.some((_question, questionIndex) => {
      const selected = selectedOptions[questionIndex] || [];
      if (!selected.includes("__other__")) return false;
      return !(otherTexts[questionIndex] || "").trim();
    });
    if (hasEmptyOther) return;
    const answers: Record<string, string> = {};
    questions.forEach((question, questionIndex) => {
      const selected = selectedOptions[questionIndex] || [];
      if (selected.includes("__other__")) {
        const customValue = otherTexts[questionIndex] || "";
        answers[question.question] = question.multiSelect
          ? [
              ...selected.filter((label) => label !== "__other__"),
              ...(customValue ? [customValue] : []),
            ].join(", ")
          : customValue;
      } else if (selected.length > 0) {
        answers[question.question] = selected.join(", ");
      }
    });
    const sent = onRespond(call.id, answers);
    if (sent === false) {
      setSendError("Disconnected — reconnecting...");
    } else {
      setSendError(null);
      setSubmitted(true);
    }
  };

  const hasSelection = Object.values(selectedOptions).some(
    (selected) => selected.length > 0,
  );

  return (
    <div
      className={cn(
        "my-1.5 overflow-hidden rounded-lg border border-accent/30 bg-accent/5 p-3",
        submitted && "opacity-60",
      )}
    >
      {questions.map((question, questionIndex) => (
        <div key={questionIndex} className="mb-3 last:mb-0">
          <div className="mb-1.5 flex items-center gap-2">
            <Badge variant="info">{question.header}</Badge>
            {question.multiSelect && (
              <span className="text-xs text-muted-foreground">
                Select multiple
              </span>
            )}
          </div>
          <div className="mb-2 text-sm text-foreground">
            {question.question}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {question.options.map((option, optionIndex) => {
              const isSelected = (
                selectedOptions[questionIndex] || []
              ).includes(option.label);
              return (
                <Button
                  key={optionIndex}
                  type="button"
                  variant="ghost"
                  size="sm"
                  className={cn(
                    "justify-start rounded-md border px-3 py-1.5 text-left text-sm font-normal whitespace-normal transition-colors",
                    isSelected
                      ? "border-accent bg-accent/20 text-foreground"
                      : "border-border text-muted-foreground hover:bg-muted",
                  )}
                  aria-pressed={isSelected}
                  onClick={() =>
                    handleOptionClick(
                      questionIndex,
                      option.label,
                      question.multiSelect,
                    )
                  }
                  disabled={submitted}
                >
                  <div className="font-medium">{option.label}</div>
                  {option.description && (
                    <div className="text-xs opacity-75">
                      {option.description}
                    </div>
                  )}
                </Button>
              );
            })}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={cn(
                "rounded-md border px-3 py-1.5 text-sm transition-colors",
                (selectedOptions[questionIndex] || []).includes("__other__")
                  ? "border-accent bg-accent/20 text-foreground"
                  : "border-border text-muted-foreground hover:bg-muted",
              )}
              aria-pressed={(selectedOptions[questionIndex] || []).includes(
                "__other__",
              )}
              onClick={() =>
                handleOptionClick(
                  questionIndex,
                  "__other__",
                  question.multiSelect,
                )
              }
              disabled={submitted}
            >
              Other
            </Button>
          </div>
          {(selectedOptions[questionIndex] || []).includes("__other__") && (
            <Input
              wrapperClassName="mt-2"
              className="h-auto w-full rounded-md border border-border bg-transparent px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:ring-1 focus:ring-accent focus:outline-none"
              type="text"
              placeholder="Type your answer..."
              value={otherTexts[questionIndex] || ""}
              onChange={(event) =>
                setOtherTexts((previous) => ({
                  ...previous,
                  [questionIndex]: event.target.value,
                }))
              }
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  handleSubmit();
                }
              }}
              disabled={submitted}
            />
          )}
        </div>
      ))}
      {!submitted && hasSelection && (
        <Button
          size="sm"
          variant="accent"
          onClick={handleSubmit}
          className="mt-2"
        >
          Submit
        </Button>
      )}
      {sendError && (
        <div className="mt-1.5 text-xs text-warning-foreground">
          {sendError}
        </div>
      )}
    </div>
  );
}
