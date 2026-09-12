import { createRoot, type Root } from "react-dom/client";
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Button } from "../../../../web/src/components/ui/Button";
import { Input } from "../../../../web/src/components/ui/Input";
import type { Annotation } from "@gobby/annotate-core";
import type { Batch } from "./drafts";
import { Toolbar } from "./Toolbar";
import { CapturePanel, SaveStatus } from "./CapturePanel";
import { assertSelectionCurrent, select, type Mode } from "./selection";
import { captureHidden, fingerprint, sameCapture } from "./screenshots";
import { viewport } from "./frame-agent";
import { rpc } from "./api";
import css from "./ui.css?inline";
import { installFonts } from "./fonts";
import { scopeStyles } from "./styles";

const HOST_ID = "gobby-annotate-root";
export function clampPosition(
  x: number,
  y: number,
  width: number,
  height: number,
  view = viewport().visual,
) {
  return {
    x: Math.max(
      view.offsetLeft + 4,
      Math.min(x, view.offsetLeft + view.width - width - 4),
    ),
    y: Math.max(
      view.offsetTop + 4,
      Math.min(y, view.offsetTop + view.height - height - 4),
    ),
  };
}
function App({
  host,
  deactivate,
  documentId,
}: {
  host: HTMLElement;
  deactivate: () => void;
  documentId: string;
}) {
  const [batch, setBatch] = useState<Batch | null>(null),
    current = useRef<Batch | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]),
    [mode, setMode] = useState<Mode>("browse");
  const [panel, setPanel] = useState<"none" | "list" | "menu">("none");
  const [editing, setEditing] = useState<string | null>(null),
    [image, setImage] = useState<string | null>(null);
  const [status, setStatus] = useState(""),
    [error, setError] = useState(""),
    [collapsed, setCollapsed] = useState(false);
  const [failed, setFailed] = useState<Annotation | null>(null),
    [busy, setBusy] = useState(false);
  const shell = useRef<HTMLDivElement>(null),
    sequence = useRef(Promise.resolve());
  const pendingEdit = useRef<{ id: string; text: string } | null>(null);
  const pendingTitle = useRef<{ text: string } | null>(null);
  const selected = batch?.annotations.find((a) => a.id === editing);
  function assign(value: Batch) {
    current.current = value;
    setBatch(value);
  }
  function report(e: unknown) {
    setError(e instanceof Error ? e.message : String(e));
    setStatus("Unsaved changes");
  }
  function queue(work: () => Promise<void>) {
    sequence.current = sequence.current.then(work).catch(report);
  }
  function requireSaved() {
    if (pendingEdit.current || pendingTitle.current !== null)
      throw new Error(
        "Your changes are not saved yet. Wait for saving or use Retry save.",
      );
  }
  function canLeaveEditor() {
    try {
      requireSaved();
      return true;
    } catch (error) {
      report(error);
      return false;
    }
  }
  useEffect(() => {
    const guard = (event: Event) => {
      if (!canLeaveEditor()) event.preventDefault();
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (pendingEdit.current || pendingTitle.current !== null) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    host.addEventListener("annotate-before-deactivate", guard);
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      host.removeEventListener("annotate-before-deactivate", guard);
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, []);
  useEffect(() => {
    rpc<Batch[]>({ type: "list" })
      .then(async (list) => {
        setBatches(list);
        assign(
          list.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0] ??
            (await rpc<Batch>({ type: "create" })),
        );
      })
      .catch(report);
  }, []);
  useEffect(() => {
    const node = shell.current;
    if (!node) return;
    const reposition = () => {
      const view = viewport().visual;
      node.style.maxWidth = Math.max(44, view.width - 8) + "px";
      node.style.maxHeight = Math.max(44, view.height - 8) + "px";
      const rect = node.getBoundingClientRect();
      const point = clampPosition(
        rect.x,
        rect.y,
        rect.width,
        rect.height,
        view,
      );
      node.style.left = point.x + "px";
      node.style.top = point.y + "px";
    };
    const observer = new ResizeObserver(reposition);
    observer.observe(node);
    window.addEventListener("resize", reposition);
    visualViewport?.addEventListener("resize", reposition);
    visualViewport?.addEventListener("scroll", reposition);
    reposition();
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", reposition);
      visualViewport?.removeEventListener("resize", reposition);
      visualViewport?.removeEventListener("scroll", reposition);
    };
  }, []);
  useEffect(() => {
    if (mode === "browse") return;
    return select(
      mode,
      host,
      (a) => {
        setMode("browse");
        void capture(a);
      },
      () => setMode("browse"),
    );
  }, [mode]);
  async function persist(a: Annotation, screenshot?: string) {
    if (!current.current) throw new Error("Batch is still loading");
    const value = await rpc<Batch>({
      type: "save",
      batchId: current.current.id,
      annotation: a,
      ...(screenshot ? { image: screenshot } : {}),
    });
    assign(value);
    setEditing(a.id);
    setImage(screenshot ?? null);
    setStatus("Saved locally");
    setFailed(null);
  }
  async function capture(a: Annotation) {
    setBusy(true);
    setError("");
    const expected = {
      ...fingerprint(documentId),
      url: a.page.url,
      viewport: a.viewport,
    };
    try {
      if (!sameCapture(expected, fingerprint(documentId)))
        throw new Error("Selection is stale. Select the target again.");
      const result = await captureHidden(host, async () => {
        assertSelectionCurrent(a);
        const image = await rpc<{
          image: string;
          width: number;
          height: number;
        }>({ type: "capture", expected });
        assertSelectionCurrent(a);
        return image;
      });
      await persist(
        {
          ...a,
          screenshot: {
            status: "available",
            path: `screenshots/${a.id}.png`,
            width: result.width,
            height: result.height,
          },
        },
        result.image,
      );
    } catch (e) {
      report(e);
      setFailed(a);
    } finally {
      setBusy(false);
    }
  }
  function changeMode(next: Mode) {
    if (!canLeaveEditor()) return false;
    setMode(next);
    setPanel("none");
    setEditing(null);
    setFailed(null);
    return true;
  }
  function edit(text: string) {
    const annotationId = editing;
    if (!annotationId) return;
    const pending = { id: annotationId, text };
    pendingEdit.current = pending;
    setStatus("Saving…");
    queue(async () => {
      const latest = current.current?.annotations.find(
        (a) => a.id === annotationId,
      );
      if (!latest || !current.current)
        throw new Error("Annotation no longer exists");
      assign(
        await rpc<Batch>({
          type: "save",
          batchId: current.current.id,
          annotation: { ...latest, comment: text },
        }),
      );
      if (pendingEdit.current === pending) {
        pendingEdit.current = null;
        setStatus("Saved locally");
        setError("");
      }
    });
  }
  async function openAnnotation(a: Annotation) {
    if (!canLeaveEditor()) return;
    setEditing(a.id);
    setPanel("none");
    setImage(null);
    setStatus("Saved locally");
    if (a.screenshot.status === "available" && batch)
      setImage(
        await rpc<string>({
          type: "image",
          batchId: batch.id,
          path: a.screenshot.path,
        }),
      );
  }
  function rename(title: string) {
    const pending = { text: title };
    pendingTitle.current = pending;
    setStatus("Saving…");
    queue(async () => {
      if (!current.current) throw new Error("Batch is still loading");
      assign(
        await rpc<Batch>({
          type: "rename",
          batchId: current.current.id,
          title,
        }),
      );
      if (pendingTitle.current === pending) {
        pendingTitle.current = null;
        setStatus("Saved locally");
        setError("");
      }
    });
  }
  function dock(right: boolean) {
    const node = shell.current;
    if (!node) return;
    const v = viewport().visual,
      r = node.getBoundingClientRect();
    const p = clampPosition(
      right ? v.offsetLeft + v.width : v.offsetLeft,
      v.offsetTop + v.height,
      r.width,
      r.height,
      v,
    );
    node.style.left = p.x + "px";
    node.style.top = p.y + "px";
  }
  function drag(e: ReactPointerEvent<HTMLButtonElement>) {
    e.preventDefault();
    const node = shell.current;
    if (!node) return;
    const handle = e.currentTarget;
    let moved = false;
    const origin = node.getBoundingClientRect(),
      x = e.clientX,
      y = e.clientY;
    const move = (event: PointerEvent) => {
      moved ||= Math.hypot(event.clientX - x, event.clientY - y) > 4;
      const p = clampPosition(
        origin.x + event.clientX - x,
        origin.y + event.clientY - y,
        origin.width,
        origin.height,
      );
      node.style.left = p.x + "px";
      node.style.top = p.y + "px";
    };
    const done = (event: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", done);
      window.removeEventListener("pointercancel", done);
      if (moved && event.type === "pointerup") {
        const stopClick = (click: Event) => {
          click.preventDefault();
          click.stopImmediatePropagation();
        };
        handle.addEventListener("click", stopClick, {
          once: true,
          capture: true,
        });
        setTimeout(
          () => handle.removeEventListener("click", stopClick, true),
          0,
        );
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", done, { once: true });
    window.addEventListener("pointercancel", done, { once: true });
  }
  return (
    <div ref={shell} className="annotate-shell" style={{ left: 8, top: 8 }}>
      {error && (
        <div role="alert" className="annotate-panel">
          {error}
          {(pendingEdit.current || pendingTitle.current !== null) && (
            <Button
              onClick={() => {
                if (pendingEdit.current) edit(pendingEdit.current.text);
                if (pendingTitle.current) rename(pendingTitle.current.text);
              }}
            >
              Retry save
            </Button>
          )}
          {(pendingEdit.current || pendingTitle.current !== null) && (
            <Button
              variant="destructive"
              onClick={() =>
                queue(async () => {
                  const list = await rpc<Batch[]>({ type: "list" });
                  const latest = list.find(
                    (item) => item.id === current.current?.id,
                  );
                  if (!latest) throw new Error("Batch no longer exists");
                  assign(latest);
                  pendingEdit.current = null;
                  pendingTitle.current = null;
                  setEditing(null);
                  setPanel("none");
                  setError("");
                  setStatus("Saved version restored");
                })
              }
            >
              Discard unsaved changes
            </Button>
          )}
          <Button onClick={() => setError("")}>Dismiss</Button>
        </div>
      )}
      {busy && (
        <p role="status" className="annotate-panel">
          Capturing…
        </p>
      )}
      {failed && (
        <section className="annotate-panel" aria-label="Capture failed">
          <p>Screenshot failed. Your selection is retained.</p>
          <Button
            onClick={() => {
              void capture(failed);
            }}
          >
            Retry
          </Button>
          <Button
            onClick={() =>
              queue(() =>
                persist({
                  ...failed,
                  screenshot: {
                    status: "unavailable",
                    reason: error || "Capture failed; user chose no screenshot",
                  },
                }),
              )
            }
          >
            Save without screenshot
          </Button>
          <Button onClick={() => setFailed(null)}>Cancel</Button>
        </section>
      )}
      {selected && !collapsed && (
        <CapturePanel
          key={selected.id}
          annotation={selected}
          image={image}
          status={status}
          onEdit={edit}
          onClose={() => {
            if (canLeaveEditor()) setEditing(null);
          }}
          onDelete={() =>
            queue(async () => {
              if (current.current)
                assign(
                  await rpc<Batch>({
                    type: "delete",
                    batchId: current.current.id,
                    annotationId: selected.id,
                  }),
                );
              setEditing(null);
              pendingEdit.current = null;
            })
          }
        />
      )}
      {panel === "list" && !collapsed && (
        <section className="annotate-panel" aria-label="Annotations">
          <strong>{batch?.title}</strong>
          {batch?.annotations.length ? (
            batch.annotations.map((a) => (
              <Button
                key={a.id}
                className="annotate-list-row"
                onClick={() => {
                  void openAnnotation(a).catch(report);
                }}
              >
                {a.comment || "Unfinished note"}
              </Button>
            ))
          ) : (
            <p>Select an element or draw a rectangle to add a note.</p>
          )}
        </section>
      )}
      {panel === "menu" && !collapsed && (
        <section className="annotate-panel" aria-label="Batch and export">
          <label htmlFor="batch-title">Batch name</label>
          <Input
            id="batch-title"
            key={batch?.id}
            defaultValue={batch?.title}
            maxLength={200}
            onChange={(e) => rename(e.target.value)}
          />
          <Button
            variant="primary"
            disabled={!batch || busy}
            onClick={() =>
              queue(async () => {
                requireSaved();
                if (!current.current) return;
                setBusy(true);
                try {
                  setStatus(
                    await rpc<string>({
                      type: "export",
                      batchId: current.current.id,
                    }),
                  );
                } finally {
                  setBusy(false);
                }
              })
            }
          >
            Export ZIP
          </Button>
          <SaveStatus status={status} />
          <Button
            onClick={() =>
              queue(async () => {
                requireSaved();
                assign(await rpc<Batch>({ type: "create" }));
                setEditing(null);
                setBatches(await rpc<Batch[]>({ type: "list" }));
              })
            }
          >
            New batch
          </Button>
          <details>
            <summary>Reopen batch</summary>
            {batches.map((b) => (
              <Button
                key={b.id}
                className="annotate-list-row"
                onClick={() =>
                  queue(async () => {
                    requireSaved();
                    const latest = await rpc<Batch[]>({ type: "list" });
                    const found = latest.find((a) => a.id === b.id);
                    if (found) assign(found);
                    setEditing(null);
                  })
                }
              >
                {b.title}
              </Button>
            ))}
          </details>
          <Button onClick={() => dock(false)}>Dock left</Button>
          <Button onClick={() => dock(true)}>Dock right</Button>
          <Button
            onClick={() => {
              host.dataset.theme =
                host.dataset.theme === "light" ? "dark" : "light";
            }}
          >
            Switch theme
          </Button>
          <Button
            onClick={() =>
              queue(async () => {
                deactivate();
              })
            }
          >
            Deactivate annotation
          </Button>
        </section>
      )}
      <Toolbar
        count={batch?.annotations.length ?? 0}
        collapsed={collapsed}
        mode={mode}
        onMode={changeMode}
        onList={() => {
          if (!changeMode("browse")) return;
          setPanel(panel === "list" ? "none" : "list");
        }}
        onMenu={() => {
          if (!changeMode("browse")) return;
          setPanel(panel === "menu" ? "none" : "menu");
        }}
        onCollapse={() => {
          if (!changeMode("browse")) return;
          setCollapsed(!collapsed);
        }}
        onDrag={drag}
      />
    </div>
  );
}

export function activate(): () => void {
  const existing = document.getElementById(HOST_ID);
  if (existing)
    return () => existing.dispatchEvent(new Event("annotate-deactivate"));
  const host = document.createElement("div");
  host.id = HOST_ID;
  host.dataset.theme = matchMedia("(prefers-color-scheme: light)").matches
    ? "light"
    : "dark";
  host.style.cssText =
    "all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
  const shadow = host.attachShadow({ mode: "open" }),
    style = document.createElement("style");
  style.textContent = scopeStyles(css);
  shadow.append(style);
  const mount = document.createElement("div");
  shadow.append(mount);
  document.documentElement.append(host);
  const documentId = crypto.randomUUID();
  let root: Root | null = createRoot(mount);
  const removeFonts = installFonts();
  const listener = (
    message: { type?: string },
    _sender: chrome.runtime.MessageSender,
    respond: (value: unknown) => void,
  ) => {
    if (message.type === "fingerprint") respond(fingerprint(documentId));
  };
  chrome.runtime.onMessage.addListener(listener);
  const deactivate = () => {
    if (
      !host.dispatchEvent(
        new Event("annotate-before-deactivate", { cancelable: true }),
      )
    )
      return;
    root?.unmount();
    root = null;
    chrome.runtime.onMessage.removeListener(listener);
    removeFonts();
    host.remove();
  };
  host.addEventListener("annotate-deactivate", deactivate);
  root.render(
    <App host={host} deactivate={deactivate} documentId={documentId} />,
  );
  return deactivate;
}
if (typeof chrome !== "undefined" && chrome.runtime?.id) activate();
