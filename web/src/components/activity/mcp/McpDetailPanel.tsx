import type {
  McpServer,
  McpStatus,
  McpTool,
  McpToolSchema,
} from "../../../hooks/useMcp";
import { JsonBlock } from "../../chat/JsonBlock";
import { JsonResultBlock } from "../../chat/ToolResultBlocks";
import { Heading } from "../../shared/Heading";
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
  if (!selection) {
    return (
      <div className="activity-mcp-detail">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail">
          <span className="activity-panel-status-bar__title">MCP</span>
        </div>
        <div className="activity-mcp-detail-body activity-mcp-detail-body--empty">
          Select a server or tool.
        </div>
      </div>
    );
  }

  if (selection.kind === "server") {
    const health = server ? status?.server_health?.[server.name] : null;
    return (
      <div className="activity-mcp-detail">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail">
          <span className="activity-panel-status-bar__title">
            {server?.name ?? selection.serverName}
          </span>
        </div>
        <div className="activity-mcp-detail-body">
          {server ? (
            <dl className="activity-mcp-kv">
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
            <p className="activity-mcp-detail-muted">Server not found.</p>
          )}
        </div>
      </div>
    );
  }

  const title = `${selection.serverName}.${selection.toolName}`;
  return (
    <div className="activity-mcp-detail">
      <div className="activity-panel-status-bar activity-panel-status-bar--detail">
        <span className="activity-panel-status-bar__title">{title}</span>
        {schema && (
          <div className="activity-panel-status-bar__actions">
            <button
              type="button"
              className="btn btn-accent btn-sm activity-panel-action-btn"
              onClick={onCallTool}
              disabled={executing}
              aria-label="Call tool"
              title="Call tool"
            >
              <CallToolIcon />
              <span className="activity-panel-action-btn__label">
                {executing ? "Calling" : "Call Tool"}
              </span>
            </button>
          </div>
        )}
      </div>
      <div className="activity-mcp-detail-body">
        {schemaLoading ? (
          <p className="activity-mcp-detail-muted">Loading schema...</p>
        ) : schema ? (
          <>
            {(schema.description || tool?.brief) && (
              <p className="activity-mcp-description">
                {schema.description || tool?.brief}
              </p>
            )}
            <section className="activity-mcp-section">
              <Heading level={3} className="activity-mcp-section-title">
                Input Schema
              </Heading>
              <JsonBlock
                value={schema.inputSchema}
                className="activity-mcp-json-block"
              />
            </section>
            <section className="activity-mcp-section">
              <Heading level={3} className="activity-mcp-section-title">
                Arguments
              </Heading>
              <ToolArgumentForm
                schema={schema.inputSchema}
                values={argumentValues}
                onChange={onArgumentValuesChange}
                disabled={executing}
              />
            </section>
            {executionResult && (
              <section className="activity-mcp-section">
                <Heading level={3} className="activity-mcp-section-title">
                  Result
                </Heading>
                <JsonResultBlock
                  value={executionResult.value}
                  variant={executionResult.success ? "normal" : "error"}
                  className="activity-mcp-result"
                />
              </section>
            )}
          </>
        ) : (
          <p className="activity-mcp-detail-muted">Schema unavailable.</p>
        )}
      </div>
    </div>
  );
}
