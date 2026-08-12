import type { BuildProfile, ProfileSource, StageEntry } from "./StagesTabData";

type StagePayload = Omit<StageEntry, "name" | "deleted_at" | "is_edited">;

type ProfilePayload = {
  name?: string;
  display_label: string;
  description: string;
  skip_stages: string[];
  isolation: BuildProfile["isolation"];
  unattended: boolean;
  delivery_mode: BuildProfile["delivery_mode"];
  delivery_target_repo: string | null;
  enabled: boolean;
  source?: ProfileSource;
  project_id: string | null;
  tags: string[];
};

async function sendJson<T>(
  url: string,
  method: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String(data.detail)
        : "Request failed";
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

function stagePayload(stage: StageEntry): StagePayload {
  return {
    display_label: stage.display_label,
    description: stage.description,
    category: stage.category,
    default_agent: stage.default_agent || null,
    reviewer_agent: stage.reviewer_agent || null,
    reviewer_agent_selector_json: stage.reviewer_agent_selector_json || null,
    review_policy: stage.review_policy,
    dispatch_type: stage.dispatch_type || null,
    dispatch_target: stage.dispatch_target || null,
    dispatch_inputs_json: stage.dispatch_inputs_json || null,
    position_hint: stage.position_hint,
    requires_human: stage.requires_human,
    is_terminal: stage.is_terminal,
    default_max_work_attempts: stage.default_max_work_attempts,
    default_max_review_rounds: stage.default_max_review_rounds,
  };
}

function profilePayload(
  profile: BuildProfile,
  includeIdentity: boolean,
): ProfilePayload {
  return {
    ...(includeIdentity ? { name: profile.name, source: profile.source } : {}),
    display_label: profile.display_label,
    description: profile.description,
    skip_stages: profile.skip_stages,
    isolation: profile.isolation,
    unattended: profile.unattended,
    delivery_mode: profile.delivery_mode,
    delivery_target_repo: profile.delivery_target_repo || null,
    enabled: profile.enabled,
    project_id: profile.project_id,
    tags: profile.tags ?? [],
  };
}

function profileParams(profile: BuildProfile): URLSearchParams {
  const params = new URLSearchParams({ source: profile.source });
  if (profile.project_id) params.set("project_id", profile.project_id);
  return params;
}

export async function saveStageDraft(stage: StageEntry): Promise<StageEntry> {
  return sendJson<StageEntry>(
    `/api/stages/registry/${encodeURIComponent(stage.name)}`,
    "PUT",
    stagePayload(stage),
  );
}

export async function deleteStage(stage: StageEntry): Promise<void> {
  await sendJson(
    `/api/stages/registry/${encodeURIComponent(stage.name)}`,
    "DELETE",
  );
}

export async function restoreStage(stage: StageEntry): Promise<void> {
  await sendJson(
    `/api/stages/registry/${encodeURIComponent(stage.name)}/restore`,
    "POST",
  );
}

export async function saveProfileDraft(
  profile: BuildProfile,
  creating: boolean,
): Promise<BuildProfile> {
  if (creating) {
    return sendJson<BuildProfile>(
      "/api/profiles",
      "POST",
      profilePayload(profile, true),
    );
  }
  const params = profileParams(profile);
  return sendJson<BuildProfile>(
    `/api/profiles/${encodeURIComponent(profile.name)}?${params}`,
    "PUT",
    profilePayload(profile, false),
  );
}

export async function setProfileEnabled(
  profile: BuildProfile,
  enabled: boolean,
): Promise<void> {
  const params = profileParams(profile);
  await sendJson(
    `/api/profiles/${encodeURIComponent(profile.name)}/${enabled ? "enable" : "disable"}?${params}`,
    "POST",
  );
}

export async function deleteProfile(profile: BuildProfile): Promise<void> {
  const params = profileParams(profile);
  await sendJson(
    `/api/profiles/${encodeURIComponent(profile.name)}?${params}`,
    "DELETE",
  );
}

export async function restoreProfile(profile: BuildProfile): Promise<void> {
  const params = profileParams(profile);
  await sendJson(
    `/api/profiles/${encodeURIComponent(profile.name)}/restore?${params}`,
    "POST",
  );
}

export async function setProfileAsDefault(
  profile: BuildProfile,
  projectId?: string | null,
): Promise<BuildProfile> {
  return sendJson<BuildProfile>("/api/profiles", "POST", {
    ...profilePayload(profile, true),
    name: "default",
    display_label: "Default",
    source: "project",
    project_id: projectId ?? profile.project_id ?? null,
  });
}
