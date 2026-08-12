import { describe, expect, it } from "vitest";

import {
  DYNAMIC_SEGMENT_CODEC_VECTORS,
  INVALID_DYNAMIC_SEGMENTS,
  INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS,
} from "../runtimeConfigCodecVectors.gen";
import {
  DynamicSegmentError,
  decodeDynamicSegment,
  decodeDynamicSegmentLenient,
  encodeDynamicSegment,
} from "../runtimeConfigSegments";

describe("dynamic segment codec contract", () => {
  it("encodes_every_shared_vector_canonically", () => {
    for (const { decoded, encoded } of DYNAMIC_SEGMENT_CODEC_VECTORS) {
      expect(encodeDynamicSegment(decoded)).toBe(encoded);
    }
  });

  it("decodes_every_shared_vector_back_to_its_source", () => {
    for (const { decoded, encoded } of DYNAMIC_SEGMENT_CODEC_VECTORS) {
      expect(decodeDynamicSegment(encoded)).toBe(decoded);
    }
  });

  it("rejects_every_shared_invalid_segment", () => {
    expect(INVALID_DYNAMIC_SEGMENTS).toContain("");
    for (const segment of INVALID_DYNAMIC_SEGMENTS) {
      expect(() => decodeDynamicSegment(segment), segment).toThrow(
        DynamicSegmentError,
      );
    }
  });

  it("rejects_encoding_an_empty_segment", () => {
    expect(() => encodeDynamicSegment("")).toThrow(DynamicSegmentError);
  });

  it("rejects_invalid_unicode_text_before_encoding", () => {
    for (const value of INVALID_DYNAMIC_SEGMENT_TEXT_VECTORS) {
      expect(() => encodeDynamicSegment(value)).toThrow(DynamicSegmentError);
    }
  });

  it("lenient_decode_falls_back_to_the_raw_key", () => {
    expect(decodeDynamicSegmentLenient("dot%2Esegment")).toBe("dot.segment");
    expect(decodeDynamicSegmentLenient("raw.dot")).toBe("raw.dot");
  });
});
