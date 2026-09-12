import "fake-indexeddb/auto";
import { expect, it } from "vitest";
import { Drafts } from "../src/drafts";
import { fixture, png } from "../../core/test/fixtures";
it("persists drafts and blobs across database connections and edits with revision checks", async () => {
  const name = crypto.randomUUID(),
    drafts = new Drafts(name),
    batch = await drafts.create("Review");
  const a = fixture().manifest.annotations[0]!;
  const saved = await drafts.change(batch.id, {
    annotation: a,
    image: new Blob([new Uint8Array(png)], { type: "image/png" }),
  });
  const reopened = new Drafts(name);
  expect((await reopened.list())[0]).toEqual(saved);
  expect(
    (await reopened.snapshot(batch.id)).assets.get("screenshots/example.png"),
  ).toEqual(png);
  const edited = await reopened.change(batch.id, {
    annotation: { ...a, comment: "New note" },
  });
  expect(edited.annotations[0]).toMatchObject({
    id: a.id,
    revision: 2,
    comment: "New note",
  });
  await expect(drafts.change(batch.id, { annotation: a })).rejects.toThrow(
    /another tab/,
  );
  await reopened.change(batch.id, { deleteId: a.id });
  expect(
    await reopened.image(batch.id, "screenshots/example.png"),
  ).toBeUndefined();
  expect((await reopened.list())[0]!.annotations).toHaveLength(0);
});
it("does not write a record when its screenshot is missing", async () => {
  const drafts = new Drafts(crypto.randomUUID()),
    batch = await drafts.create();
  await expect(
    drafts.change(batch.id, { annotation: fixture().manifest.annotations[0]! }),
  ).rejects.toThrow(/missing/);
  expect((await drafts.list())[0]!.annotations).toEqual([]);
});
