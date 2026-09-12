import { expect, it, vi } from "vitest";
import { exportSnapshot } from "../src/export";
import { decodeBundle } from "@gobby/annotate-core";
import { fixture } from "../../core/test/fixtures";
it("delivers matching metadata and all original image bytes", async () => {
  const bundle = fixture(),
    original = structuredClone(bundle);
  const result = await exportSnapshot(bundle, async (bytes, name) => {
    expect(decodeBundle(bytes)).toEqual(original);
    expect(name).toContain(bundle.manifest.exportId);
    return "Saved";
  });
  expect(result).toBe("Saved");
  expect(bundle).toEqual(original);
});
it("preserves the snapshot after cancellation and validates before delivery", async () => {
  const bundle = fixture(),
    original = structuredClone(bundle);
  await expect(
    exportSnapshot(bundle, async () => {
      throw new Error("Cancelled");
    }),
  ).rejects.toThrow("Cancelled");
  expect(bundle).toEqual(original);
  bundle.manifest.annotations[0]!.comment = "";
  const deliver = vi.fn();
  await expect(exportSnapshot(bundle, deliver)).rejects.toThrow(/comment/);
  expect(deliver).not.toHaveBeenCalled();
});
