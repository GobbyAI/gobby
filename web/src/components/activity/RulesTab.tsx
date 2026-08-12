import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { RuleDetail, RuleSummary } from "../../hooks/useRules";
import { cn } from "../../lib/utils";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { Switch } from "../ui/Switch";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { FilterDropdownShell, InlineFilterFieldRow } from "./FilterPrimitives";
import { useRegisterActivityActions } from "./activityActions";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { SelectField } from "./fields";
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
  onReset: () => void;
  onClose: () => void;
}

function RulesFilterDropdown({
  filters,
  eventOptions,
  groupOptions,
  tagOptions,
  enforcementEnabled,
  onFiltersChange,
  onEnforcementChange,
  onReset,
  onClose,
}: RulesFilterDropdownProps) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <FilterDropdownShell
      ariaLabel="Rule filters"
      onClose={onClose}
      overlayTestId="rules-filter-overlay"
      panelVariant="inline"
      className="z-[100]"
    >
      <div className="grid grid-cols-2 gap-1">
        <InlineFilterFieldRow>
          <SelectField
            label="Event"
            ariaLabel="Filter by event"
            value={filters.event}
            onChange={(event) => onFiltersChange({ ...filters, event })}
            options={[
              { value: "", label: "Any event" },
              ...eventOptions.map((eventName) => ({
                value: eventName,
                label: eventName,
              })),
            ]}
          />
        </InlineFilterFieldRow>
        <InlineFilterFieldRow>
          <SelectField
            label="Group"
            ariaLabel="Filter by group"
            value={filters.group}
            onChange={(group) => onFiltersChange({ ...filters, group })}
            options={[
              { value: "", label: "Any group" },
              ...groupOptions.map((group) => ({ value: group, label: group })),
            ]}
          />
        </InlineFilterFieldRow>
        <InlineFilterFieldRow>
          <SelectField
            label="Source"
            ariaLabel="Filter by source"
            value={filters.source}
            onChange={(source) =>
              onFiltersChange({
                ...filters,
                source: source as RulesFilters["source"],
              })
            }
            options={[...RULE_SOURCE_OPTIONS]}
          />
        </InlineFilterFieldRow>
        <InlineFilterFieldRow>
          <SelectField
            label="Tag"
            ariaLabel="Filter by tag"
            value={filters.tag}
            onChange={(tag) => onFiltersChange({ ...filters, tag })}
            options={[
              { value: "", label: "Any tag" },
              ...tagOptions.map((tag) => ({ value: tag, label: tag })),
            ]}
          />
        </InlineFilterFieldRow>
      </div>
      <div className="flex items-center justify-between gap-2 border-t border-border px-2 py-1.5">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          dense
          className={coarseHitAreaCls}
          onClick={onReset}
          aria-label="Reset rule filters"
        >
          Reset
        </Button>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span>Enforcement</span>
          <Switch
            checked={enforcementEnabled}
            aria-label="Rules enforcement"
            onChange={onEnforcementChange}
          />
        </div>
      </div>
    </FilterDropdownShell>
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
  const filterTriggerRef = useRef<HTMLElement | null>(null);
  const { fetchRuleDetail } = data;

  const existingNames = useMemo(
    () => data.rules.map((rule) => rule.name),
    [data.rules],
  );

  const guardedRun = useCallback((next: () => void) => {
    confirmLeaveRef.current(next);
  }, []);

  useEffect(() => {
    const keep =
      selectedName !== null &&
      data.rules.some((rule) => rule.name === selectedName);
    const next = keep ? selectedName : (data.filteredRules[0]?.name ?? null);
    if (next !== selectedName) guardedRun(() => setSelectedName(next));
  }, [data.filteredRules, data.rules, guardedRun, selectedName]);

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

  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    data.setSearch("");
  };
  const closeFilters = useCallback(() => {
    setShowFilters(false);
    filterTriggerRef.current?.focus();
  }, []);
  const toggleFilters = () => {
    if (showFilters) {
      closeFilters();
      return;
    }
    if (document.activeElement instanceof HTMLElement) {
      filterTriggerRef.current = document.activeElement;
    }
    setShowFilters(true);
    closeSearch();
  };
  const openSearch = () => {
    setShowFilters(false);
    setSearchOpen(true);
  };

  useRegisterActivityActions(
    {
      selector: {
        value: data.statusSegment,
        onChange: (value) => handleStatusChange(value as RuleStatusSegment),
        options: RULE_STATUS_OPTIONS,
        ariaLabel: "Rule status filter",
      },
      filter: {
        open: showFilters,
        onToggle: toggleFilters,
        ariaLabel: "Filter rules",
        activeCount: data.activeFilterCount,
      },
      search: {
        open: searchOpen,
        onToggle: searchOpen ? closeSearch : openSearch,
        ariaLabel: "Search rules",
      },
    },
    // handleStatusChange is deliberately not a dep: `data` is a fresh object
    // every render, so listing the callback would re-register (and re-render
    // via context) in a loop. The closures it reaches — guardedRun and the
    // useState setters on data — are stable, so a stale capture is safe.
    [data.statusSegment, showFilters, data.activeFilterCount, searchOpen],
  );

  const handleToggle = useCallback(
    async (rule: RuleSummary) => {
      setActionError(null);
      setBusyRuleName(rule.name);
      try {
        const didToggle = await data.toggleRule(rule.name, !rule.enabled);
        if (!didToggle) {
          setActionError(
            `Failed to ${rule.enabled ? "disable" : "enable"} rule`,
          );
          return;
        }
        if (selectedName === rule.name)
          setDetailRefreshToken((value) => value + 1);
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

  const hasActiveFilters =
    data.activeFilterCount > 0 || data.search.trim().length > 0;
  const emptyMessage = hasActiveFilters
    ? "No rules match the current filters"
    : data.statusSegment === "enabled"
      ? "No enabled rules"
      : "No disabled rules";

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={data.search}
          onChange={data.setSearch}
          placeholder="Search"
          ariaLabel="Search rules"
          onClose={closeSearch}
        />
      )}
      {showFilters && (
        <RulesFilterDropdown
          filters={data.filters}
          eventOptions={data.eventOptions}
          groupOptions={data.groupOptions}
          tagOptions={data.tagOptions}
          enforcementEnabled={data.enforcementEnabled}
          onFiltersChange={handleFiltersChange}
          onEnforcementChange={(enabled) => void data.setEnforcement(enabled)}
          onReset={() => handleFiltersChange(DEFAULT_RULE_FILTERS)}
          onClose={closeFilters}
        />
      )}

      {actionError && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          className={cn(
            "min-h-11 cursor-pointer rounded-none border-0 border-b border-[var(--border)] bg-[var(--color-warning-soft)] px-3 py-[0.45rem] text-left text-[length:var(--text-sm)] text-[var(--color-warning-foreground)]",
            coarseHitAreaCls,
          )}
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </Button>
      )}

      <div
        className={cn(
          "min-h-0 flex-[1_1_auto] overflow-auto",
          selectedName && "flex-[0_0_auto] border-b border-[var(--border)]",
        )}
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
                <Button
                  type="button"
                  size="sm"
                  className={coarseHitAreaCls}
                  onClick={() => {
                    data.setSearch("");
                    data.setFilters(DEFAULT_RULE_FILTERS);
                  }}
                >
                  Clear filters
                </Button>
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
          <div className="min-h-0 flex-[1_1_auto]">
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
