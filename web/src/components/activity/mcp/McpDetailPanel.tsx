import { useState } from "react";

import type {
  McpServer,
  McpStatus,
  McpTool,
  McpToolSchema,
} from "../../../hooks/useMcp";
import { JsonBlock } from "../../chat/JsonBlock";
import { JsonResultBlock } from "../../chat/ToolResultBlocks";
import { Heading } from "../../shared/Heading";
import { Button } from "../../ui/Button";
import { ToolArgumentForm } from "../../command-browser/ToolArgumentForm";
import { CallToolIcon } from "./mcpIcons";
import { getServerType, type McpSelection } from "./mcpShared";

export interface McpExecutionResult {
  success: boolean;
  value: unknown;
}

interface McpDetailPanelProps {
  selection: McpSelection | null;
  server: McpServer | null;
  tool: McpTool | null;
  schema: McpToolSchema | null;
  schemaLoading: boolean;
  argumentValues: Record<string, unknown>;
  onArgumentValuesChange: (values: Record<string, unknown>) => void;
  executing: boolean;
  executionResult: McpExecutionResult | null;
  onCallTool: () => void;
  status: McpStatus | null;
  toolsByServer: Record<string, McpTool[]>;
}

export function McpDetailPanel({
  selection,
  server,
  tool,
  schema,
  schemaLoading,
  argumentValues,
  onArgumentValuesChange,
  executing,
  executionResult,
  onCallTool,
  status,
  toolsByServer,
}: McpDetailPanelProps) {
  const [formValid, setFormValid] = useState(true);

  if (!selection) {
    return (
      <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center justify-between gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
          <span className="activity-panel-status-bar__title block min-w-0 truncate text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-primary)]">
            MCP
          </span>
        </div>
        <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
          Select a server or tool.
        </div>
      </div>
    );
  }

  if (selection.kind === "server") {
    const health = server ? status?.server_health?.[server.name] : null;
    return (
      <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center justify-between gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
          <span className="activity-panel-status-bar__title block min-w-0 truncate text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-primary)]">
            {server?.name ?? selection.serverName}
          </span>
        </div>
        <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-primary)]">
          {server ? (
            <dl className="m-0 grid gap-[0.45rem] [&>div]:grid [&>div]:grid-cols-[minmax(5.5rem,30%)_minmax(0,1fr)] [&>div]:gap-3 [&_dd]:m-0 [&_dd]:min-w-0 [&_dd]:[overflow-wrap:anywhere] [&_dt]:text-[var(--text-secondary)]">
              <div>
                <dt>Type</dt>
                <dd>{getServerType(server) === "internal" ? "Internal" : "External"}</dd>
              </div>
              <div>
                <dt>Transport</dt>
                <dd>{server.transport}</dd>
              </div>
              <div>
                <dt>State</dt>
                <dd>{server.state}</dd>
              </div>
              <div>
                <dt>Health</dt>
                <dd>{health?.health ?? "unknown"}</dd>
              </div>
              <div>
                <dt>Enabled</dt>
                <dd>{server.enabled === false ? "No" : "Yes"}</dd>
              </div>
              <div>
                <dt>Tools</dt>
                <dd>{toolsByServer[server.name]?.length ?? 0}</dd>
              </div>
              {server.note && (
                <div>
                  <dt>Note</dt>
                  <dd>{server.note}</dd>
                </div>
              )}
            </dl>
          ) : (
            <p className="text-[var(--text-secondary)]">Server not found.</p>
          )}
        </div>
      </div>
    );
  }

  const title = `${selection.serverName}.${selection.toolName}`;
  return (
    <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
      <div className="activity-panel-status-bar activity-panel-status-bar--detail flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center justify-between gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
        <span className="activity-panel-status-bar__title block min-w-0 truncate text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-primary)]">
          {title}
        </span>
        {schema && (
          <div className="activity-panel-status-bar__actions flex flex-none items-center gap-3">
            <Button
              type="button"
              variant="accent"
              size="sm"
              onClick={onCallTool}
              disabled={executing || !formValid}
              aria-label="Call tool"
              title="Call tool"
            >
              <CallToolIcon />
              <span className="activity-panel-action-btn__label @max-[479px]/activity-panel:hidden">
                {executing ? "Calling" : "Call Tool"}
              </span>
            </Button>
          </div>
        )}
      </div>
      <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-primary)]">
        {schemaLoading ? (
          <p className="text-[var(--text-secondary)]">Loading schema...</p>
        ) : schema ? (
          <>
            {(schema.description || tool?.brief) && (
              <p className="mb-3 mt-0 text-[var(--text-secondary)]">
                {schema.description || tool?.brief}
              </p>
            )}
            <section>
              <Heading level={3} className="mb-[0.4rem] mt-0 text-[length:var(--text-sm)] font-[var(--font-weight-medium)]">
                Input Schema
              </Heading>
              <JsonBlock
                value={schema.inputSchema}
                className="max-h-56 overflow-auto rounded-md border border-[var(--border)] bg-[var(--bg-deep)] px-[0.6rem] py-2 text-[length:var(--text-xs)] leading-[1.45] [overflow-wrap:anywhere]"
              />
            </section>
            <section className="mt-[0.9rem]">
              <Heading level={3} className="mb-[0.4rem] mt-0 text-[length:var(--text-sm)] font-[var(--font-weight-medium)]">
                Arguments
              </Heading>
              <ToolArgumentForm
                schema={schema.inputSchema}
                values={argumentValues}
                onChange={onArgumentValuesChange}
                onValidityChange={setFormValid}
                disabled={executing}
              />
            </section>
            {executionResult && (
              <section className="mt-[0.9rem]">
                <Heading level={3} className="mb-[0.4rem] mt-0 text-[length:var(--text-sm)] font-[var(--font-weight-medium)]">
                  Result
                </Heading>
                <JsonResultBlock
                  value={executionResult.value}
                  variant={executionResult.success ? "normal" : "error"}
                />
              </section>
            )}
          </>
        ) : (
          <p className="text-[var(--text-secondary)]">Schema unavailable.</p>
        )}
      </div>
    </div>
  );
}
