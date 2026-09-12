import { z } from "zod";
import { annotationSchema, id, pngDimensions } from "@gobby/annotate-core";
import { Drafts } from "./drafts";
import { sameCapture, base64, type Fingerprint } from "./screenshots";
import { exportSnapshot, stageSafari } from "./export";

const drafts = new Drafts();
let captureQueue: Promise<unknown> = Promise.resolve();
let lastCapture = -Infinity;

function captureVisible(windowId: number): Promise<string> {
  const pending = captureQueue.then(async () => {
    // Chrome permits two visible-tab captures per second. Serialize across tabs.
    const delay = 550 - (performance.now() - lastCapture);
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    lastCapture = performance.now();
    return chrome.tabs.captureVisibleTab(windowId, { format: "png" });
  });
  captureQueue = pending.catch(() => {});
  return pending;
}
const commands = z.discriminatedUnion("type", [
  z.object({ type: z.literal("list") }),
  z.object({ type: z.literal("create") }),
  z.object({
    type: z.literal("rename"),
    batchId: id,
    title: z.string().max(200),
  }),
  z.object({
    type: z.literal("save"),
    batchId: id,
    annotation: annotationSchema,
    image: z
      .string()
      .max(180 * 1024 * 1024)
      .optional(),
  }),
  z.object({ type: z.literal("delete"), batchId: id, annotationId: id }),
  z.object({
    type: z.literal("image"),
    batchId: id,
    path: z.string().regex(/^screenshots\/[\w-]+\.png$/),
  }),
  z.object({ type: z.literal("export"), batchId: id }),
  z.object({
    type: z.literal("capture"),
    expected: z.custom<Fingerprint>(
      (v) =>
        !!v &&
        typeof v === "object" &&
        "document" in v &&
        "viewport" in v &&
        "url" in v,
    ),
  }),
]);
let activationEpoch = 0;
chrome.tabs.onActivated.addListener(() => {
  activationEpoch++;
});
chrome.tabs.onUpdated.addListener((_id, change) => {
  if (change.status === "loading") activationEpoch++;
});
chrome.action.onClicked.addListener((tab) => {
  if (tab.id === undefined) return;
  const tabId = tab.id;
  chrome.scripting
    .executeScript({ target: { tabId }, files: ["content.js"] })
    .catch(async (error) => {
      await chrome.action.setBadgeText({ tabId, text: "!" });
      await chrome.action.setTitle({
        tabId,
        title: `Cannot annotate this page: ${String(error)}`,
      });
    });
});

async function dispatch(
  raw: unknown,
  sender: chrome.runtime.MessageSender,
): Promise<unknown> {
  if (
    sender.id !== chrome.runtime.id ||
    sender.tab?.id === undefined ||
    sender.frameId !== 0
  )
    throw new Error("Only activated top-frame extension UI may access drafts");
  const command = commands.parse(raw);
  switch (command.type) {
    case "list":
      return drafts.list();
    case "create":
      return drafts.create();
    case "rename":
      return drafts.change(command.batchId, { title: command.title });
    case "delete":
      return drafts.change(command.batchId, { deleteId: command.annotationId });
    case "save": {
      if (command.image && !command.image.startsWith("data:image/png;base64,"))
        throw new Error("Expected a PNG screenshot");
      const image = command.image
        ? await (await fetch(command.image)).blob()
        : undefined;
      return drafts.change(command.batchId, {
        annotation: command.annotation,
        ...(image ? { image } : {}),
      });
    }
    case "image": {
      const blob = await drafts.image(command.batchId, command.path);
      if (!blob) throw new Error("Screenshot not found");
      return (
        "data:image/png;base64," +
        base64(new Uint8Array(await blob.arrayBuffer()))
      );
    }
    case "export":
      return exportSnapshot(
        await drafts.snapshot(command.batchId),
        async (bytes, name) => {
          if (!chrome.downloads?.download) return stageSafari(bytes, name);
          const downloadId = await chrome.downloads.download({
            url: "data:application/zip;base64," + base64(bytes),
            filename: name,
            saveAs: true,
          });
          if (downloadId === undefined)
            throw new Error("Export cancelled; drafts are retained");
          return "Download started. Drafts retained.";
        },
      );
    case "capture": {
      const tabId = sender.tab.id,
        windowId = sender.tab.windowId,
        epoch = activationEpoch;
      const before: Fingerprint = await chrome.tabs.sendMessage(tabId, {
        type: "fingerprint",
      });
      const [active] = await chrome.tabs.query({ active: true, windowId });
      if (active?.id !== tabId || !sameCapture(before, command.expected))
        throw new Error("Selection is stale. Select the target again.");
      const image = await captureVisible(windowId);
      const after: Fingerprint = await chrome.tabs.sendMessage(tabId, {
        type: "fingerprint",
      });
      const [stillActive] = await chrome.tabs.query({ active: true, windowId });
      if (
        epoch !== activationEpoch ||
        stillActive?.id !== tabId ||
        !sameCapture(before, after)
      )
        throw new Error(
          "Page or tab changed during capture. Select the target again.",
        );
      const bytes = new Uint8Array(await (await fetch(image)).arrayBuffer());
      return { image, ...pngDimensions(bytes) };
    }
  }
}
chrome.runtime.onMessage.addListener((message: unknown, sender, respond) => {
  dispatch(message, sender)
    .then((value) => respond({ ok: true, value }))
    .catch((error) =>
      respond({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }),
    );
  return true;
});
