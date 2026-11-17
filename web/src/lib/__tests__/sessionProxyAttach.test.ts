import { describe, expect, it } from "vitest";
import { canProxyAttachSessionRecord } from "../sessionProxyAttach";

describe("terminal session attachment identity", () => {
  it.each([
    [{ gobby_terminal_id: "native-terminal" }, true],
    [{ gobby_terminal_id: "" }, false],
    [{ gobby_terminal_id: 42 }, false],
    [{ parent_pid: 42 }, false],
    [{ parent_pid: "42" }, false],
    [{}, false],
    [null, false],
  ])(
    "requires a native terminal identity in %j",
    (terminal_context, expected) => {
      expect(
        canProxyAttachSessionRecord({
          session_type: "terminal",
          status: "awaiting_handoff",
          terminal_context,
        }),
      ).toBe(expected);
    },
  );

  it("honors an explicit daemon refusal even with a native identity", () => {
    expect(
      canProxyAttachSessionRecord({
        session_type: "terminal",
        status: "active",
        terminal_context: { gobby_terminal_id: "native-terminal" },
        can_proxy_attach: false,
      }),
    ).toBe(false);
  });
});
