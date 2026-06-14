export type StageSegment = "stages" | "profiles";
export type ProfileSource = "installed" | "project";
export type Isolation = "none" | "worktree" | "clone";
export type DeliveryMode = "auto" | "pull_request";

export interface StageEntry {
  name: string;
  display_label: string;
  description: string;
  category: string;
  default_agent: string | null;
  reviewer_agent: string | null;
  reviewer_agent_selector_json: string | null;
  review_policy: string;
  dispatch_type: string | null;
  dispatch_target: string | null;
  dispatch_inputs_json: string | null;
  position_hint: number;
  requires_human: boolean;
  is_terminal: boolean;
  default_max_work_attempts: number;
  default_max_review_rounds: number;
  deleted_at: string | null;
  is_edited: boolean;
}

export interface BuildProfile {
  id: string;
  name: string;
  display_label: string;
  description: string;
  skip_stages: string[];
  isolation: Isolation;
  unattended: boolean;
  delivery_mode: DeliveryMode;
  delivery_target_repo: string | null;
  enabled: boolean;
  source: ProfileSource;
  project_id: string | null;
  tags: string[] | null;
  deleted_at: string | null;
  state: "bundled" | "edited" | "custom" | "deleted";
}

export interface StagesTabData {
  stages: StageEntry[];
  profiles: BuildProfile[];
}

export const STAGE_SEGMENT_OPTIONS = [
  { value: "stages", label: "Stages" },
  { value: "profiles", label: "Profiles" },
] as const;

export const STAGE_CATEGORY_OPTIONS = [
  "discovery",
  "design",
  "implementation",
  "verification",
  "delivery",
].map((category) => ({ value: category, label: category }));

export const ISOLATION_OPTIONS = [
  { value: "none", label: "none" },
  { value: "worktree", label: "worktree" },
  { value: "clone", label: "clone" },
] as const;

export const DELIVERY_MODE_OPTIONS = [
  { value: "auto", label: "auto" },
  { value: "pull_request", label: "pull_request" },
] as const;

export const PROFILE_SOURCE_OPTIONS = [
  { value: "project", label: "project" },
  { value: "installed", label: "installed" },
] as const;

export function stageKey(stage: StageEntry): string {
  return stage.name;
}

export function profileKey(profile: BuildProfile): string {
  return `${profile.source}:${profile.project_id ?? "global"}:${profile.name}`;
}

export function createProfileDraft(projectId?: string | null): BuildProfile {
  return {
    id: "",
    name: "",
    display_label: "",
    description: "",
    skip_stages: [],
    isolation: "worktree",
    unattended: false,
    delivery_mode: "auto",
    delivery_target_repo: null,
    enabled: true,
    source: "project",
    project_id: projectId ?? null,
    tags: [],
    deleted_at: null,
    state: "custom",
  };
}

async function readJson<T>(response: Response, fallbackMessage: string): Promise<T> {
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail =
      data && typeof data === "object" && "detail" in data ? String(data.detail) : fallbackMessage;
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export async function loadStages(): Promise<StageEntry[]> {
  const response = await fetch("/api/stages/registry?include_deleted=true");
  const data = await readJson<{ stages?: StageEntry[] }>(response, "Failed to load stages");
  return data.stages ?? [];
}

export async function loadProfiles(projectId?: string | null): Promise<BuildProfile[]> {
  const params = new URLSearchParams({ include_deleted: "true" });
  if (projectId) params.set("project_id", projectId);
  const response = await fetch(`/api/profiles?${params}`);
  const data = await readJson<{ profiles?: BuildProfile[] }>(response, "Failed to load profiles");
  return data.profiles ?? [];
}

export async function loadStagesTabData(projectId?: string | null): Promise<StagesTabData> {
  const [stages, profiles] = await Promise.all([loadStages(), loadProfiles(projectId)]);
  return { stages, profiles };
}

export function filterStages(stages: StageEntry[], searchText: string): StageEntry[] {
  const query = searchText.trim().toLowerCase();
  return stages
    .filter((stage) => !stage.deleted_at)
    .filter((stage) => {
      if (!query) return true;
      return [
        stage.name,
        stage.display_label,
        stage.description,
        stage.category,
        stage.default_agent,
        stage.reviewer_agent,
        stage.review_policy,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
}

export function filterProfiles(profiles: BuildProfile[], searchText: string): BuildProfile[] {
  const query = searchText.trim().toLowerCase();
  return profiles
    .filter((profile) => !profile.deleted_at)
    .filter((profile) => {
      if (!query) return true;
      return [
        profile.name,
        profile.display_label,
        profile.description,
        profile.source,
        profile.isolation,
        profile.delivery_mode,
        ...(profile.tags ?? []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
}

export function stageNames(stages: StageEntry[]): Set<string> {
  return new Set(stages.filter((stage) => !stage.deleted_at).map((stage) => stage.name));
}
