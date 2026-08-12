import { useState, useEffect, useCallback, useMemo } from "react";
import {
  useMcp,
  type McpServer,
  type McpTool,
  type McpToolSchema,
} from "../../hooks/useMcp";
import { ToolArgumentForm } from "./ToolArgumentForm";
import { Input } from "../ui/Input";
import { Button } from "../ui/Button";
import { Badge } from "../ui/Badge";
import { ScrollArea } from "../ui/ScrollArea";
import { cn } from "../../lib/utils";
import { Heading } from "../shared/Heading";

interface ToolBrowserModalProps {
  filter: "internal" | "external";
  onSendMessage: (content: string, injectContext: string) => void;
  onClose: () => void;
}

export function ToolBrowserModal({
  filter,
  onSendMessage,
  onClose,
}: ToolBrowserModalProps) {
  const {
    servers,
    toolsByServer,
    fetchTools,
    fetchServers,
    fetchToolSchema,
    callTool,
    isLoading,
  } = useMcp();
  const [search, setSearch] = useState("");
  const [selectedServer, setSelectedServer] = useState<string | null>(null);
  const [selectedTool, setSelectedTool] = useState<string | null>(null);
  const [schemaState, setSchemaState] = useState<{
    request: object | null;
    schema: McpToolSchema | null;
    isLoading: boolean;
  }>({ request: null, schema: null, isLoading: false });
  const { schema, isLoading: schemaLoading } = schemaState;
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});
  const [formValid, setFormValid] = useState(true);
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState<{
    success: boolean;
    data?: unknown;
    error?: string;
  } | null>(null);
  const [collapsedServers, setCollapsedServers] = useState<Set<string>>(
    new Set(),
  );
  const [hasFetched, setHasFetched] = useState(false);

  // Lazy fetch on modal open
  useEffect(() => {
    if (!hasFetched) {
      setHasFetched(true);
      fetchServers();
      fetchTools();
    }
  }, [hasFetched, fetchServers, fetchTools]);

  const filteredServers = useMemo(() => {
    return servers.filter((s: McpServer) =>
      filter === "internal"
        ? s.transport === "internal"
        : s.transport !== "internal",
    );
  }, [servers, filter]);

  const filteredToolsByServer = useMemo(() => {
    const out: Record<string, McpTool[]> = {};
    const lowerSearch = search.toLowerCase();
    for (const server of filteredServers) {
      const tools = toolsByServer[server.name] || [];
      const matched = lowerSearch
        ? tools.filter(
            (t) =>
              t.name.toLowerCase().includes(lowerSearch) ||
              t.brief?.toLowerCase().includes(lowerSearch),
          )
        : tools;
      if (matched.length > 0) {
        out[server.name] = matched;
      }
    }
    return out;
  }, [filteredServers, toolsByServer, search]);

  const totalToolCount = useMemo(() => {
    return Object.values(filteredToolsByServer).reduce(
      (sum, tools) => sum + tools.length,
      0,
    );
  }, [filteredToolsByServer]);

  const toggleCollapse = useCallback((serverName: string) => {
    setCollapsedServers((prev) => {
      const next = new Set(prev);
      if (next.has(serverName)) next.delete(serverName);
      else next.add(serverName);
      return next;
    });
  }, []);

  const handleSelectTool = useCallback(
    async (serverName: string, toolName: string) => {
      const request = {};
      setSelectedServer(serverName);
      setSelectedTool(toolName);
      setSchemaState({ request, schema: null, isLoading: true });
      setFormValues({});
      setFormValid(true);
      setResult(null);
      const fetched = await fetchToolSchema(serverName, toolName);
      setSchemaState((current) =>
        current.request === request
          ? { request: null, schema: fetched, isLoading: false }
          : current,
      );
    },
    [fetchToolSchema],
  );

  const handleExecute = useCallback(async () => {
    if (!selectedServer || !selectedTool) return;
    setExecuting(true);
    setResult(null);
    try {
      const res = await callTool(selectedServer, selectedTool, formValues);
      setResult({ success: res.success, data: res.result, error: res.error });
      if (res.success) {
        const resultStr = JSON.stringify(res.result, null, 2);
        onSendMessage(`Ran /${selectedServer}.${selectedTool}`, resultStr);
      }
    } catch (e) {
      setResult({ success: false, error: String(e) });
    } finally {
      setExecuting(false);
    }
  }, [selectedServer, selectedTool, formValues, callTool, onSendMessage]);

  const handleBack = useCallback(() => {
    setSelectedServer(null);
    setSelectedTool(null);
    setSchemaState({ request: null, schema: null, isLoading: false });
    setFormValues({});
    setFormValid(true);
    setResult(null);
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-muted/30 px-4 py-3">
        <div className="flex items-center gap-2">
          <ToolsIcon />
          <Heading level={2} className="text-lg font-semibold text-foreground">
            {filter === "internal" ? "Gobby Tools" : "MCP Tools"}
          </Heading>
          {!isLoading && (
            <span className="text-xs text-muted-foreground">
              ({totalToolCount})
            </span>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          dense
          onClick={onClose}
          className="min-h-0 w-auto rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          aria-label="Close"
        >
          <XIcon />
        </Button>
      </div>

      {/* Mobile: stacked layout. Desktop: side-by-side */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Left panel: server/tool list */}
        <div
          className={cn(
            "flex min-h-0 flex-col border-border",
            selectedTool
              ? "hidden md:flex md:w-[35%] md:border-r"
              : "w-full md:w-[35%] md:border-r",
          )}
        >
          <div className="shrink-0 border-b border-border p-3">
            <Input
              type="text"
              placeholder="Search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="bg-muted/50"
            />
          </div>
          <ScrollArea className="flex-1">
            {isLoading && !Object.keys(filteredToolsByServer).length ? (
              <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
                <SpinnerIcon />
                Loading tools...
              </div>
            ) : Object.keys(filteredToolsByServer).length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {search ? "No tools match your search." : "No tools available."}
              </p>
            ) : (
              Object.entries(filteredToolsByServer).map(
                ([serverName, tools]) => (
                  <div key={serverName}>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      dense
                      className="flex min-h-0 w-full items-center justify-between rounded-none border-0 bg-muted/20 px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted/50"
                      onClick={() => toggleCollapse(serverName)}
                    >
                      <span className="flex items-center gap-1.5">
                        <ChevronIcon
                          collapsed={collapsedServers.has(serverName)}
                        />
                        <span className="font-semibold">{serverName}</span>
                      </span>
                      <Badge variant="default">{tools.length}</Badge>
                    </Button>
                    {!collapsedServers.has(serverName) &&
                      tools.map((tool) => (
                        <Button
                          key={`${serverName}.${tool.name}`}
                          type="button"
                          variant="ghost"
                          size="sm"
                          dense
                          className={cn(
                            "min-h-0 w-full items-stretch justify-start rounded-none border-x-0 border-t-0 border-b border-border/20 px-3 py-2 pl-7 text-left text-sm font-normal whitespace-normal transition-colors",
                            selectedServer === serverName &&
                              selectedTool === tool.name
                              ? "bg-accent/15 text-foreground"
                              : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                          )}
                          onClick={() =>
                            handleSelectTool(serverName, tool.name)
                          }
                        >
                          <div className="text-xs font-medium text-foreground">
                            {tool.name}
                          </div>
                          {tool.brief && (
                            <div className="mt-0.5 truncate text-xs opacity-60">
                              {tool.brief}
                            </div>
                          )}
                        </Button>
                      ))}
                  </div>
                ),
              )
            )}
          </ScrollArea>
        </div>

        {/* Right panel: tool detail + form */}
        <div
          className={cn(
            "flex min-h-0 flex-col",
            selectedTool ? "flex-1" : "hidden flex-1 md:flex",
          )}
        >
          {!selectedTool ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 p-4 text-sm text-muted-foreground">
              <ToolsIcon size={32} />
              <span>Select a tool to view its schema</span>
            </div>
          ) : (
            <>
              {/* Mobile back button */}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                dense
                className="flex min-h-0 shrink-0 items-center justify-start gap-1 rounded-none border-x-0 border-t-0 border-b border-border px-3 py-2 text-sm text-accent hover:bg-muted/50 md:hidden"
                onClick={handleBack}
              >
                <ChevronLeftIcon />
                Back to list
              </Button>

              <div className="shrink-0 border-b border-border bg-muted/20 px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-foreground">
                    {selectedTool}
                  </span>
                  <Badge variant="info">{selectedServer}</Badge>
                </div>
                {schema?.description && (
                  <p className="mt-1 text-sm text-muted-foreground">
                    {schema.description}
                  </p>
                )}
              </div>

              <ScrollArea className="flex-1 px-4 py-3">
                {schemaLoading ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <SpinnerIcon />
                    Loading schema...
                  </div>
                ) : (
                  <>
                    <div className="mb-2 text-xs font-semibold tracking-wider text-muted-foreground uppercase">
                      Arguments
                    </div>
                    <ToolArgumentForm
                      schema={schema?.inputSchema ?? null}
                      values={formValues}
                      onChange={setFormValues}
                      onValidityChange={setFormValid}
                      disabled={executing}
                    />

                    <div className="mt-4 flex gap-2">
                      <Button
                        variant="accent"
                        onClick={handleExecute}
                        disabled={executing || schemaLoading || !formValid}
                      >
                        {executing ? "Executing..." : "Execute"}
                      </Button>
                    </div>

                    {result && (
                      <div className="mt-4">
                        <div className="mb-2 text-xs font-semibold tracking-wider text-muted-foreground uppercase">
                          Result
                        </div>
                        <div
                          className={cn(
                            "max-h-[30vh] overflow-x-auto overflow-y-auto rounded-md border p-3 font-mono text-sm whitespace-pre-wrap",
                            result.success
                              ? "border-success/50 bg-success/5 text-foreground"
                              : "border-destructive-foreground/50 bg-destructive/5 text-destructive-foreground",
                          )}
                        >
                          {result.error
                            ? `Error: ${result.error}`
                            : JSON.stringify(result.data, null, 2)}
                        </div>
                      </div>
                    )}
                  </>
                )}
              </ScrollArea>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function XIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

function ChevronIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn("transition-transform", collapsed ? "" : "rotate-90")}
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

function ChevronLeftIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

function ToolsIcon({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="text-accent"
    >
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      className="animate-spin"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        strokeDasharray="32"
        strokeDashoffset="32"
      />
    </svg>
  );
}
