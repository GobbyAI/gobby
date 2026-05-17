import { describe, expect, it } from "vitest";

import type { GobbyTask } from "../../../../hooks/useTasks";
import {
  computeTaskDetail,
  formatTaskDetailDate,
  ownerDisplay,
  stageLabel,
  stageVariant,
} from "../taskDetailFormat";
import { makeTask } from "../../__tests__/fixtures";

describe("taskDetailFormat — owner identity (#14772 / D5)", () => {
  it("prefers a human agent name, rendered non-mono", () => {
    const owner = ownerDisplay(makeTask({ agent_name: "  codex  " }));
    expect(owner).toEqual({ label: "codex", mono: false, source: null });
  });

  it("falls back to the friendly session ref, never the raw UUID", () => {
    const task = makeTask({
      agent_name: null,
      claimed_by_session_id: "11111111-2222-3333-4444-555555555555",
      owner_session_ref: {
        session_id: "11111111-2222-3333-4444-555555555555",
        ref: "#5122",
        source: "claude",
      },
    }) as GobbyTask;
    const owner = ownerDisplay(task);
    expect(owner).toEqual({ label: "#5122", mono: true, source: "claude" });
    expect(owner.label).not.toContain("11111111-2222");
  });

  it("degrades to a short hash (not the full UUID) for legacy payloads", () => {
    const owner = ownerDisplay(
      makeTask({
        agent_name: null,
        claimed_by_session_id: "11111111-2222-3333-4444-555555555555",
      }),
    );
    expect(owner.label).toBe("11111111");
    expect(owner.mono).toBe(true);
  });

  it("returns a null label when there is no owner", () => {
    expect(ownerDisplay(makeTask()).label).toBeNull();
  });
});

describe("taskDetailFormat — stage + dates (#14772 / D5)", () => {
  it("dashes a missing/invalid date", () => {
    expect(formatTaskDetailDate(null)).toBe("—");
    expect(formatTaskDetailDate("not-a-date")).toBe("—");
  });

  it("marks escalation as the escalated variant", () => {
    expect(stageVariant(true, "in_progress")).toBe("escalated");
    expect(stageVariant(false, "in_progress")).toBe("active");
    expect(stageVariant(false, "blocked")).toBe("blocked");
    expect(stageVariant(false, "closed")).toBe("closed");
    expect(stageVariant(false, "ready")).toBe("default");
  });

  it("labels an escalated task 'Escalated' and a closed task 'Closed'", () => {
    const escalated = makeTask({ escalated_at: "2026-05-16T01:00:00Z" });
    const c = computeTaskDetail(escalated);
    expect(c.isEscalated).toBe(true);
    expect(c.stageLabel).toBe("Escalated");
    expect(c.stageVariant).toBe("escalated");

    const closed = makeTask({ closed_at: "2026-05-16T01:00:00Z" });
    const cs = computeTaskDetail(closed);
    expect(
      stageLabel(closed, cs.taskState, cs.displayState, cs.isEscalated),
    ).toBe("Closed");
  });
});
