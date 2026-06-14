import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { SegmentedControl } from "../ui/SegmentedControl";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import {
  deleteProfile,
  deleteStage,
  restoreProfile,
  restoreStage,
  saveProfileDraft,
  saveStageDraft,
  setProfileAsDefault,
  setProfileEnabled,
} from "./stages/StagesTabActions";
import {
  STAGE_SEGMENT_OPTIONS,
  type BuildProfile,
  type StageEntry,
  type StageSegment,
  filterProfiles,
  filterStages,
  loadStagesTabData,
  profileKey,
  stageNames,
} from "./stages/StagesTabData";
import { ProfileDetailPanel } from "./stages/ProfileDetailPanel";
import { ProfilesList } from "./stages/ProfilesList";
import { StageDetailPanel } from "./stages/StageDetailPanel";
import { StagesList } from "./stages/StagesList";

interface StagesTabProps {
  projectId?: string | null;
}

export const StagesTab = memo(function StagesTab({ projectId }: StagesTabProps) {
  const [segment, setSegment] = useState<StageSegment>("stages");
  const [search, setSearch] = useState("");
  const [stages, setStages] = useState<StageEntry[]>([]);
  const [profiles, setProfiles] = useState<BuildProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedStageName, setSelectedStageName] = useState<string | null>(null);
  const [selectedProfileKey, setSelectedProfileKey] = useState<string | null>(null);
  const [creatingProfile, setCreatingProfile] = useState(false);
  const [busyStageName, setBusyStageName] = useState<string | null>(null);
  const [busyProfileKey, setBusyProfileKey] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());

  const refreshData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await loadStagesTabData(projectId);
      setStages(data.stages);
      setProfiles(data.profiles);
      setSelectedStageName((current) =>
        current && data.stages.some((stage) => stage.name === current)
          ? current
          : data.stages[0]?.name ?? null,
      );
      setSelectedProfileKey((current) =>
        current && data.profiles.some((profile) => profileKey(profile) === current)
          ? current
          : data.profiles[0]
            ? profileKey(data.profiles[0])
            : null,
      );
      return data;
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Failed to load stages");
      return null;
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void refreshData();
  }, [refreshData]);

  const filteredStages = useMemo(() => filterStages(stages, search), [stages, search]);
  const filteredProfiles = useMemo(() => filterProfiles(profiles, search), [profiles, search]);
  const currentStageNames = useMemo(() => stageNames(stages), [stages]);
  const selectedStage = useMemo(
    () => stages.find((stage) => stage.name === selectedStageName) ?? null,
    [selectedStageName, stages],
  );
  const selectedProfile = useMemo(
    () => profiles.find((profile) => profileKey(profile) === selectedProfileKey) ?? null,
    [profiles, selectedProfileKey],
  );

  const handleSegmentChange = (next: StageSegment) => {
    confirmLeaveRef.current(() => {
      setSegment(next);
      if (next === "stages") setCreatingProfile(false);
    });
  };

  const handleStageSelect = (stage: StageEntry) => {
    confirmLeaveRef.current(() => {
      setCreatingProfile(false);
      setSelectedStageName(stage.name);
    });
  };

  const handleProfileSelect = (profile: BuildProfile) => {
    confirmLeaveRef.current(() => {
      setCreatingProfile(false);
      setSelectedProfileKey(profileKey(profile));
    });
  };

  const handleCreateProfile = () => {
    confirmLeaveRef.current(() => {
      setSegment("profiles");
      setCreatingProfile(true);
      setSelectedProfileKey(null);
    });
  };

  const withStageBusy = async (stage: StageEntry, action: () => Promise<void>) => {
    setBusyStageName(stage.name);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusyStageName(null);
    }
  };

  const withProfileBusy = async (profile: BuildProfile, action: () => Promise<void>) => {
    const key = profileKey(profile);
    setBusyProfileKey(key);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusyProfileKey(null);
    }
  };

  const handleStageSave = useCallback(
    async (draft: StageEntry) => {
      const saved = await saveStageDraft(draft);
      await refreshData();
      setSelectedStageName(saved.name);
      return true;
    },
    [refreshData],
  );

  const handleProfileSave = useCallback(
    async (draft: BuildProfile, creating: boolean) => {
      const saved = await saveProfileDraft(draft, creating);
      setCreatingProfile(false);
      await refreshData();
      setSelectedProfileKey(profileKey(saved));
      return true;
    },
    [refreshData],
  );

  const hasDetail =
    segment === "stages" ? Boolean(selectedStage) : Boolean(creatingProfile || selectedProfile);

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border bg-[var(--bg-secondary)] px-3 py-2">
        <ActivityPanelSearch
          value={search}
          onChange={setSearch}
          placeholder={segment === "stages" ? "Search stages..." : "Search profiles..."}
          ariaLabel="Search stages and profiles"
        />
        <SegmentedControl<StageSegment>
          options={STAGE_SEGMENT_OPTIONS}
          value={segment}
          onChange={handleSegmentChange}
          ariaLabel="Stage surface"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
        {segment === "profiles" && (
          <button
            type="button"
            className="ml-auto min-h-8 rounded-md bg-accent px-3 text-xs font-medium text-accent-foreground transition-colors hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            onClick={handleCreateProfile}
          >
            + Profile
          </button>
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
      <div className="flex min-h-0 flex-1 flex-col">
        <div
          className="min-h-0 overflow-y-auto"
          style={hasDetail ? { height: `${topHeight}%` } : undefined}
        >
          {loading ? (
            <ActivityPanelEmpty heading="Stages" body="Loading stages and profiles..." />
          ) : segment === "stages" ? (
            filteredStages.length === 0 ? (
              <ActivityPanelEmpty
                icon={<TasksEmptyIcon />}
                heading="No stages"
                body="No stage registry rows match the current filters."
              />
            ) : (
              <StagesList
                stages={filteredStages}
                selectedName={selectedStageName}
                busyName={busyStageName}
                onSelect={handleStageSelect}
                onDelete={(stage) =>
                  void withStageBusy(stage, async () => {
                    await deleteStage(stage);
                    await refreshData();
                  })
                }
                onRestore={(stage) =>
                  void withStageBusy(stage, async () => {
                    await restoreStage(stage);
                    await refreshData();
                  })
                }
              />
            )
          ) : filteredProfiles.length === 0 ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="No profiles"
              body="No build profiles match the current filters."
            />
          ) : (
            <ProfilesList
              profiles={filteredProfiles}
              selectedKey={selectedProfileKey}
              busyKey={busyProfileKey}
              onSelect={handleProfileSelect}
              onSetDefault={(profile) =>
                void withProfileBusy(profile, async () => {
                  const saved = await setProfileAsDefault(profile, projectId);
                  await refreshData();
                  setSelectedProfileKey(profileKey(saved));
                })
              }
              onToggleEnabled={(profile) =>
                void withProfileBusy(profile, async () => {
                  await setProfileEnabled(profile, !profile.enabled);
                  await refreshData();
                })
              }
              onDelete={(profile) =>
                void withProfileBusy(profile, async () => {
                  await deleteProfile(profile);
                  await refreshData();
                })
              }
              onRestore={(profile) =>
                void withProfileBusy(profile, async () => {
                  await restoreProfile(profile);
                  await refreshData();
                })
              }
            />
          )}
        </div>
        {hasDetail && (
          <>
            <ResizeHandle
              direction="vertical"
              panelHeight={topHeight}
              minHeight={25}
              maxHeight={75}
              onResize={setTopHeight}
            />
            <div className="min-h-0 flex-1">
              {segment === "stages" ? (
                <StageDetailPanel
                  stage={selectedStage}
                  onSave={handleStageSave}
                  onError={setError}
                  onConfirmLeaveChange={(handler) => {
                    confirmLeaveRef.current = handler;
                  }}
                />
              ) : (
                <ProfileDetailPanel
                  profile={selectedProfile}
                  creating={creatingProfile}
                  projectId={projectId}
                  stageNames={currentStageNames}
                  onSave={handleProfileSave}
                  onError={setError}
                  onConfirmLeaveChange={(handler) => {
                    confirmLeaveRef.current = handler;
                  }}
                />
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
});
