import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { RuleDetail, RuleSummary } from "../../hooks/useRules";
import { cn } from "../../lib/utils";
import { ResizeHandle } from "../shared/ResizeHandle";
import { SegmentedControl } from "../ui/SegmentedControl";
import { Switch } from "../ui/Switch";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import {
  copyRuleWithRetry,
  formatRuleError,
  saveRuleDraft,
} from "./rules/RulesTabActions";
import {
  DEFAULT_RULE_FILTERS,
  RULE_SOURCE_OPTIONS,
  RULE_STATUS_OPTIONS,
  isBundledRule,
  useRulesTabData,
  type RuleDraft,
  type RuleStatusSegment,
  type RulesFilters,
} from "./rules/RulesTabData";
import { RulesDetailPanel } from "./rules/RulesDetailPanel";
import { RulesTabList } from "./rules/RulesTabList";

interface RulesTabProps {
  projectId?: string | null;
}

interface RulesFilterDropdownProps {
  filters: RulesFilters;
  eventOptions: string[];
  groupOptions: string[];
  tagOptions: string[];
  enforcementEnabled: boolean;
  onFiltersChange: (filters: RulesFilters) => void;
  onEnforcementChange: (enabled: boolean) => void;
}

function RulesFilterDropdown({
  filters,
  eventOptions,
  groupOptions,
  tagOptions,
  enforcementEnabled,
  onFiltersChange,
  onEnforcementChange,
}: RulesFilterDropdownProps) {
  return (
    <div className="rules-filter-dropdown">
      <label className="rules-filter-dropdown__field">
        <span>Event</span>
        <select
          aria-label="Event"
          value={filters.event}
          onChange={(event) => onFiltersChange({ ...filters, event: event.target.value })}
        >
          <option value="">Any event</option>
          {eventOptions.map((eventName) => (
            <option key={eventName} value={eventName}>
              {eventName}
            </option>
          ))}
        </select>
      </label>
      <label className="rules-filter-dropdown__field">
        <span>Group</span>
        <select
          aria-label="Group"
          value={filters.group}
          onChange={(event) => onFiltersChange({ ...filters, group: event.target.value })}
        >
          <option value="">Any group</option>
          {groupOptions.map((group) => (
            <option key={group} value={group}>
              {group}
            </option>
          ))}
        </select>
      </label>
      <label className="rules-filter-dropdown__field">
        <span>Source</span>
        <select
          aria-label="Source"
          value={filters.source}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              source: event.target.value as RulesFilters["source"],
            })
          }
        >
          {RULE_SOURCE_OPTIONS.map((source) => (
            <option key={source.value} value={source.value}>
              {source.label}
            </option>
          ))}
        </select>
      </label>
      <label className="rules-filter-dropdown__field">
        <span>Tag</span>
        <select
          aria-label="Tag"
          value={filters.tag}
          onChange={(event) => onFiltersChange({ ...filters, tag: event.target.value })}
        >
          <option value="">Any tag</option>
          {tagOptions.map((tag) => (
            <option key={tag} value={tag}>
              {tag}
            </option>
          ))}
        </select>
      </label>
      <div className="rules-filter-dropdown__footer">
        <span>Enforcement</span>
        <Switch
          checked={enforcementEnabled}
          aria-label="Rules enforcement"
          onChange={onEnforcementChange}
        />
      </div>
    </div>
  );
}

export function RulesTab({ projectId: _projectId }: RulesTabProps) {
  const data = useRulesTabData();
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [detail, setDetail] = useState<RuleDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [showFilters, setShowFilters] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyRuleName, setBusyRuleName] = useState<string | null>(null);
  const [detailRefreshToken, setDetailRefreshToken] = useState(0);
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());
  const { fetchRuleDetail } = data;

  const existingNames = useMemo(() => data.rules.map((rule) => rule.name), [data.rules]);

  useEffect(() => {
    if (!selectedName) return;

    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setDetailLoading(true);
      setDetailError(null);
      try {
        const nextDetail = await fetchRuleDetail(selectedName);
        if (cancelled) return;
        if (!nextDetail) {
          setDetail(null);
          setDetailError(`Rule "${selectedName}" was not found`);
          return;
        }
        setDetail(nextDetail);
      } catch (error) {
        if (!cancelled) setDetailError(formatRuleError(error));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [detailRefreshToken, fetchRuleDetail, selectedName]);

  const guardedRun = useCallback((next: () => void) => {
    confirmLeaveRef.current(next);
  }, []);

  const handleSelect = useCallback(
    (rule: RuleSummary) => {
      void guardedRun(() => setSelectedName(rule.name));
    },
    [guardedRun],
  );

  const handleStatusChange = useCallback(
    (value: RuleStatusSegment) => {
      void guardedRun(() => data.setStatusSegment(value));
    },
    [data, guardedRun],
  );

  const handleFiltersChange = useCallback(
    (filters: RulesFilters) => {
      void guardedRun(() => data.setFilters(filters));
    },
    [data, guardedRun],
  );

  const handleToggle = useCallback(
    async (rule: RuleSummary) => {
      setActionError(null);
      setBusyRuleName(rule.name);
      try {
        const didToggle = await data.toggleRule(rule.name, !rule.enabled);
        if (!didToggle) {
          setActionError(`Failed to ${rule.enabled ? "deactivate" : "activate"} rule`);
          return;
        }
        if (selectedName === rule.name) setDetailRefreshToken((value) => value + 1);
      } catch (error) {
        setActionError(formatRuleError(error));
      } finally {
        setBusyRuleName(null);
      }
    },
    [data, selectedName],
  );

  const handleCopy = useCallback(
    async (rule: RuleSummary) => {
      setActionError(null);
      setBusyRuleName(rule.name);
      try {
        const copiedName = await copyRuleWithRetry(rule, existingNames, data);
        setSelectedName(copiedName);
      } catch (error) {
        setActionError(formatRuleError(error));
      } finally {
        setBusyRuleName(null);
      }
    },
    [data, existingNames],
  );

  const handleDelete = useCallback(
    async (rule: RuleSummary) => {
      if (!window.confirm(`Delete "${rule.name}"?`)) return;
      setActionError(null);
      setBusyRuleName(rule.name);
      try {
        await data.deleteRule(rule.name, isBundledRule(rule));
        if (selectedName === rule.name) {
          setSelectedName(null);
          setDetail(null);
        }
      } catch (error) {
        setActionError(formatRuleError(error));
      } finally {
        setBusyRuleName(null);
      }
    },
    [data, selectedName],
  );

  const handleSave = useCallback(
    async (originalName: string, draft: RuleDraft) => {
      const savedName = await saveRuleDraft(originalName, draft, data);
      setSelectedName(savedName);
      const savedDetail = await data.fetchRuleDetail(savedName);
      if (savedDetail) setDetail(savedDetail);
      return savedName;
    },
    [data],
  );

  const hasActiveFilters = data.activeFilterCount > 0 || data.search.trim().length > 0;
  const emptyMessage = hasActiveFilters
    ? "No rules match the current filters"
    : data.statusSegment === "enabled"
      ? "No enabled rules"
      : "No disabled rules";

  return (
    <div className="rules-tab">
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={data.search}
          onChange={data.setSearch}
          placeholder="Search"
          ariaLabel="Search rules"
        />
        <SegmentedControl<RuleStatusSegment>
          value={data.statusSegment}
          onChange={handleStatusChange}
          options={[...RULE_STATUS_OPTIONS]}
          ariaLabel="Rule status filter"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
          onClick={() => setShowFilters((value) => !value)}
          aria-label="Filter rules"
          title="Filter rules"
          aria-expanded={showFilters}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          <span className="activity-panel-action-btn__label">Filter</span>
          {data.activeFilterCount > 0 && (
            <span className="activity-filter-badge">{data.activeFilterCount}</span>
          )}
        </button>
        {showFilters && (
          <RulesFilterDropdown
            filters={data.filters}
            eventOptions={data.eventOptions}
            groupOptions={data.groupOptions}
            tagOptions={data.tagOptions}
            enforcementEnabled={data.enforcementEnabled}
            onFiltersChange={handleFiltersChange}
            onEnforcementChange={(enabled) => void data.setEnforcement(enabled)}
          />
        )}
      </div>

      {actionError && (
        <button
          type="button"
          className="rules-action-error"
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </button>
      )}

      <div
        className={cn("rules-list-shell", selectedName && "rules-list-shell--split")}
        style={selectedName ? { height: `${topHeight}%` } : undefined}
      >
        {data.isLoading && data.rules.length === 0 ? (
          <ActivityPanelEmpty body="Loading rules..." />
        ) : data.filteredRules.length === 0 ? (
          <ActivityPanelEmpty
            heading="Rules"
            body={emptyMessage}
            footer={
              hasActiveFilters ? (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => {
                    data.setSearch("");
                    data.setFilters(DEFAULT_RULE_FILTERS);
                  }}
                >
                  Clear filters
                </button>
              ) : undefined
            }
          />
        ) : (
          <RulesTabList
            rules={data.filteredRules}
            selectedName={selectedName}
            busyRuleName={busyRuleName}
            onSelect={handleSelect}
            onToggle={(rule) => void handleToggle(rule)}
            onCopy={(rule) => void handleCopy(rule)}
            onDelete={(rule) => void handleDelete(rule)}
          />
        )}
      </div>

      {selectedName && (
        <>
          <ResizeHandle
            direction="vertical"
            onResize={setTopHeight}
            panelHeight={topHeight}
            minHeight={15}
            maxHeight={80}
          />
          <div className="rules-detail-shell">
            <RulesDetailPanel
              detail={detail}
              isLoading={detailLoading}
              error={detailError}
              onSave={handleSave}
              onError={setActionError}
              onConfirmLeaveChange={(confirmIfDirty) => {
                confirmLeaveRef.current = confirmIfDirty;
              }}
            />
          </div>
        </>
      )}
    </div>
  );
}
