/** Dormant CodeWiki status shown above the code-mode tree. */

import { useEffect, useState } from "react";

import { Badge } from "../../ui/Badge";
import { fetchCodewikiStatus, type CodewikiStatus } from "./WikiTabData";

const POLL_INTERVAL_MS = 30_000;

type StripState =
  | { status: "loading" }
  | { status: "unavailable" }
  | { status: "ready"; snapshot: CodewikiStatus };

function reasonText(snapshot: CodewikiStatus): string {
  if (snapshot.reason === "pending_wiki_redesign") {
    return "Generation paused pending wiki redesign.";
  }
  return snapshot.reason.replace(/_/g, " ");
}

export function WikiCodewikiStatus() {
  const [state, setState] = useState<StripState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const load = async (): Promise<CodewikiStatus | null> => {
      try {
        const snapshot = await fetchCodewikiStatus();
        if (cancelled) return null;
        setState({ status: "ready", snapshot });
        return snapshot;
      } catch {
        if (!cancelled) setState({ status: "unavailable" });
        return null;
      }
    };

    const stopPolling = () => {
      if (timer !== undefined) {
        window.clearInterval(timer);
        timer = undefined;
      }
    };

    const poll = async () => {
      const snapshot = await load();
      if (snapshot !== null && (snapshot.state === "disabled" || snapshot.enabled === false)) {
        stopPolling();
      }
    };

    timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    void poll();

    return () => {
      cancelled = true;
      stopPolling();
    };
  }, []);

  if (state.status === "loading") return null;

  const snapshot = state.status === "ready" ? state.snapshot : null;
  // A live surface needs no dormancy strip: render only while the daemon
  // reports the surface disabled, or when its status is unreachable.
  if (snapshot !== null && snapshot.state !== "disabled" && snapshot.enabled !== false) {
    return null;
  }

  return (
    <p
      role="status"
      aria-label="Codewiki status"
      className="flex shrink-0 items-center gap-1.5 truncate border-b border-border px-2 py-1 text-2xs text-muted-foreground"
    >
      <Badge className="shrink-0 px-1.5 py-0 text-2xs">
        {snapshot === null ? "Unavailable" : "Paused"}
      </Badge>
      <span className="truncate">
        {snapshot === null ? "Codewiki status unavailable" : reasonText(snapshot)}
      </span>
    </p>
  );
}
