import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { SegmentedControl } from "../ui/SegmentedControl";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import "./skills/SkillsTab.css";
import {
  deleteSkill,
  downloadSkillExport,
  exportSkill,
  moveSkillToInstalled,
  moveSkillToProject,
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
        if (!window.confirm(`Delete ${skill.name}?`)) return;
        const deleted = await deleteSkill(skill.id);
        if (!deleted) throw new Error(`Failed to delete ${skill.name}`);
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
    <div className="skills-tab flex h-full flex-col">
      <div className="activity-panel-toolbar skills-tab__toolbar">
        {segment === "installed" && (
          <ActivityPanelSearch
            value={filters.search}
            onChange={(search) => setFilters((current) => ({ ...current, search }))}
            placeholder="Search skills"
            ariaLabel="Search skills"
          />
        )}
        <SegmentedControl<SkillSegment>
          options={SKILL_SEGMENT_OPTIONS}
          value={segment}
          onChange={handleSegmentChange}
          ariaLabel="Skills surface"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
        {segment === "installed" && (
          <>
            <select
              className="min-h-8 rounded-md border border-border bg-[var(--bg-secondary)] px-2 text-xs text-foreground pointer-coarse:min-h-11"
              aria-label="Skill source"
              name="skill-source"
              value={filters.source}
              onChange={(event) =>
                setFilters((current) => ({
                  ...current,
                  source: event.target.value as SkillFilters["source"],
                }))
              }
            >
              {SKILL_SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <select
              className="min-h-8 rounded-md border border-border bg-[var(--bg-secondary)] px-2 text-xs text-foreground pointer-coarse:min-h-11"
              aria-label="Skill category"
              name="skill-category"
              value={filters.category}
              onChange={(event) =>
                setFilters((current) => ({ ...current, category: event.target.value }))
              }
            >
              <option value="all">All categories</option>
              {categoryOptions.map((category) => (
                <option key={category} value={category}>
                  {category}
                </option>
              ))}
            </select>
          </>
        )}
      </div>

      {error && (
        <button
          type="button"
          className="border-b border-border bg-error-soft px-3 py-2 text-left text-sm text-error"
          onClick={() => setError(null)}
          aria-label={`Dismiss error: ${error}`}
        >
          {error}
        </button>
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
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
});
