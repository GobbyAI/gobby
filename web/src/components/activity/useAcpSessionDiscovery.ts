import { useEffect, useRef } from "react";

const DISCOVER_DEBOUNCE_MS = 250;

/**
 * Triggers ACP session discovery when the Sessions panel opens and on each
 * segmented-control (Live | Expired) change.
 *
 * The POST is best-effort: the canonical rows it reconciles arrive through the
 * existing `session_created` / `session_updated` WebSocket broadcasts, so a
 * failed or dropped discover never blocks the panel — there is no parallel
 * data path. A trailing-edge debounce plus an in-flight guard coalesce
 * panel-open and toggle bursts into at most one outstanding request (with a
 * single pending re-run), pairing with the backend's per-provider in-flight
 * lock so rapid toggles don't hammer the ACP subprocess.
 *
 * @param trigger A value that changes whenever discovery should re-run (the
 *   Sessions status mode). The hook also fires once on mount.
 */
export function useAcpSessionDiscovery(trigger: string): void {
  const inFlightRef = useRef(false);
  const pendingRef = useRef(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    const run = async (): Promise<void> => {
      if (inFlightRef.current) {
        pendingRef.current = true;
        return;
      }
      inFlightRef.current = true;
      try {
        const res = await fetch("/api/sessions/acp/discover", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (!res.ok) {
          const body = await res.text().catch(() => "");
          console.warn("ACP session discovery failed", {
            trigger,
            status: res.status,
            body,
          });
        }
      } catch (error) {
        // Best-effort: discovered rows arrive via the session WS broadcasts.
        console.warn("ACP session discovery failed", { trigger, error });
      } finally {
        inFlightRef.current = false;
        if (pendingRef.current) {
          pendingRef.current = false;
          void run();
        }
      }
    };

    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      void run();
    }, DISCOVER_DEBOUNCE_MS);

    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [trigger]);
}
