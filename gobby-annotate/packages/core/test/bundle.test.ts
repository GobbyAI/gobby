import { describe, expect, it } from "vitest";
import { strToU8, zipSync } from "fflate";
import { decodeBundle, encodeBundle, inspectZip } from "../src";
import { fixture, png } from "./fixtures";

describe("portable bundles", () => {
  it("compares timestamp instants across fractional precision", () => {
    const bundle = fixture();
    const annotation = bundle.manifest.annotations[0]!;
    annotation.createdAt = "2026-09-11T12:00:00Z";
    annotation.updatedAt = "2026-09-11T12:00:00.500Z";
    expect(decodeBundle(encodeBundle(bundle))).toEqual(bundle);
    annotation.createdAt = "2026-09-11T12:00:00.500Z";
    annotation.updatedAt = "2026-09-11T12:00:00Z";
    expect(() => encodeBundle(bundle)).toThrow(/reversed timestamps/);
  });
  it.each(["truncated", "checksum", "trailing"])(
    "rejects %s PNG assets before export",
    (kind) => {
      const bundle = fixture();
      const bytes =
        kind === "truncated"
          ? png.slice(0, 33)
          : kind === "trailing"
            ? new Uint8Array([...png, 0])
            : png.slice();
      if (kind === "checksum") bytes[45] ^= 1;
      bundle.assets.set("screenshots/example.png", bytes);
      expect(() => encodeBundle(bundle)).toThrow(/PNG/);
    },
  );
  it("validates compressed image bytes and manifest", () => {
    const bundle = fixture();
    const bytes = zipSync({
      "capture.json": strToU8(JSON.stringify(bundle.manifest)),
      ...Object.fromEntries(bundle.assets),
    });
    expect(decodeBundle(bytes)).toEqual(bundle);
  });
  it("rejects corrupted stored bytes even when the JSON remains valid", () => {
    const bytes = encodeBundle(fixture());
    const entry = inspectZip(bytes).get("capture.json")!;
    bytes[entry.start + 2] = "X".charCodeAt(0);
    expect(() => decodeBundle(bytes)).toThrow(/checksum mismatch/);
  });
  it("rejects compressed expansion beyond a forged declared size", () => {
    const bytes = zipSync({ "capture.json": strToU8("a".repeat(1_000_000)) });
    const view = new DataView(bytes.buffer);
    const end = bytes.length - 22;
    const central = view.getUint32(end + 16, true);
    view.setUint32(central + 24, 1, true);
    expect(() => decodeBundle(bytes)).toThrow(/size mismatch/);
  });
  it("round trips manifest and original image bytes", () => {
    const bundle = fixture();
    expect(decodeBundle(encodeBundle(bundle))).toEqual(bundle);
  });
  it("accepts an explicit screenshot failure", () => {
    const bundle = fixture();
    bundle.assets.clear();
    bundle.manifest.annotations[0]!.screenshot = {
      status: "unavailable",
      reason: "Site capture denied",
    };
    expect(decodeBundle(encodeBundle(bundle)).manifest).toEqual(
      bundle.manifest,
    );
  });
  it.each([
    "../escape.png",
    "/etc/passwd",
    "screenshots/../escape.png",
    "screenshots\\a.png",
  ])("rejects unsafe path %s", (path) => {
    expect(() => decodeBundle(zipSync({ [path]: png }))).toThrow(/Unsafe/);
  });
  it("rejects unsupported versions and empty notes", () => {
    const bundle = fixture();
    expect(() =>
      decodeBundle(
        zipSync({
          "capture.json": strToU8(
            JSON.stringify({ ...bundle.manifest, version: 2 }),
          ),
        }),
      ),
    ).toThrow();
    bundle.manifest.annotations[0]!.comment = "  ";
    expect(() => encodeBundle(bundle)).toThrow(/needs a comment/);
  });
  it("rejects missing images and duplicate annotation IDs", () => {
    const bundle = fixture();
    bundle.assets.clear();
    expect(() => encodeBundle(bundle)).toThrow(/Missing screenshot/);
    bundle.assets.set("screenshots/example.png", png);
    bundle.manifest.annotations.push(bundle.manifest.annotations[0]!);
    expect(() => encodeBundle(bundle)).toThrow(/Duplicate annotation/);
  });
  it("rejects oversized central sizes before inflating", () => {
    const bytes = encodeBundle(fixture());
    const v = new DataView(bytes.buffer);
    const central = v.getUint32(bytes.length - 6, true);
    v.setUint32(central + 24, 129 * 1024 * 1024, true);
    expect(() => inspectZip(bytes)).toThrow(/128 MiB/);
  });
  it("rejects symlink entries", () => {
    const bytes = encodeBundle(fixture());
    const v = new DataView(bytes.buffer);
    const central = v.getUint32(bytes.length - 6, true);
    v.setUint32(central + 38, 0xa000 << 16, true);
    expect(() => inspectZip(bytes)).toThrow(/file type/);
  });
  it("rejects duplicate archive names before object decoding loses them", () => {
    const bytes = zipSync({
      "screenshots/a.png": png,
      "screenshots/b.png": png,
    });
    const a = strToU8("screenshots/a.png"),
      b = strToU8("screenshots/b.png");
    for (let p = 0; p <= bytes.length - b.length; p++)
      if (b.every((n, i) => bytes[p + i] === n)) bytes.set(a, p);
    expect(() => inspectZip(bytes)).toThrow(/Duplicate ZIP/);
  });
});
