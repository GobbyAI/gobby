import { describe, expect, it } from "vitest";

import {
  LIVE_CONTEXT_USAGE_MAX_ENTRIES,
  LIVE_CONTEXT_USAGE_TTL_MS,
  pruneLiveContextUsageEntries,
  type LiveContextUsageEntry,
} from "../useChat/contextUsageState";

describe("context usage live freshness pruning", () => {
  it("drops stale entries by TTL", () => {
    const now = 1_000_000;
    const entries = new Map<string, LiveContextUsageEntry>([
      ["fresh", { usageTimestamp: now, lastSeenAt: now }],
      [
        "stale",
        {
          usageTimestamp: now - LIVE_CONTEXT_USAGE_TTL_MS - 1,
          lastSeenAt: now - LIVE_CONTEXT_USAGE_TTL_MS - 1,
        },
      ],
    ]);

    pruneLiveContextUsageEntries(entries, now);

    expect([...entries.keys()]).toEqual(["fresh"]);
  });

  it("evicts oldest live entries above the max size", () => {
    const entries = new Map<string, LiveContextUsageEntry>();
    for (let index = 0; index < LIVE_CONTEXT_USAGE_MAX_ENTRIES + 2; index += 1) {
      entries.set(`session-${index}`, {
        usageTimestamp: index,
        lastSeenAt: index,
      });
    }

    pruneLiveContextUsageEntries(entries, LIVE_CONTEXT_USAGE_TTL_MS);

    expect(entries.size).toBe(LIVE_CONTEXT_USAGE_MAX_ENTRIES);
    expect(entries.has("session-0")).toBe(false);
    expect(entries.has("session-1")).toBe(false);
  });
});
