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
} from "./workflows-styles";
import { Heading } from '../shared/Heading'

type SourceFilter = "installed" | "project" | "templates" | "deleted";

interface StageEntry {
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

interface DefaultStageEntry {
  stage_name: string;
  position: number;
}

type ParsedDefaultStageLine =
  | { ok: true; stage: DefaultStageEntry }
  | { ok: false; error: string };

const CATEGORY_OPTIONS = [
  "discovery",
  "design",
  "implementation",
  "verification",
  "delivery",
];

function parseDefaultStageLine(line: string, index: number): ParsedDefaultStageLine {
  const separatorIndex = line.lastIndexOf(":");
  if (separatorIndex < 0) {
    return { ok: false, error: `Defaults line ${index + 1} needs stage:position` };
  }
  const stageName = line.slice(0, separatorIndex).trim();
  if (!stageName) {
    return { ok: false, error: `Defaults line ${index + 1} needs a stage name` };
  }
  const positionText = line.slice(separatorIndex + 1).trim();
  const position = Number(positionText);
  if (!Number.isInteger(position)) {
    return { ok: false, error: `Defaults line ${index + 1} needs an integer position` };
  }
  return { ok: true, stage: { stage_name: stageName, position } };
}

export function StagesTab({
  searchText,
  sourceFilter,
  refreshKey,
}: {
  searchText: string;
  sourceFilter: SourceFilter;
  refreshKey: number;
}) {
  const [stages, setStages] = useState<StageEntry[]>([]);
  const [selected, setSelected] = useState<StageEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/stages/registry?include_deleted=true");
      if (!res.ok) throw new Error(`Failed to load stages (${res.status})`);
      const data = await res.json();
      setStages(data.stages ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load stages");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) {
        void load();
      }
    });
    return () => {
      cancelled = true;
    };
  }, [load, refreshKey]);

  const filtered = useMemo(() => {
    const q = searchText.trim().toLowerCase();
    return stages
      .filter((stage) =>
        sourceFilter === "deleted" ? stage.deleted_at : !stage.deleted_at,
      )
      .filter((stage) => {
        if (!q) return true;
        return `${stage.name} ${stage.display_label} ${stage.description} ${stage.category}`
          .toLowerCase()
          .includes(q);
      });
  }, [stages, sourceFilter, searchText]);

  const mutate = async (url: string, init: RequestInit) => {
    const res = await fetch(url, init);
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new Error(data?.detail ?? `Request failed (${res.status})`);
    }
    await load();
  };

  return (
    <div className={`${WORKFLOWS_CONTENT_CLS} grid grid-cols-[minmax(0,1fr)_360px] gap-3 max-lg:grid-cols-1`}>
      <div>
        {loading && <div className={WORKFLOWS_LOADING_CLS}>Loading stages...</div>}
        {error && <div className={WORKFLOWS_EMPTY_CLS}>{error}</div>}
        {!loading && !error && filtered.length === 0 && (
          <div className={WORKFLOWS_EMPTY_CLS}>No stages</div>
        )}
        <div className={WORKFLOWS_GRID_CLS}>
          {filtered.map((stage) => (
            <button
              key={stage.name}
              type="button"
              className={`${WORKFLOWS_CARD_CLS} text-left ${stage.deleted_at ? WORKFLOWS_CARD_DELETED_CLS : ""}`}
              onClick={() => setSelected(stage)}
            >
              <div className={WORKFLOWS_CARD_HEADER_CLS}>
                <span
                  className={`${WORKFLOWS_CARD_NAME_CLS} ${stage.deleted_at ? WORKFLOWS_CARD_NAME_DELETED_CLS : ""}`}
                >
                  {stage.display_label}
                </span>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{stage.name}</span>
              </div>
              <div className={WORKFLOWS_CARD_DESC_CLS}>{stage.description}</div>
              <div className={WORKFLOWS_CARD_BADGES_CLS}>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{stage.category}</span>
                <span className={WORKFLOWS_CARD_BADGE_CLS}>{stage.review_policy}</span>
                {stage.is_edited && (
                  <span className={WORKFLOWS_CARD_BADGE_DRIFT_CLS}>edited</span>
                )}
                {stage.deleted_at && (
                  <span className={WORKFLOWS_CARD_BADGE_CLS}>deleted</span>
                )}
              </div>
            </button>
          ))}
        </div>
      </div>
      <StageEditor
        key={selected?.name ?? "empty"}
        stage={selected}
        onSaved={load}
        onRestore={(name) =>
          mutate(`/api/stages/registry/${encodeURIComponent(name)}/restore`, {
            method: "POST",
          })
        }
        onDelete={(name) =>
          mutate(`/api/stages/registry/${encodeURIComponent(name)}`, {
            method: "DELETE",
          })
        }
      />
    </div>
  );
}

function StageEditor({
  stage,
  onSaved,
  onRestore,
  onDelete,
}: {
  stage: StageEntry | null;
  onSaved: () => Promise<void>;
  onRestore: (name: string) => Promise<void>;
  onDelete: (name: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState<StageEntry | null>(stage);
  const [taskType, setTaskType] = useState("task");
  const [defaults, setDefaults] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/task-types/${encodeURIComponent(taskType)}/default-stages`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load defaults (${res.status})`);
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        const rows = (data?.stages ?? []) as DefaultStageEntry[];
        setDefaults(rows.map((row) => `${row.stage_name}:${row.position}`).join("\n"));
        setError(null);
      })
      .catch((err) => {
        console.error("Failed to load stage defaults", err);
        if (!cancelled) {
          setDefaults("");
          setError(err instanceof Error ? err.message : "Failed to load defaults");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskType]);

  if (!draft) {
    return <aside className={WORKFLOWS_CARD_CLS}>Select a stage</aside>;
  }

  const setField = (key: keyof StageEntry, value: string | number | boolean | null) => {
    setDraft({ ...draft, [key]: value });
  };

  const save = async () => {
    setError(null);
    const payload = {
      display_label: draft.display_label,
      description: draft.description,
      category: draft.category,
      default_agent: draft.default_agent || null,
      reviewer_agent: draft.reviewer_agent || null,
      reviewer_agent_selector_json: draft.reviewer_agent_selector_json || null,
      review_policy: draft.review_policy,
      dispatch_type: draft.dispatch_type || null,
      dispatch_target: draft.dispatch_target || null,
      dispatch_inputs_json: draft.dispatch_inputs_json || null,
      position_hint: draft.position_hint,
      requires_human: draft.requires_human,
      is_terminal: draft.is_terminal,
      default_max_work_attempts: draft.default_max_work_attempts,
      default_max_review_rounds: draft.default_max_review_rounds,
    };
    const res = await fetch(`/api/stages/registry/${encodeURIComponent(draft.name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Save failed");
      return;
    }
    await onSaved();
  };

  const saveDefaults = async () => {
    setError(null);
    const stages: { stage_name: string; position: number }[] = [];
    const lines = defaults
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    for (const [index, line] of lines.entries()) {
      const parsed = parseDefaultStageLine(line, index);
      if (!parsed.ok) {
        setError(parsed.error);
        return;
      }
      stages.push(parsed.stage);
    }
    const res = await fetch(`/api/task-types/${encodeURIComponent(taskType)}/default-stages`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stages }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      setError(data?.detail ?? "Defaults save failed");
      return;
    }
    const data = await res.json().catch(() => null);
    const rows = (data?.stages ?? []) as DefaultStageEntry[];
    setDefaults(rows.map((row) => `${row.stage_name}:${row.position}`).join("\n"));
  };

  return (
    <aside className={`${WORKFLOWS_CARD_CLS} overflow-y-auto`}>
      <div className={WORKFLOWS_CARD_HEADER_CLS}>
        <Heading level={2} className={WORKFLOWS_CARD_NAME_CLS}>{draft.name}</Heading>
      </div>
      {error && <div className="mb-2 text-sm text-[var(--color-error)]">{error}</div>}
      <label>
        <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Label</span>
        <input
          className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
          value={draft.display_label}
          onChange={(e) => setField("display_label", e.target.value)}
        />
      </label>
      <label>
        <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Category</span>
        <select
          className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
          value={draft.category}
          onChange={(e) => setField("category", e.target.value)}
        >
          {CATEGORY_OPTIONS.map((category) => (
            <option key={category}>{category}</option>
          ))}
        </select>
      </label>
      <label>
        <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Description</span>
        <textarea
          className={WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS}
          value={draft.description}
          onChange={(e) => setField("description", e.target.value)}
        />
      </label>
      <label>
        <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Dispatch Inputs JSON</span>
        <textarea
          className={WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS}
          value={draft.dispatch_inputs_json ?? ""}
          onChange={(e) => setField("dispatch_inputs_json", e.target.value || null)}
        />
      </label>
      <div className={WORKFLOWS_CARD_FOOTER_CLS}>
        <button type="button" className={WORKFLOWS_MODAL_SUBMIT_CLS} onClick={save}>
          Save
        </button>
        <button
          type="button"
          className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_RESTORE_CLS}`}
          onClick={() => void onRestore(draft.name)}
        >
          Restore
        </button>
        {!draft.deleted_at && (
          <button
            type="button"
            className={`${WORKFLOWS_ACTION_BTN_CLS} ${WORKFLOWS_ACTION_BTN_DANGER_CLS}`}
            onClick={() => void onDelete(draft.name)}
          >
            Delete
          </button>
        )}
      </div>
      <div className="mt-4 border-t border-border pt-3">
        <label>
          <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Task Type</span>
          <input
            className={WORKFLOWS_MODAL_FIELD_INPUT_CLS}
            value={taskType}
            onChange={(e) => setTaskType(e.target.value)}
          />
        </label>
        <label>
          <span className={WORKFLOWS_MODAL_FIELD_LABEL_CLS}>Defaults</span>
          <textarea
            className={WORKFLOWS_MODAL_FIELD_TEXTAREA_CLS}
            value={defaults}
            onChange={(e) => setDefaults(e.target.value)}
          />
        </label>
        <button type="button" className={WORKFLOWS_MODAL_SUBMIT_CLS} onClick={saveDefaults}>
          Save Defaults
        </button>
      </div>
    </aside>
  );
}
