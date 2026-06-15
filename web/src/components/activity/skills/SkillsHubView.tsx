import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";

import { ResizeHandle } from "../../chat/artifacts/ResizeHandle";
import { ActivityPanelEmpty, TasksEmptyIcon } from "../ActivityPanelEmpty";
import { DEFAULT_TOP_PANEL_PERCENT } from "../constants";
import { loadSkillHubs, searchSkillHubs } from "./SkillsTabActions";
import type { ActivitySkill, SkillHub, SkillHubResult } from "./SkillsTabData";
import { SkillsHubDetail } from "./SkillsHubDetail";

interface SkillsHubViewProps {
  projectId?: string | null;
  onInstalled: (skill: ActivitySkill) => void;
  onError: (message: string | null) => void;
}

function resultKey(result: SkillHubResult): string {
  return `${result.hub_name}/${result.slug}`;
}

function hubAuthLabel(hub: SkillHub): string {
  if (hub.auth_required === false) return "open";
  if (hub.auth_configured === true) return "auth ready";
  if (hub.auth_required === true) return hub.auth_key_name ? `needs ${hub.auth_key_name}` : "auth required";
  return hub.type;
}

export function SkillsHubView({ projectId, onInstalled, onError }: SkillsHubViewProps) {
  const [hubs, setHubs] = useState<SkillHub[]>([]);
  const [hubErrors, setHubErrors] = useState<Record<string, string>>({});
  const [selectedHub, setSelectedHub] = useState("all");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SkillHubResult[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [loadingHubs, setLoadingHubs] = useState(true);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoadingHubs(true);
      onError(null);
      try {
        const loaded = await loadSkillHubs();
        if (!cancelled) setHubs(loaded);
      } catch (error) {
        if (!cancelled) onError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) setLoadingHubs(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [onError]);

  const selectedResult = useMemo(
    () => results.find((result) => resultKey(result) === selectedKey) ?? null,
    [results, selectedKey],
  );

  const handleSearch = useCallback(async () => {
    const trimmed = query.trim();
    setSearched(true);
    setSelectedKey(null);
    if (!trimmed) {
      setResults([]);
      setHubErrors({});
      return;
    }

    setSearching(true);
    onError(null);
    try {
      const response = await searchSkillHubs(
        trimmed,
        selectedHub === "all" ? undefined : selectedHub,
      );
      setResults(response.results);
      setHubErrors(response.hubErrors);
      setSelectedKey(response.results[0] ? resultKey(response.results[0]) : null);
    } catch (error) {
      setResults([]);
      setHubErrors({});
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  }, [onError, query, selectedHub]);

  function handleSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      void handleSearch();
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="overflow-y-auto border-b border-border" style={{ height: `${topHeight}%` }}>
        <div className="flex flex-col gap-3 p-3">
          <div className="grid gap-2 [grid-template-columns:minmax(9rem,0.45fr)_minmax(12rem,1fr)_auto] max-[560px]:grid-cols-1">
            <label className="flex min-w-0 flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">Hub</span>
              <select
                className="min-h-11 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                aria-label="Hub source"
                name="hub-source"
                value={selectedHub}
                onChange={(event) => setSelectedHub(event.target.value)}
              >
                <option value="all">All hubs</option>
                {hubs.map((hub) => (
                  <option key={hub.name} value={hub.name}>
                    {hub.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex min-w-0 flex-col gap-1.5">
              <span className="text-xs font-medium text-muted-foreground">Search</span>
              <input
                type="search"
                className="min-h-11 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                aria-label="Search hub skills"
                name="hub-skill-search"
                value={query}
                placeholder="Search hub skills"
                onKeyDown={handleSearchKeyDown}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn btn-primary mt-auto"
              aria-label="Search hub skills"
              disabled={searching}
              onClick={() => void handleSearch()}
            >
              {searching ? "Searching..." : "Search"}
            </button>
          </div>

          {loadingHubs ? (
            <div className="rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2 text-sm text-muted-foreground">
              Loading skill hubs...
            </div>
          ) : hubs.length === 0 ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Skill Hub"
              body="No skill hubs are configured for this daemon."
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              {hubs.map((hub) => (
                <span
                  key={hub.name}
                  className="inline-flex min-h-8 items-center gap-1.5 rounded-md border border-border bg-[var(--bg-secondary)] px-2 text-xs text-muted-foreground"
                >
                  <span className="font-medium text-foreground">{hub.name}</span>
                  {hubAuthLabel(hub)}
                </span>
              ))}
            </div>
          )}

          {Object.keys(hubErrors).length > 0 && (
            <div className="grid gap-2" aria-label="Hub search errors">
              {Object.entries(hubErrors).map(([hub, error]) => (
                <div
                  key={hub}
                  className="rounded-md border border-border bg-[var(--color-warning-soft)] px-3 py-2 text-sm text-foreground"
                >
                  <span className="font-semibold text-[var(--color-warning-foreground)]">{hub}</span>{" "}
                  {error}
                </div>
              ))}
            </div>
          )}

          {results.length > 0 ? (
            <div className="flex flex-col" role="list" aria-label="Hub search results">
              {results.map((result) => {
                const selected = resultKey(result) === selectedKey;
                return (
                  <div
                    key={resultKey(result)}
                    role="listitem"
                    className={`border-b border-border ${
                      selected ? "bg-[var(--accent-tint)]" : "bg-[var(--bg-primary)]"
                    }`}
                  >
                    <button
                      type="button"
                      aria-label={`Select ${result.display_name || result.slug}`}
                      className="flex min-h-14 w-full min-w-0 items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                      onClick={() => setSelectedKey(resultKey(result))}
                    >
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="activity-row-title">{result.display_name || result.slug}</span>
                        <span className="activity-row-meta truncate">
                          {result.description || "No description"}
                        </span>
                      </span>
                      <span className="activity-chip">
                        {result.hub_name}
                      </span>
                      <span className="activity-chip">
                        {result.version ? `v${result.version}` : "latest"}
                      </span>
                    </button>
                  </div>
                );
              })}
            </div>
          ) : searched && !searching ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Skill Hub"
              body={query.trim() ? "No hub skills match the current search." : "Enter a search query to look across hubs."}
            />
          ) : (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Skill Hub"
              body="Search across configured hubs before installing new skills."
            />
          )}
        </div>
      </div>

      <ResizeHandle
        direction="vertical"
        onResize={setTopHeight}
        panelHeight={topHeight}
        minHeight={25}
        maxHeight={75}
      />

      <div className="min-h-0 flex-1">
        <SkillsHubDetail
          result={selectedResult}
          projectId={projectId}
          onInstalled={onInstalled}
          onError={onError}
        />
      </div>
    </div>
  );
}
