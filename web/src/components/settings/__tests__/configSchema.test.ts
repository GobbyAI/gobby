import { describe, it, expect } from "vitest";
import {
  enumOptionsAt,
  numberBoundsAt,
  resolveSchemaNode,
} from "../configSchema";

// Mirrors the real DaemonConfig schema shape: top-level `properties` whose
// nested object fields are `$ref`s into `$defs`, and leaf fields (like
// `profile`) that are themselves `$ref`s to an enum def.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    FeatureProfile: {
      enum: ["feature_low", "feature_mid", "feature_high"],
      type: "string",
    },
    RecommendToolsConfig: {
      type: "object",
      properties: {
        profile: {
          $ref: "#/$defs/FeatureProfile",
          default: "feature_mid",
          description: "Capability profile.",
        },
        candidates: { type: "array", items: { type: "string" } },
        enabled: { type: "boolean", default: true },
      },
    },
    AIConfig: {
      type: "object",
      properties: { generation: { $ref: "#/$defs/GenerationConfig" } },
    },
    GenerationEndpointConfig: {
      type: "object",
      additionalProperties: false,
      properties: {
        protocol: {
          enum: ["openai-compatible", "lmstudio", "ollama", "vllm"],
          type: "string",
        },
        wire_api: {
          enum: ["chat-completions", "responses"],
          type: "string",
        },
      },
    },
    GenerationConfig: {
      type: "object",
      properties: {
        timeout_seconds: {
          type: "number",
          default: 600,
          minimum: 1,
          maximum: 3600,
        },
        endpoints: {
          type: "object",
          additionalProperties: { $ref: "#/$defs/GenerationEndpointConfig" },
        },
      },
    },
  },
  type: "object",
  properties: {
    recommend_tools: {
      $ref: "#/$defs/RecommendToolsConfig",
      description: "tool rec",
    },
    ai: { $ref: "#/$defs/AIConfig" },
    context_window_overrides: {
      type: "object",
      additionalProperties: { type: "integer" },
    },
  },
};

describe("resolveSchemaNode", () => {
  it("resolves a leaf field whose own node is a $ref to an enum def", () => {
    const node = resolveSchemaNode(SCHEMA, "recommend_tools.profile");
    expect(node?.enum).toEqual(["feature_low", "feature_mid", "feature_high"]);
    // local overrides (description) win over the referenced def
    expect(node?.description).toBe("Capability profile.");
  });

  it("walks nested $ref objects to a deep scalar leaf", () => {
    const node = resolveSchemaNode(SCHEMA, "ai.generation.timeout_seconds");
    expect(node?.type).toBe("number");
    expect(node?.minimum).toBe(1);
    expect(node?.maximum).toBe(3600);
  });

  it("returns a top-level map node directly", () => {
    const node = resolveSchemaNode(SCHEMA, "context_window_overrides");
    expect(node?.type).toBe("object");
    expect(node?.additionalProperties).toEqual({ type: "integer" });
  });

  it("returns null for an unknown path", () => {
    expect(resolveSchemaNode(SCHEMA, "recommend_tools.nope")).toBeNull();
    expect(resolveSchemaNode(SCHEMA, "does.not.exist")).toBeNull();
  });

  it("walks additionalProperties to a map-item enum field", () => {
    const node = resolveSchemaNode(
      SCHEMA,
      "ai.generation.endpoints.local.protocol",
    );
    expect(node?.enum).toEqual([
      "openai-compatible",
      "lmstudio",
      "ollama",
      "vllm",
    ]);
    expect(
      resolveSchemaNode(SCHEMA, "ai.generation.endpoints.local.wire_api")?.enum,
    ).toEqual(["chat-completions", "responses"]);
  });

  it("returns null when the schema is null", () => {
    expect(resolveSchemaNode(null, "recommend_tools.profile")).toBeNull();
  });
});

describe("enumOptionsAt", () => {
  it("maps an enum to FieldOption rows preserving order", () => {
    expect(enumOptionsAt(SCHEMA, "recommend_tools.profile")).toEqual([
      { value: "feature_low", label: "feature_low" },
      { value: "feature_mid", label: "feature_mid" },
      { value: "feature_high", label: "feature_high" },
    ]);
  });

  it("returns an empty array for a non-enum or missing path", () => {
    expect(enumOptionsAt(SCHEMA, "recommend_tools.candidates")).toEqual([]);
    expect(enumOptionsAt(SCHEMA, "missing.path")).toEqual([]);
    expect(enumOptionsAt(null, "recommend_tools.profile")).toEqual([]);
  });

  it("maps a map-item enum through additionalProperties", () => {
    expect(
      enumOptionsAt(SCHEMA, "ai.generation.endpoints.local.protocol"),
    ).toEqual([
      { value: "openai-compatible", label: "openai-compatible" },
      { value: "lmstudio", label: "lmstudio" },
      { value: "ollama", label: "ollama" },
      { value: "vllm", label: "vllm" },
    ]);
  });
});

describe("numberBoundsAt", () => {
  it("reads minimum/maximum bounds when present", () => {
    expect(numberBoundsAt(SCHEMA, "ai.generation.timeout_seconds")).toEqual({
      min: 1,
      max: 3600,
    });
  });

  it("returns empty bounds when absent or path missing", () => {
    expect(numberBoundsAt(SCHEMA, "context_window_overrides")).toEqual({});
    expect(numberBoundsAt(null, "x")).toEqual({});
  });
});
