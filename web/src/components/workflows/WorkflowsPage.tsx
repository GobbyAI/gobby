import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { TabBar } from "../shared/TabBar";
import { PipelinesTab } from "./PipelinesTab";
import { CodeMirrorEditor } from "../shared/CodeMirrorEditor";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { useDialogFocus } from "../../hooks/useDialogFocus";
import {
  WORKFLOWS_PAGE_CLS,
  WORKFLOWS_TOOLBAR_CLS,
  WORKFLOWS_TOOLBAR_LEFT_CLS,
  WORKFLOWS_TOOLBAR_TITLE_CLS,
  WORKFLOWS_TAB_ROW_CLS,
  WORKFLOWS_TAB_ROW_RIGHT_CLS,
  WORKFLOWS_FILTER_ICON_BTN_CLS,
  WORKFLOWS_FILTER_ICON_BTN_ACTIVE_CLS,
  WORKFLOWS_SEARCH_CLS,
  WORKFLOWS_TOOLBAR_BTN_CLS,
  WORKFLOWS_TOOLBAR_BTN_SPINNING_CLS,
  WORKFLOWS_NEW_BTN_CLS,
  WORKFLOWS_FILTER_WRAPPER_CLS,
  WORKFLOWS_FILTER_POPOVER_CLS,
  WORKFLOWS_FILTER_POPOVER_SECTION_CLS,
  WORKFLOWS_FILTER_POPOVER_SECTION_BOTTOM_CLS,
  WORKFLOWS_FILTER_POPOVER_LABEL_CLS,
  WORKFLOWS_FILTER_POPOVER_CHIPS_CLS,
  WORKFLOWS_FILTER_POPOVER_CHECKBOX_CLS,
  WORKFLOWS_FILTER_CHIP_CLS,
  WORKFLOWS_FILTER_CHIP_ACTIVE_CLS,
  WORKFLOWS_TOGGLE_TRACK_CLS,
  WORKFLOWS_TOGGLE_TRACK_ON_CLS,
  WORKFLOWS_TOGGLE_KNOB_CLS,
  WORKFLOWS_TOGGLE_KNOB_ON_CLS,
  WORKFLOWS_MODAL_OVERLAY_CLS,
  WORKFLOWS_YAML_MODAL_CLS,
  WORKFLOWS_YAML_HEADER_CLS,
  WORKFLOWS_YAML_HEADER_HEADING_CLS,
  WORKFLOWS_YAML_HEADER_ACTIONS_CLS,
  WORKFLOWS_YAML_ERROR_CLS,
  WORKFLOWS_YAML_EDITOR_CLS,
  WORKFLOWS_LOADING_CLS,
  WORKFLOWS_MODAL_CANCEL_CLS,
  WORKFLOWS_MODAL_SUBMIT_CLS,
} from "./workflows-styles";
import { Heading } from '../shared/Heading'

type SourceFilter = "installed" | "project" | "templates" | "deleted";

const TABS = [
  { id: "pipelines", label: "Pipelines" },
];

const SOURCE_OPTIONS: { value: SourceFilter; label: string }[] = [
  { value: "installed", label: "Installed" },
  { value: "project", label: "Project" },
  { value: "templates", label: "Templates" },
  { value: "deleted", label: "Deleted" },
];

export function WorkflowsPage({ projectId }: { projectId?: string }) {
  const [searchText, setSearchText] = useState("");
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("installed");
  const [devMode, setDevMode] = useState(false);
  const [showPipelineCreate, setShowPipelineCreate] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [hideGobby, setHideGobby] = useState(false);
  const [hideInstalled, setHideInstalled] = useState(false);

  // Lifted tab-specific filter state
  const [pipelineEnabledFilter, setPipelineEnabledFilter] = useState<
    boolean | null
  >(null);

  // Dynamic options reported by tabs
  const [availableTags, setAvailableTags] = useState<string[]>([]);

  // Cross-tab filters
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [priorityFilter, setPriorityFilter] = useState<number | null>(null);

  // Popover state
  const [showFilterPopover, setShowFilterPopover] = useState(false);
  const filterRef = useRef<HTMLDivElement>(null);

  // Fetch dev_mode from admin status
  useEffect(() => {
    fetch("/api/admin/status")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.dev_mode) setDevMode(true);
      })
      .catch(() => {});
  }, []);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    setRefreshKey((k) => k + 1);
    setTimeout(() => setRefreshing(false), 600);
  }, []);

  // Click-outside to close popover
  useEffect(() => {
    if (!showFilterPopover) return;
    const handleMouseDown = (e: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) {
        setShowFilterPopover(false);
      }
    };
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [showFilterPopover]);

  // Badge count
  const activeFilterCount = useMemo(() => {
    let count = 0;
    if (sourceFilter !== "installed") count++;
    if (hideGobby) count++;
    if (sourceFilter === "templates" && hideInstalled) count++;
    if (pipelineEnabledFilter !== null) count++;
    if (tagFilter !== null) count++;
    if (priorityFilter !== null) count++;
    return count;
  }, [
    sourceFilter,
    hideGobby,
    hideInstalled,
    pipelineEnabledFilter,
    tagFilter,
    priorityFilter,
  ]);

  return (
    <main className={WORKFLOWS_PAGE_CLS}>
      {/* Title row */}
      <div className={WORKFLOWS_TOOLBAR_CLS}>
        <div className={WORKFLOWS_TOOLBAR_LEFT_CLS}>
          <Heading level={1} className={WORKFLOWS_TOOLBAR_TITLE_CLS}>Workflows</Heading>
        </div>
      </div>

      {/* Tab bar + search/filter/actions */}
      <div className={WORKFLOWS_TAB_ROW_CLS}>
        <TabBar
          tabs={TABS}
          activeTab="pipelines"
          onTabChange={() => {}}
          className="mb-0 shrink-0"
        />
        <div className={WORKFLOWS_TAB_ROW_RIGHT_CLS}>
          <input
            className={WORKFLOWS_SEARCH_CLS}
            type="text"
            name="workflow-search"
            aria-label="Search workflows"
            placeholder="Search"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
          />
          <div className={WORKFLOWS_FILTER_WRAPPER_CLS} ref={filterRef}>
            <button
              type="button"
              className={`${WORKFLOWS_FILTER_ICON_BTN_CLS} ${activeFilterCount > 0 ? WORKFLOWS_FILTER_ICON_BTN_ACTIVE_CLS : ""}`}
              onClick={() => setShowFilterPopover((v) => !v)}
              title="Filter"
              aria-label="Filter workflows"
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              >
                <line x1="2" y1="4" x2="14" y2="4" />
                <line x1="4" y1="8" x2="12" y2="8" />
                <line x1="6" y1="12" x2="10" y2="12" />
              </svg>
            </button>
            {showFilterPopover && (
              <FilterPopover
                sourceFilter={sourceFilter}
                onSourceFilterChange={setSourceFilter}
                hideGobby={hideGobby}
                onHideGobbyChange={setHideGobby}
                hideInstalled={hideInstalled}
                onHideInstalledChange={setHideInstalled}
                pipelineEnabledFilter={pipelineEnabledFilter}
                onPipelineEnabledFilterChange={setPipelineEnabledFilter}
                tagFilter={tagFilter}
                onTagFilterChange={setTagFilter}
                availableTags={availableTags}
                priorityFilter={priorityFilter}
                onPriorityFilterChange={setPriorityFilter}
              />
            )}
          </div>
          <button
            type="button"
            className={`${WORKFLOWS_TOOLBAR_BTN_CLS} ${refreshing ? WORKFLOWS_TOOLBAR_BTN_SPINNING_CLS : ""}`}
            onClick={handleRefresh}
            title="Refresh"
          >
            &#x21bb;
          </button>
          <button
            type="button"
            className={WORKFLOWS_NEW_BTN_CLS}
            onClick={() => setShowPipelineCreate(true)}
          >
            + Pipeline
          </button>
        </div>
      </div>

      {/* Tab content */}
      <PipelinesTab
        searchText={searchText}
        sourceFilter={sourceFilter}
        devMode={devMode}
        showCreate={showPipelineCreate}
        onCreateHandled={() => setShowPipelineCreate(false)}
        refreshKey={refreshKey}
        projectId={projectId}
        hideGobby={hideGobby}
        hideInstalled={sourceFilter === "templates" && hideInstalled}
        enabledFilter={pipelineEnabledFilter}
        tagFilter={tagFilter}
        priorityFilter={priorityFilter}
        onTagsChange={setAvailableTags}
      />
    </main>
  );
}

function FilterPopover({
  sourceFilter,
  onSourceFilterChange,
  hideGobby,
  onHideGobbyChange,
  hideInstalled,
  onHideInstalledChange,
  pipelineEnabledFilter,
  onPipelineEnabledFilterChange,
  tagFilter,
  onTagFilterChange,
  availableTags,
  priorityFilter,
  onPriorityFilterChange,
}: {
  sourceFilter: SourceFilter;
  onSourceFilterChange: (v: SourceFilter) => void;
  hideGobby: boolean;
  onHideGobbyChange: (v: boolean) => void;
  hideInstalled: boolean;
  onHideInstalledChange: (v: boolean) => void;
  pipelineEnabledFilter: boolean | null;
  onPipelineEnabledFilterChange: (v: boolean | null) => void;
  tagFilter: string | null;
  onTagFilterChange: (v: string | null) => void;
  availableTags: string[];
  priorityFilter: number | null;
  onPriorityFilterChange: (v: number | null) => void;
}) {
  return (
    <div className={WORKFLOWS_FILTER_POPOVER_CLS}>
      {/* Source section */}
      <div className={WORKFLOWS_FILTER_POPOVER_SECTION_CLS}>
        <div className={WORKFLOWS_FILTER_POPOVER_LABEL_CLS}>Source</div>
        <div className={WORKFLOWS_FILTER_POPOVER_CHIPS_CLS}>
          {SOURCE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={`${WORKFLOWS_FILTER_CHIP_CLS} ${sourceFilter === opt.value ? WORKFLOWS_FILTER_CHIP_ACTIVE_CLS : ""}`}
              onClick={() => onSourceFilterChange(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab-specific section */}
      <div className={WORKFLOWS_FILTER_POPOVER_SECTION_CLS}>
        <div className={WORKFLOWS_FILTER_POPOVER_LABEL_CLS}>Status</div>
        <div className={WORKFLOWS_FILTER_POPOVER_CHIPS_CLS}>
          <button
            type="button"
            className={`${WORKFLOWS_FILTER_CHIP_CLS} ${pipelineEnabledFilter === true ? WORKFLOWS_FILTER_CHIP_ACTIVE_CLS : ""}`}
            onClick={() =>
              onPipelineEnabledFilterChange(
                pipelineEnabledFilter === true ? null : true,
              )
            }
          >
            Enabled
          </button>
          <button
            type="button"
            className={`${WORKFLOWS_FILTER_CHIP_CLS} ${pipelineEnabledFilter === false ? WORKFLOWS_FILTER_CHIP_ACTIVE_CLS : ""}`}
            onClick={() =>
              onPipelineEnabledFilterChange(
                pipelineEnabledFilter === false ? null : false,
              )
            }
          >
            Disabled
          </button>
        </div>
      </div>

      <div className={WORKFLOWS_FILTER_POPOVER_SECTION_CLS}>
        <div className={WORKFLOWS_FILTER_POPOVER_LABEL_CLS}>Priority</div>
        <div className={WORKFLOWS_FILTER_POPOVER_CHIPS_CLS}>
          {[1, 2, 3].map((p) => (
            <button
              key={p}
              type="button"
              className={`${WORKFLOWS_FILTER_CHIP_CLS} ${priorityFilter === p ? WORKFLOWS_FILTER_CHIP_ACTIVE_CLS : ""}`}
              onClick={() =>
                onPriorityFilterChange(priorityFilter === p ? null : p)
              }
            >
              P{p}
            </button>
          ))}
        </div>
      </div>

      {/* Tags */}
      {availableTags.length > 0 && (
        <div className={WORKFLOWS_FILTER_POPOVER_SECTION_CLS}>
          <div className={WORKFLOWS_FILTER_POPOVER_LABEL_CLS}>Tag</div>
          <div className={WORKFLOWS_FILTER_POPOVER_CHIPS_CLS}>
            {availableTags.map((tag) => (
              <button
                key={tag}
                type="button"
                className={`${WORKFLOWS_FILTER_CHIP_CLS} ${tagFilter === tag ? WORKFLOWS_FILTER_CHIP_ACTIVE_CLS : ""}`}
                onClick={() =>
                  onTagFilterChange(tagFilter === tag ? null : tag)
                }
              >
                {tag}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Hide Built-in */}
      <div
        className={`${WORKFLOWS_FILTER_POPOVER_SECTION_CLS} ${WORKFLOWS_FILTER_POPOVER_SECTION_BOTTOM_CLS}`}
      >
        <button
          type="button"
          role="switch"
          aria-checked={hideGobby}
          className={WORKFLOWS_FILTER_POPOVER_CHECKBOX_CLS}
          onClick={() => onHideGobbyChange(!hideGobby)}
        >
          <div
            className={`${WORKFLOWS_TOGGLE_TRACK_CLS} ${hideGobby ? WORKFLOWS_TOGGLE_TRACK_ON_CLS : ""}`}
          >
            <div
              className={`${WORKFLOWS_TOGGLE_KNOB_CLS} ${hideGobby ? WORKFLOWS_TOGGLE_KNOB_ON_CLS : ""}`}
            />
          </div>
          <span>Hide Built-in</span>
        </button>
        {sourceFilter === "templates" && (
          <button
            type="button"
            role="switch"
            aria-checked={hideInstalled}
            className={WORKFLOWS_FILTER_POPOVER_CHECKBOX_CLS}
            onClick={() => onHideInstalledChange(!hideInstalled)}
          >
            <div
              className={`${WORKFLOWS_TOGGLE_TRACK_CLS} ${hideInstalled ? WORKFLOWS_TOGGLE_TRACK_ON_CLS : ""}`}
            >
              <div
                className={`${WORKFLOWS_TOGGLE_KNOB_CLS} ${hideInstalled ? WORKFLOWS_TOGGLE_KNOB_ON_CLS : ""}`}
              />
            </div>
            <span>Hide Installed</span>
          </button>
        )}
      </div>
    </div>
  );
}

export function YamlEditorModal({
  workflowName,
  yamlContent,
  loading,
  onChange,
  onSave,
  onClose,
}: {
  workflowName: string;
  yamlContent: string;
  loading: boolean;
  onChange: (content: string) => void;
  onSave: () => Promise<void>;
  onClose: () => void;
}) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const wrappedOnChange = useCallback(
    (content: string) => {
      setIsDirty(true);
      onChange(content);
    },
    [onChange],
  );

  const handleSave = async () => {
    setError(null);
    setSaving(true);
    try {
      await onSave();
      setIsDirty(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid YAML");
    } finally {
      setSaving(false);
    }
  };

  const handleClose = async () => {
    if (
      isDirty &&
      !(await confirm({
        title: "Unsaved changes",
        description: "You have unsaved changes. Discard them?",
        confirmLabel: "Discard",
        destructive: true,
      }))
    )
      return;
    onClose();
  };

  useDialogFocus({ ref: dialogRef, isOpen: true, onClose: handleClose });

  return (
    <div className={WORKFLOWS_MODAL_OVERLAY_CLS} onClick={handleClose}>
      {ConfirmDialogElement}
      <div
        ref={dialogRef}
        className={WORKFLOWS_YAML_MODAL_CLS}
        role="dialog"
        aria-modal="true"
        aria-labelledby="workflows-yaml-title"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <div className={WORKFLOWS_YAML_HEADER_CLS}>
          <Heading level={2}
            id="workflows-yaml-title"
            className={WORKFLOWS_YAML_HEADER_HEADING_CLS}
          >
            Edit YAML — {workflowName}
          </Heading>
          <div className={WORKFLOWS_YAML_HEADER_ACTIONS_CLS}>
            {error && <span className={WORKFLOWS_YAML_ERROR_CLS}>{error}</span>}
            <button
              type="button"
              className={WORKFLOWS_MODAL_CANCEL_CLS}
              onClick={handleClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className={WORKFLOWS_MODAL_SUBMIT_CLS}
              onClick={handleSave}
              disabled={saving || loading}
            >
              {saving ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
        <div className={WORKFLOWS_YAML_EDITOR_CLS}>
          {loading ? (
            <div className={WORKFLOWS_LOADING_CLS}>Loading YAML...</div>
          ) : (
            <CodeMirrorEditor
              content={yamlContent}
              language="yaml"
              onChange={wrappedOnChange}
              onSave={handleSave}
            />
          )}
        </div>
      </div>
    </div>
  );
}
