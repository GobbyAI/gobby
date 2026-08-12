import { useMemo } from "react";
import type { ToolCall } from "../../types/chat";
import { cn } from "../../lib/utils";
import { CodeBlock } from "../shared/CodeBlock";
import { DiffBlock } from "../shared/DiffBlock";
import { computeSyntheticDiffLines } from "../shared/DiffBlock.helpers";
import { MarkdownBody } from "../shared/MarkdownBody";
import { TOOL_CARD_SPACING } from "../shared/spacing";
import { JsonBlock } from "./JsonBlock";
import {
  extractBase64Image,
  extractResultContent,
  extractResultMetadata,
  extractShellOutputContent,
  formatToolName,
  getLanguageFromPath,
  parseGrepOutput,
  parseReadOutput,
  pathBasename,
  resolveToolType,
  unwrapMcpResultEnvelope,
} from "./ToolCallCard.helpers";
import {
  TOOL_ERROR_PRE_CLASS,
  TOOL_RESULT_CUSTOM_STYLE,
} from "./ToolCallCard.styles";
import {
  JsonResultBlock,
  MetadataStrip,
  ToolResultBody,
} from "./ToolResultBlocks";
import { ToolResultImage } from "./ToolResultImage";

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function ToolArgumentsContent({
  args,
  callId,
}: {
  args: Record<string, unknown>;
  callId: string;
}): JSX.Element {
  const filePath = stringValue(args.file_path);

  if (filePath && typeof args.content === "string") {
    const language = getLanguageFromPath(filePath);
    return (
      <div>
        <div className={cn("text-muted-foreground", TOOL_CARD_SPACING.label)}>
          Write <span className="font-mono text-foreground">{filePath}</span>
        </div>
        <CodeBlock
          language={language}
          startingLineNumber={1}
          className="tool-code-surface"
          customStyle={TOOL_RESULT_CUSTOM_STYLE}
        >
          {args.content as string}
        </CodeBlock>
      </div>
    );
  }

  if (
    filePath &&
    typeof args.old_string === "string" &&
    typeof args.new_string === "string"
  ) {
    const language = getLanguageFromPath(filePath);
    return (
      <div>
        <div className={cn("text-muted-foreground", TOOL_CARD_SPACING.label)}>
          Edit <span className="font-mono text-foreground">{filePath}</span>
        </div>
        <DiffBlock
          lines={computeSyntheticDiffLines(
            args.old_string as string,
            args.new_string as string,
          )}
          language={language}
          className="tool-code-surface"
        />
      </div>
    );
  }

  if (typeof args.plan === "string") {
    const title =
      typeof args.title === "string" && args.title.trim() ? args.title : "Plan";
    return (
      <div>
        <div className={cn("text-muted-foreground", TOOL_CARD_SPACING.label)}>
          {title}
        </div>
        <MarkdownBody id={`tool-plan-${callId}`} content={args.plan} />
      </div>
    );
  }

  return (
    <div>
      <div className={cn("text-muted-foreground", TOOL_CARD_SPACING.label)}>
        Arguments
      </div>
      <JsonBlock
        value={args}
        className="tool-code-surface max-h-96 rounded"
        testId="toolcall-json"
      />
    </div>
  );
}

export function ToolErrorBody({ error }: { error: string }): JSX.Element {
  const cleaned = error.replace(/<\/?tool_use_error>/g, "").trim();
  const looksLikeJson = cleaned.startsWith("{") || cleaned.startsWith("[");
  return (
    <div>
      <div
        className={cn("text-destructive-foreground", TOOL_CARD_SPACING.label)}
      >
        Error
      </div>
      {looksLikeJson ? (
        <JsonResultBlock value={cleaned} variant="error" />
      ) : (
        <pre className={TOOL_ERROR_PRE_CLASS}>{cleaned}</pre>
      )}
    </div>
  );
}

function formatToolLocation(location: Record<string, unknown>): string {
  const uri =
    stringValue(location.uri) ||
    stringValue(location.path) ||
    stringValue(location.file);
  const line = numberValue(location.line) ?? numberValue(location.startLine);
  const column =
    numberValue(location.column) ?? numberValue(location.startColumn);
  const suffix = [line != null ? line : null, column != null ? column : null]
    .filter((value) => value != null)
    .join(":");
  if (uri && suffix) return `${uri}:${suffix}`;
  if (uri) return uri;
  return JSON.stringify(location);
}

export function ToolLocations({
  callId,
  locations,
}: {
  callId: string;
  locations: Record<string, unknown>[];
}): JSX.Element {
  return (
    <div>
      <div className={cn("text-muted-foreground", TOOL_CARD_SPACING.label)}>
        Locations
      </div>
      <div className="space-y-1 font-mono text-muted-foreground">
        {locations.map((location, index) => (
          <div key={`${callId}-loc-${index}`} className="truncate">
            {formatToolLocation(location)}
          </div>
        ))}
      </div>
    </div>
  );
}

export function ToolResultContent({ call }: { call: ToolCall }): JSX.Element {
  const toolType = resolveToolType(call);
  const extractedContent = extractResultContent(call.result);
  const rawContent =
    toolType === "bash"
      ? extractShellOutputContent(extractedContent)
      : extractedContent;
  const metadata = extractResultMetadata(call.result);

  const imageSrc = useMemo(() => extractBase64Image(rawContent), [rawContent]);

  const resultStr = useMemo(() => {
    try {
      if (typeof rawContent === "string") {
        try {
          return JSON.stringify(JSON.parse(rawContent), null, 2);
        } catch {
          return rawContent;
        }
      }
      return JSON.stringify(rawContent, null, 2);
    } catch (error) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to serialize tool call result:", error);
      }
      return String(rawContent);
    }
  }, [rawContent]);
  const filePath = stringValue(call.arguments?.file_path);

  if (imageSrc) return <ToolResultImage src={imageSrc} />;

  if (filePath) {
    const parsed = parseReadOutput(resultStr);
    if (parsed) {
      const language = getLanguageFromPath(filePath);
      const fileName = pathBasename(filePath);
      const lineCount = metadata?.line_count as number | undefined;
      return (
        <div className="overflow-hidden rounded">
          <div className="flex items-center justify-between bg-muted/50 px-3 py-1 text-xs">
            <span className="truncate font-mono text-muted-foreground">
              {fileName}
            </span>
            {lineCount != null && (
              <span className="ml-2 text-muted-foreground/60">
                {lineCount} lines
              </span>
            )}
          </div>
          <CodeBlock
            language={language}
            startingLineNumber={parsed.startLine}
            wrapLongLines
            className="tool-code-surface"
            customStyle={{ ...TOOL_RESULT_CUSTOM_STYLE, borderRadius: 0 }}
          >
            {parsed.content}
          </CodeBlock>
        </div>
      );
    }
  }

  if (toolType === "grep") {
    const groups = parseGrepOutput(resultStr);
    if (groups) {
      const matchCount = metadata?.match_count as number | undefined;
      return (
        <div className="space-y-2">
          {matchCount != null && (
            <div className="text-xs text-muted-foreground/60">
              {matchCount} match{matchCount !== 1 ? "es" : ""}
            </div>
          )}
          {groups.map((group, index) => {
            const language = getLanguageFromPath(group.filePath);
            const content = group.lines.map((line) => line.content).join("\n");
            const startLine = group.lines[0].lineNum;
            return (
              <div key={index}>
                <div className="mb-1 font-mono text-xs [overflow-wrap:anywhere] text-muted-foreground">
                  {group.filePath}
                </div>
                <CodeBlock
                  language={language}
                  startingLineNumber={startLine}
                  wrapLongLines
                  className="tool-code-surface"
                  customStyle={TOOL_RESULT_CUSTOM_STYLE}
                >
                  {content}
                </CodeBlock>
              </div>
            );
          })}
        </div>
      );
    }

    const fileLines = resultStr
      .trim()
      .split("\n")
      .filter((line) => line.trim());
    if (fileLines.length > 0) {
      return (
        <div className="space-y-0.5 py-1 font-mono text-xs [overflow-wrap:anywhere]">
          {fileLines.map((file, index) => (
            <div key={index} className="text-muted-foreground">
              {file}
            </div>
          ))}
        </div>
      );
    }
  }

  if (toolType === "bash" && metadata?.exit_code != null) {
    const exitCode = metadata.exit_code as number;
    return (
      <div>
        {exitCode !== 0 && (
          <div className="mb-1 text-xs text-destructive-foreground/70">
            exit code {exitCode}
          </div>
        )}
        <ToolResultBody body={resultStr} />
      </div>
    );
  }

  const toolName = formatToolName(call.tool_name);
  if (toolName === "Agent" || toolName === "Task") {
    return (
      <div className="tool-code-surface max-h-96 overflow-y-auto p-2 text-xs">
        <MarkdownBody content={resultStr} id={`tool-result-${call.id}`} />
      </div>
    );
  }

  const envelope = unwrapMcpResultEnvelope(rawContent);
  if (envelope) {
    return (
      <div className="overflow-hidden rounded border border-border/40">
        <MetadataStrip meta={envelope.meta} />
        <ToolResultBody body={envelope.primary} />
      </div>
    );
  }

  return <ToolResultBody body={resultStr} />;
}
