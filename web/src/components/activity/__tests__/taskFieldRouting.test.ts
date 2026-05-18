import { describe, expect, it } from "vitest";

import {
  classifyEditableField,
  isPatchEditableField,
  PATCH_EDITABLE_FIELDS,
} from "../taskFieldRouting";

const legacyStageField = "lifecycle_stage";

describe("taskFieldRouting — endpoint family classification (#14771 / D4)", () => {
  it("classifies every PATCH-safe field as the patch family", () => {
    for (const field of PATCH_EDITABLE_FIELDS) {
      expect(classifyEditableField(field)).toBe("patch");
      expect(isPatchEditableField(field)).toBe(true);
    }
  });

  it("never routes assignee through patch — the PATCH-400 guarantee", () => {
    expect(classifyEditableField("assignee")).toBe("assignee");
    expect(isPatchEditableField("assignee")).toBe(false);
  });

  it("routes state and stage to the stage family", () => {
    expect(classifyEditableField("state")).toBe("stage");
    expect(classifyEditableField("stage")).toBe("stage");
    expect(isPatchEditableField("state")).toBe(false);
  });

  it("routes lifecycle actions to the terminal family", () => {
    for (const action of ["close", "reopen", "escalate", "de-escalate"]) {
      expect(classifyEditableField(action)).toBe("terminal");
      expect(isPatchEditableField(action)).toBe(false);
    }
  });

  it("returns null for unknown / legacy fields (never PATCH-by-default)", () => {
    expect(classifyEditableField("status")).toBeNull();
    expect(classifyEditableField(legacyStageField)).toBeNull();
    expect(classifyEditableField("nonsense")).toBeNull();
  });
});
