import { describe, expect, it } from "vitest";

import {
  getPreferredModelForProvider,
  resolveProviderModelPair,
  type ProviderModelEntry,
} from "../providerModels";

const catalog: ProviderModelEntry[] = [
  {
    provider: "claude",
    available: true,
    source: "live",
    models: [
      {
        value: "haiku",
        label: "Haiku",
        canonical_id: "claude-haiku-4-5-20251001",
      },
      {
        value: "sonnet",
        label: "Sonnet",
        canonical_id: "claude-sonnet-4-6",
      },
      {
        value: "opus",
        label: "Opus",
        canonical_id: "claude-opus-4-6",
      },
    ],
  },
];

describe("providerModels", () => {
  it("normalizes canonical model identifiers to the provider alias", () => {
    expect(
      getPreferredModelForProvider(catalog, "claude", "claude-opus-4-6"),
    ).toBe("opus");
  });

  it("resolves provider/model pairs using canonical model identifiers", () => {
    expect(
      resolveProviderModelPair(catalog, {
        provider: "claude",
        model: "claude-opus-4-6",
      }),
    ).toEqual({
      provider: "claude",
      model: "opus",
    });
  });
});
