import { describe, expect, it } from "vitest";

import { getSessionLabelLines, type SessionRosterLabel } from "../sessionLabel";

const NO_ROSTER: SessionRosterLabel = {
  definition: null,
  task: null,
  statusLine: null,
};

describe("derived session labels", () => {
  it("shows the ref alone when the roster names no definition", () => {
    expect(getSessionLabelLines({ ref: "gobby#12" }, null).head).toBe(
      "gobby#12",
    );
  });

  it("shows the ref and the definition name", () => {
    expect(
      getSessionLabelLines(
        { ref: "gobby#12" },
        { ...NO_ROSTER, definition: "backend-architect" },
      ).head,
    ).toBe("gobby#12: backend-architect");
  });

  it("trims the project from the ref when asked", () => {
    expect(
      getSessionLabelLines(
        { ref: "gobby#12" },
        { ...NO_ROSTER, definition: "dev-team-lead" },
        true,
      ).head,
    ).toBe("#12: dev-team-lead");
  });

  it("marks the claim holder in the task line", () => {
    const label = getSessionLabelLines(
      { ref: "gobby#12" },
      {
        ...NO_ROSTER,
        task: {
          ref: "#25767",
          title: "Web UI: replace titles",
          claimHolder: true,
        },
      },
    );
    expect(label.taskLine).toBe(
      "Working task 25767 claim Web UI: replace titles",
    );
  });

  it("names the task without the mark for seats that do not hold its claim", () => {
    const label = getSessionLabelLines(
      { ref: "gobby#13" },
      {
        ...NO_ROSTER,
        task: {
          ref: "#25767",
          title: "Web UI: replace titles",
          claimHolder: false,
        },
      },
    );
    expect(label.taskLine).toBe("Working task 25767 Web UI: replace titles");
  });

  it("drops the title from the task line when the task has none", () => {
    const label = getSessionLabelLines(
      { ref: "gobby#13" },
      {
        ...NO_ROSTER,
        task: { ref: "#25767", title: null, claimHolder: false },
      },
    );
    expect(label.taskLine).toBe("Working task 25767");
  });

  it("shows the status line only when no task is claimed", () => {
    const withTask = getSessionLabelLines(
      { ref: "gobby#12" },
      {
        ...NO_ROSTER,
        task: { ref: "#25767", title: "Title", claimHolder: true },
        statusLine: "Reviewing the diff",
      },
    );
    expect(withTask.statusLine).toBeNull();

    const withoutTask = getSessionLabelLines(
      { ref: "gobby#12" },
      { ...NO_ROSTER, statusLine: "  Reviewing the diff  " },
    );
    expect(withoutTask.taskLine).toBeNull();
    expect(withoutTask.statusLine).toBe("Reviewing the diff");
  });

  it("treats a blank status line as absent", () => {
    expect(
      getSessionLabelLines(
        { ref: "gobby#12" },
        { ...NO_ROSTER, statusLine: "   " },
      ).statusLine,
    ).toBeNull();
  });
});
