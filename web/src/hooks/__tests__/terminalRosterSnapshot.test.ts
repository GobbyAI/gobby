import { describe, expect, it } from "vitest";
import { applyRosterPage } from "../terminalRosterSnapshot";

describe("native terminal roster", () => {
  it("exposes the daemon's command and cwd in the terminal list", () => {
    const page = applyRosterPage(null, {
      request_id: "init",
      items: [
        { terminal_id: "native-1", command: "zsh", cwd: "/workspace/project" },
      ],
    });

    expect(page?.rows[0].pane_command).toBe("zsh");
    expect(page?.rows[0].pane_path).toBe("/workspace/project");
  });
});
