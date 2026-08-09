import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { useRegisterActivityActions } from "./activityActions";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { SelectField } from "./fields";
import "../chat/styles/rules-tab.css";
import "./skills/SkillsTab.css";
import {
  deleteSkill,
  downloadSkillExport,
  exportSkill,
  moveSkillToInstalled,
  moveSkillToProject,
  restoreSkill,
  toggleSkill,
  updateSkill,
  type SkillUpdatePayload,
} from "./skills/SkillsTabActions";
import {
  filterInstalledSkills,
  loadInstalledSkills,
  SKILL_SEGMENT_OPTIONS,
  SKILL_SOURCE_OPTIONS,
  skillCategories,
  type ActivitySkill,
  type SkillFilters,
  type SkillSegment,
} from "./skills/SkillsTabData";
import { SkillsInstalledDetail } from "./skills/SkillsInstalledDetail";
import { SkillsInstalledList } from "./skills/SkillsInstalledList";
import { SkillsHubView } from "./skills/SkillsHubView";

interface SkillsTabProps {
  projectId?: string | null;
}

const SEGMENT_STORAGE_KEY = "gobby-skills-segment-v1";

function readStoredSegment(): SkillSegment {
  try {
    const stored = window.localStorage.getItem(SEGMENT_STORAGE_KEY);
    return stored === "hub" || stored === "installed" ? stored : "installed";
  } catch {
    return "installed";
  }
}

function skillUpdatePayload(draft: ActivitySkill): SkillUpdatePayload {
  return {
    description: draft.description ?? "",
    content: draft.content ?? "",
    version: draft.version ?? "",
    license: draft.license ?? "",
    compatibility: draft.compatibility ?? "",
    allowed_tools: draft.allowed_tools ?? [],
    enabled: draft.enabled,
    always_apply: draft.always_apply,
    injection_format: draft.injection_format || "summary",
    project_id: draft.project_id,
  };
}

export const SkillsTab = memo(function SkillsTab({ projectId }: SkillsTabProps) {
  const [segment, setSegment] = useState<SkillSegment>(() => readStoredSegment());
  const [skills, setSkills] = useState<ActivitySkill[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [filters, setFilters] = useState<SkillFilters>({
    search: "",
    source: "all",
    category: "all",
  });
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const confirmDiscardChanges = useCallback(
    () =>
      confirm({
        title: "Discard unsaved changes?",
        description: "Your edits to the current skill file will be lost.",
        confirmLabel: "Discard",
        destructive: true,
      }),
    [confirm],
  );

  const refreshSkills = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const loaded = await loadInstalledSkills();
      setSkills(loaded);
      return loaded;
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load skills");
      setSkills([]);
      return [];
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshSkills();
  }, [refreshSkills]);

  useWebSocketEvent("skill_event", () => {
    void refreshSkills();
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(SEGMENT_STORAGE_KEY, segment);
    } catch {
      /* ignore */
    }
  }, [segment]);

  const categoryOptions = useMemo(() => skillCategories(skills), [skills]);
  const filteredSkills = useMemo(
    () => filterInstalledSkills(skills, filters),
    [filters, skills],
  );
  const selectedSkill = useMemo(
    () => skills.find((skill) => skill.id === selectedId) ?? null,
    [selectedId, skills],
  );

  useEffect(() => {
    if (segment !== "installed") return;
    if (filteredSkills.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !filteredSkills.some((skill) => skill.id === selectedId)) {
      const nextSelection = filteredSkills.find((skill) => !skill.deleted_at) ?? filteredSkills[0];
      setSelectedId(nextSelection.id);
    }
  }, [filteredSkills, segment, selectedId]);

  const replaceSkill = useCallback((updated: ActivitySkill) => {
    setSkills((current) =>
      current.some((skill) => skill.id === updated.id)
        ? current.map((skill) => (skill.id === updated.id ? updated : skill))
        : [updated, ...current],
    );
    setSelectedId(updated.id);
  }, []);

  const withSkillBusy = useCallback(async (skill: ActivitySkill, action: () => Promise<void>) => {
    setBusyId(skill.id);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusyId(null);
    }
  }, []);

  const handleSegmentChange = useCallback(
    (next: SkillSegment) => {
      if (next === segment) return;
      confirmLeaveRef.current(() => setSegment(next));
    },
    [segment],
  );

  const [showFilters, setShowFilters] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    setFilters((current) => ({ ...current, search: "" }));
  };
  const activeFilterCount =
    (filters.source === "all" ? 0 : 1) + (filters.category === "all" ? 0 : 1);

  // Filter and search only apply to the installed list; the hub view has its
  // own browsing surface. The segment selector is always available.
  useRegisterActivityActions(
    {
      selector: {
        value: segment,
        onChange: (value) => handleSegmentChange(value as SkillSegment),
        options: SKILL_SEGMENT_OPTIONS,
        ariaLabel: "Skills surface",
      },
      ...(segment === "installed"
        ? {
            filter: {
              open: showFilters,
              onToggle: () => setShowFilters((value) => !value),
              ariaLabel: "Filter skills",
              activeCount: activeFilterCount,
            },
            search: {
              open: searchOpen,
              onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
              ariaLabel: "Search skills",
            },
          }
        : {}),
    },
    [segment, handleSegmentChange, showFilters, activeFilterCount, searchOpen],
  );

  const handleSelect = useCallback((skill: ActivitySkill) => {
    confirmLeaveRef.current(() => setSelectedId(skill.id));
  }, []);

  const handleSave = useCallback(
    async (draft: ActivitySkill) => {
      const payload = skillUpdatePayload(draft);
      const updated = await updateSkill(draft.id, payload);
      if (!updated) return false;
      replaceSkill({ ...updated, ...payload, id: draft.id, name: draft.name } as ActivitySkill);
      return true;
    },
    [replaceSkill],
  );

  const handleToggle = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        const updated = await toggleSkill(skill);
        if (!updated) throw new Error(`Failed to update ${skill.name}`);
        replaceSkill({ ...updated, enabled: !skill.enabled });
      });
    },
    [replaceSkill, withSkillBusy],
  );

  const handleMoveToProject = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        if (!projectId) throw new Error("No active project is available");
        const updated = await moveSkillToProject(skill.id, projectId);
        if (!updated) throw new Error(`Failed to move ${skill.name}`);
        replaceSkill({ ...updated, project_id: projectId, source: "project" });
      });
    },
    [projectId, replaceSkill, withSkillBusy],
  );

  const handleMoveToInstalled = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        const updated = await moveSkillToInstalled(skill.id);
        if (!updated) throw new Error(`Failed to move ${skill.name}`);
        replaceSkill({ ...updated, project_id: null, source: "installed" });
      });
    },
    [replaceSkill, withSkillBusy],
  );

  const handleExport = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        const exported = await exportSkill(skill.id);
        if (!exported) throw new Error(`Failed to export ${skill.name}`);
        downloadSkillExport(exported);
      });
    },
    [withSkillBusy],
  );

  const handleDelete = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        const purge = Boolean(skill.deleted_at);
        const confirmed = await confirm(
          purge
            ? {
                title: `Permanently delete ${skill.name}?`,
                description:
                  "This skill is already deleted. Deleting it again removes it and its files forever — it cannot be restored.",
                confirmLabel: "Delete forever",
                destructive: true,
              }
            : {
                title: `Delete ${skill.name}?`,
                description: "The skill moves to Deleted and can be restored later.",
                confirmLabel: "Delete",
                destructive: true,
              },
        );
        if (!confirmed) return;
        const deleted = await deleteSkill(skill.id);
        if (!deleted) throw new Error(`Failed to delete ${skill.name}`);
        await refreshSkills();
      });
    },
    [confirm, refreshSkills, withSkillBusy],
  );

  const handleRestore = useCallback(
    (skill: ActivitySkill) => {
      void withSkillBusy(skill, async () => {
        const restored = await restoreSkill(skill.id);
        if (!restored) throw new Error(`Failed to restore ${skill.name}`);
        await refreshSkills();
      });
    },
    [refreshSkills, withSkillBusy],
  );

  const handleHubInstalled = useCallback(
    (skill: ActivitySkill) => {
      replaceSkill({
        ...skill,
        source: skill.source ?? "installed",
        deleted_at: null,
      });
      setFilters((current) => ({ ...current, source: "all", category: "all", search: "" }));
      setSegment("installed");
    },
    [replaceSkill],
  );

  return (
    <div className="skills-tab relative flex h-full flex-col">
      {segment === "installed" && searchOpen && (
        <ActivityToolbarSearchRow
          value={filters.search}
          onChange={(search) => setFilters((current) => ({ ...current, search }))}
          placeholder="Search skills"
          ariaLabel="Search skills"
          onClose={closeSearch}
        />
      )}
      {segment === "installed" && showFilters && (
        <div className="activity-filter-panel">
          <div className="activity-filter-panel__field">
            <SelectField
              label="Source"
              ariaLabel="Skill source"
              name="skill-source"
              value={filters.source}
              onChange={(source) =>
                setFilters((current) => ({
                  ...current,
                  source: source as SkillFilters["source"],
                }))
              }
              options={[...SKILL_SOURCE_OPTIONS]}
            />
          </div>
          <div className="activity-filter-panel__field">
            <SelectField
              label="Category"
              ariaLabel="Skill category"
              name="skill-category"
              value={filters.category}
              onChange={(category) =>
                setFilters((current) => ({ ...current, category }))
              }
              options={[
                { value: "all", label: "All categories" },
                ...categoryOptions.map((category) => ({ value: category, label: category })),
              ]}
            />
          </div>
        </div>
      )}

      {error && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          className={`w-full justify-start rounded-none border-x-0 border-t-0 border-b-border bg-error-soft px-3 py-2 text-left text-sm text-error ${coarseHitAreaCls}`}
          onClick={() => setError(null)}
          aria-label={`Dismiss error: ${error}`}
        >
          {error}
        </Button>
      )}

      {segment === "hub" ? (
        <SkillsHubView
          projectId={projectId}
          onInstalled={handleHubInstalled}
          onError={setError}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <div
            className={`overflow-y-auto ${selectedSkill ? "border-b border-border" : "flex-1"}`}
            style={selectedSkill ? { height: `${topHeight}%` } : undefined}
          >
            {isLoading && skills.length === 0 ? (
              <ActivityPanelEmpty
                icon={<TasksEmptyIcon />}
                heading="Installed skills"
                body="Loading installed skills..."
              />
            ) : filteredSkills.length === 0 ? (
              <ActivityPanelEmpty
                icon={<TasksEmptyIcon />}
                heading="Installed skills"
                body={
                  skills.length === 0
                    ? "Installed and project-scoped skills appear here."
                    : "No skills match the current filters."
                }
              />
            ) : (
              <SkillsInstalledList
                skills={filteredSkills}
                selectedId={selectedId}
                busyId={busyId}
                projectId={projectId}
                onSelect={handleSelect}
                onToggle={handleToggle}
                onMoveToProject={handleMoveToProject}
                onMoveToInstalled={handleMoveToInstalled}
                onExport={handleExport}
                onRestore={handleRestore}
                onDelete={handleDelete}
              />
            )}
          </div>
          {selectedSkill && (
            <ResizeHandle
              direction="vertical"
              onResize={setTopHeight}
              panelHeight={topHeight}
              minHeight={25}
              maxHeight={75}
            />
          )}
          {selectedSkill && (
            <div className="min-h-0 flex-1">
              <SkillsInstalledDetail
                key={selectedSkill.id}
                skill={selectedSkill}
                onSave={handleSave}
                onError={setError}
                onConfirmLeaveChange={(handler) => {
                  confirmLeaveRef.current = handler;
                }}
                confirmDiscardChanges={confirmDiscardChanges}
              />
            </div>
          )}
        </div>
      )}
      {ConfirmDialogElement}
    </div>
  );
});
