import { useCallback, useEffect, useMemo, useState } from "react";
import {
  WORKFLOWS_ACTION_BTN_CLS,
  WORKFLOWS_ACTION_BTN_DANGER_CLS,
  WORKFLOWS_ACTION_BTN_RESTORE_CLS,
  WORKFLOWS_CARD_BADGE_CLS,
  WORKFLOWS_CARD_BADGE_DRIFT_CLS,
  WORKFLOWS_CARD_BADGES_CLS,
  WORKFLOWS_CARD_CLS,
  WORKFLOWS_CARD_DELETED_CLS,
  WORKFLOWS_CARD_DESC_CLS,
  WORKFLOWS_CARD_FOOTER_CLS,
  WORKFLOWS_CARD_HEADER_CLS,
  WORKFLOWS_CARD_NAME_CLS,
  WORKFLOWS_CARD_NAME_DELETED_CLS,
  WORKFLOWS_CONTENT_CLS,
  WORKFLOWS_EMPTY_CLS,
  WORKFLOWS_GRID_CLS,
  WORKFLOWS_LOADING_CLS,
  WORKFLOWS_MODAL_FIELD_INPUT_CLS,
  WORKFLOWS_MODAL_FIELD_LABEL_CLS,
  WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS,
  WORKFLOWS_MODAL_SUBMIT_CLS,
  WORKFLOWS_TOGGLE_KNOB_CLS,
  WORKFLOWS_TOGGLE_KNOB_ON_CLS,
  WORKFLOWS_TOGGLE_TRACK_CLS,
  WORKFLOWS_TOGGLE_TRACK_ON_CLS,
} from "./workflows-styles";

type SourceFilter = "installed" | "project" | "templates" | "deleted";
type ProfileSource = "installed" | "project";
type Isolation = "none" | "worktree" | "clone";

interface BuildProfile {
  id: string;
  name: string;
  display_label: string;
  description: string;
  skip_stages: string[];
  isolation: Isolation;
  unattended: boolean;
  enabled: boolean;
  source: ProfileSource;
  project_id: string | null;
  tags: string[] | null;
  deleted_at: string | null;
  state: "bundled" | "edited" | "custom" | "deleted";
}

export function ProfilesTab({
  searchText,
  sourceFilter,
  refreshKey,
  projectId,
}: {
  searchText: string;
  sourceFilter: SourceFilter;
  refreshKey: number;
  projectId?: string;
}) {
  const [profiles, setProfiles] = useState<BuildProfile[]>([]);
  const [selected, setSelected] = useState<BuildProfile | null>(null);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ include_deleted: "true" });
      if (projectId) params.set("project_id", projectId);
      const res = await fetch(`/api/profiles?${params}`);
      if (!res.ok) throw new Error(`Failed to load profiles (${res.status})`);
      const data = await res.json();
      setProfiles(data.profiles ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load profiles");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load, refreshKey]);

  const filtered = useMemo(() => {
    const q = searchText.trim().toLowerCase();
    return profiles
      .filter((profile) => {
        if (sourceFilter === "deleted") return profile.deleted_at;
        if (profile.deleted_at) return false;
        return profile.source === sourceFilter;
      })
      .filter((profile) => {
        if (!q) return true;
        return `${profile.name} ${profile.display_label} ${profile.description}`
          .toLowerCase()
          .includes(q);
      });
  }, [profiles, sourceFilter, searchText]);

  const globalNames = useMemo(
    () =>
      new Set(
        profiles
          .filter((profile) => profile.source === "installed" && !profile.deleted_at)
          .map((profile) => profile.name),
      ),
    [profiles],
  );

  const toggleEnabled = async (profile: BuildProfile) => {
    const action = profile.enabled ? "disable" : "enable";
    const params = new URLSearchParams({ source: profile.source });
    if (profile.project_id) params.set("project_id", profile.project_id);
    const res = await fetch(`/api/profiles/${profile.name}/${action}?${params}`, {
      method: "POST",
    });
    if (!res.ok) return;
    await load();
  };

  return (
    <div className={`${WORKFLOWS_CONTENT_CLS} grid grid-cols-[minmax(0,1fr)_360px] gap-3 max-lg:grid-cols-1`}>
      <div>
        <div className="mb-3 flex justify-end">
          <button
            type="button"
            className={WORKFLOWS_MODAL_SUBMIT_CLS}
            onClick={() => {
              setCreating(true);
              setSelected(null);
            }}
          >
            New Profile
          </button>
        </div>
        {loading && <div className={WORKFLOWS_LOADING_CLS}>Loading profiles...</div>}
        {error && <div className={WORKFLOWS_EMPTY_CLS}>{error}</div>}
        {!loading && !error && filtered.length === 0 && (
          <div className={WORKFLOWS_EMPTY_CLS}>No profiles</div>
        )}
        <div className={WORKFLOWS_GRID_CLS}>
          {filtered.map((profile) => (
            <button
              key={`${profile.source}:${profile.project_id ?? "global"}:${profile.name}`}
              type="button"
              className={`${WORKFLOWS_CARD_CLS} text-left ${profile.deleted_at ? WORKFLOWS_CARD_DELETED_CLS : ""}`}
              onClick={() => {
                setCreating(false);
                setSelected(profile);
              }}
            >
              <div className={WORKFLOWS_CARD_HEADER_CLS}>
                <span
                  className={`${WORKFLOWS_CARD_NAME_CLS} ${profile.deleted_at ? WORKFLOWS_CARD_NAME_DELETED_CLS : ""}`}
                >
                  {profile.display_label}
                </span>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{profile.name}</span>
              </div>
              <div className={WORKFLOWS_CARD_DESC_CLS}>{profile.description}</div>
              <div className={WORKFLOWS_CARD_BADGES_CLS}>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{profile.source}</span>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{profile.isolation}</span>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>
                  {profile.unattended ? "unattended" : "attended"}
                </span>
                <span
                  className={
                    profile.state === "edited"
                      ? WORKFLOWS_CARD_BADGE_DRIFT_CLS
                      : WORKFLOWS_CARD_BADGE_CLS
                  }
                >
                  {profile.state}
                </span>
              </div>
              {profile.source === "project" && globalNames.has(profile.name) && (
                <div className="text-xs text-[var(--text-secondary)]">
                  Overrides global "{profile.name}"
                </div>
              )}
              <div className={WORKFLOWS_CARD_FOOTER_CLS}>
                <span className="text-xs text-[var(--text-secondary)]">
                  skips {profile.skip_stages.length}
                </span>
                <span
                  className={`${WORKFLOWS_TOGGLE_TRACK_CLS} ${profile.enabled ? WORKFLOWS_TOGGLE_TRACK_ON_CLS : ""}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    void toggleEnabled(profile);
                  }}
                >
                  <span
                    className={`${WORKFLOWS_TOGGLE_KNOB_CLS} ${profile.enabled ? WORKFLOWS_TOGGLE_KNOB_ON_CLS : ""}`}
                  />
                </span>
              </div>
            </button>
          ))}
        </div>
      </div>
      <ProfileEditor
        key={creating ? "new" : selected?.id ?? "empty"}
        profile={selected}
        creating={creating}
        projectId={projectId}
        onSaved={async () => {
          setCreating(false);
          await load();
        }}
      />
    </div>
  );
}

function ProfileEditor({
  profile,
  creating,
  projectId,
  onSaved,
}: {
  profile: BuildProfile | null;
  creating: boolean;
  projectId?: string;
  onSaved: () => Promise<void>;
}) {
  const empty: BuildProfile = {
    id: "",
    name: "",
    display_label: "",
    description: "",
    skip_stages: [],
    isolation: "worktree",
    unattended: false,
    enabled: true,
    source: "project",
    project_id: projectId ?? null,
    tags: [],
    deleted_at: null,
    state: "custom",
  };
  const [draft, setDraft] = useState<BuildProfile>(profile ?? empty);
  const [error, setError] = useState<string | null>(null);

  if (!profile && !creating) {
    return <aside className={WORKFLOWS_CARD_CLS}>Select a profile</aside>;
  }

  const setField = (key: keyof BuildProfile, value: string | boolean | string[] | null) => {
    setDraft({ ...draft, [key]: value });
  };

  const save = async () => {
    setError(null);
    const body = {
      name: draft.name,
      display_label: draft.display_label,
      description: draft.description,
      skip_stages: draft.skip_stages,
      isolation: draft.isolation,
      unattended: draft.unattended,
      enabled: draft.enabled,
      source: draft.source,
      project_id: draft.project_id,
      tags: draft.tags ?? [],
    };
    const params = new URLSearchParams({ source: draft.source });
    if (draft.project_id) params.set("project_id", draft.project_id);
    const url = creating
      ? "/api/profiles"
      : `/api/profiles/${encodeURIComponent(draft.name)}?${params}`;
    const res = await fetch(url, {
      method: creating ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(creating ? body : { ...body, name: undefined, source: undefined }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Save failed");
      return;
    }
    await onSaved();
  };

  const action = async (name: string, suffix: string, method = "POST") => {
    const params = new URLSearchParams({ source: draft.source });
    if (draft.project_id) params.set("project_id", draft.project_id);
    const res = await fetch(`/api/profiles/${encodeURIComponent(name)}${suffix}?${params}`, {
      method,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Request failed");
      return;
    }
    await onSaved();
  };

  return (
    <aside className={`${WORKFLOWS_CARD_CLS} overflow-y-auto`}>
      <div className={WORKFLOWS_CARD_HEADER_CLS}>
        <h2 className={WORKFLOWS_CARD_NAME_CLS}>{creating ? "New Profile" : draft.name}</h2>
      </div>
      {error && <div className="mb-2 text-sm text-[var(--color-error)]">{error}</div>}
      <label className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Name</label>
      <input
        className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
        value={draft.name}
        disabled={!creating}
        onChange={(e) => setField("name", e.target.value)}
      />
      <label className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Label</label>
      <input
        className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
        value={draft.display_label}
        onChange={(e) => setField("display_label", e.target.value)}
      />
      <label className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Description</label>
      <textarea
        className={WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS}
        value={draft.description}
        onChange={(e) => setField("description", e.target.value)}
      />
      <label className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Skip Stages</label>
      <input
        className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
        value={draft.skip_stages.join(",")}
        onChange={(e) =>
          setField(
            "skip_stages",
            e.target.value
              .split(",")
              .map((item) => item.trim())
              .filter(Boolean),
          )
        }
      />
      <label className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Isolation</label>
      <select
        className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
        value={draft.isolation}
        onChange={(e) => setField("isolation", e.target.value as Isolation)}
      >
        <option>none</option>
        <option>worktree</option>
        <option>clone</option>
      </select>
      <div className="mt-3 flex gap-3">
        <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
          <input
            type="checkbox"
            checked={draft.unattended}
            onChange={(e) => setField("unattended", e.target.checked)}
          />
          Unattended
        </label>
        <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => setField("enabled", e.target.checked)}
          />
          Enabled
        </label>
      </div>
      <div className={WORKFLOWS_CARD_FOOTER_CLS}>
        <button type="button" className={WORKFLOWS_MODAL_SUBMIT_CLS} onClick={save}>
          Save
        </button>
        {!creating && draft.state !== "custom" && (
          <button
            type="button"
            className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_RESTORE_CLS}`}
            onClick={() => void action(draft.name, "/restore")}
          >
            Restore
          </button>
        )}
        {!creating && (
          <button
            type="button"
            className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_DANGER_CLS}`}
            onClick={() => void action(draft.name, "", "DELETE")}
          >
            Delete
          </button>
        )}
      </div>
    </aside>
  );
}
