/**
 * §4.2 codewiki freshness strip: one quiet line above the code-mode tree
 * with the last mirror refresh and a queued/running indicator. State is
 * carried by text (with motion as reinforcement, never hue alone) per the
 * deutan-safe contract in .impeccable.md. Polls the trigger snapshot while
 * mounted; a daemon without the trigger (503) degrades to "unavailable".
 */

import { useEffect, useState } from "react";

import { formatRelativeTime } from "../../../utils/formatTime";
import { fetchCodewikiStatus, type CodewikiStatus } from "./WikiTabData";

const POLL_INTERVAL_MS = 30_000;

type StripState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "ready"; snapshot: CodewikiStatus };

function lastRunText(snapshot: CodewikiStatus): string {
  const lastRun = snapshot.lastRun;
  if (!lastRun) return "Not refreshed yet";
  const relative = lastRun.finishedAt ? formatRelativeTime(lastRun.finishedAt) : null;
  const when = relative === "now" ? "just now" : relative ? `${relative} ago` : "";
  if (lastRun.outcome === "error") {
    return when ? `Last refresh failed ${when}` : "Last refresh failed";
  }
  const docs = lastRun.changedCount !== null ? ` · ${lastRun.changedCount} docs` : "";
  return `${when ? `Refreshed ${when}` : "Refreshed"}${docs}`;
}

export function WikiCodewikiStatus() {
  const [state, setState] = useState<StripState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchCodewikiStatus()
        .then((snapshot) => {
          if (!cancelled) setState({ status: "ready", snapshot });
        })
        .catch(() => {
          if (!cancelled) setState({ status: "unavailable" });
        });
    };
    load();
    const timer = window.setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (state.status === "loading") return null;

  const snapshot = state.status === "ready" ? state.snapshot : null;
  const busy = snapshot ? snapshot.running || snapshot.pending : false;
  const failed = snapshot?.lastRun?.outcome === "error";

  return (
    <p
      role="status"
      aria-label="Codewiki freshness"
      title={snapshot?.lastRun?.error ?? undefined}
      className="flex shrink-0 items-center gap-1.5 truncate border-b border-border px-2 py-1 text-2xs text-muted-foreground"
    >
      {busy ? (
        <span
          aria-hidden="true"
          className="size-1.5 shrink-0 animate-pulse rounded-full bg-foreground motion-reduce:animate-none"
        />
      ) : null}
      <span className={`truncate ${failed ? "text-destructive" : ""}`}>
        {snapshot === null
          ? "Codewiki status unavailable"
          : snapshot.running
            ? "Refreshing codewiki…"
            : snapshot.pending
              ? "Codewiki refresh queued"
              : lastRunText(snapshot)}
      </span>
    </p>
  );
}
