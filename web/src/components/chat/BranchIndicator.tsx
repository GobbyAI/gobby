import { useState, useEffect, useRef, useCallback } from "react";
import type { WorktreeInfo } from "../../hooks/useSourceControl";
import { NO_CHECKOUT_MESSAGE } from "../../lib/projectCheckout";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";

interface BranchInfo {
  name: string;
  is_current: boolean;
  is_remote: boolean;
  worktree_id: string | null;
}

interface BranchIndicatorProps {
  currentBranch: string | null;
  worktreePath: string | null;
  projectId: string | null;
  onWorktreeChange: (worktreePath: string, worktreeId?: string) => void;
  disabled?: boolean;
  variant?: "toolbar" | "select";
  compact?: boolean;
}

function truncateLabel(value: string, maxChars: number): string {
  return value.length > maxChars ? `${value.slice(0, maxChars)}...` : value;
}

/** Read a FastAPI error `detail` (string or `{ message }`) off a failed response. */
async function readErrorMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const data: unknown = await response.json();
    const detail = (data as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = detail.message;
      if (typeof message === "string" && message.trim()) return message;
    }
  } catch {
    // Fall through to status text.
  }
  return response.statusText || fallback;
}

/**
 * Result of GET /api/projects/{id}/checkouts. Only a 200 whose `checkout` is
 * null means "none"; every other failure is an error the user should see.
 */
type CheckoutLookup =
  | { status: "loading" }
  | { status: "ready"; rootPath: string }
  | { status: "none" }
  | { status: "error"; message: string };

const LOADING_LOOKUP: CheckoutLookup = { status: "loading" };

function readCheckoutLookup(data: unknown): CheckoutLookup {
  if (typeof data !== "object" || data === null || !("checkout" in data)) {
    return { status: "error", message: "Unexpected checkout response" };
  }
  const checkout = data.checkout;
  if (checkout === null) return { status: "none" };
  if (
    typeof checkout === "object" &&
    "root_path" in checkout &&
    typeof checkout.root_path === "string"
  ) {
    return { status: "ready", rootPath: checkout.root_path };
  }
  return { status: "error", message: "Unexpected checkout response" };
}

export function BranchIndicator({
  currentBranch,
  worktreePath,
  projectId,
  onWorktreeChange,
  variant = "toolbar",
  disabled = false,
  compact = false,
}: BranchIndicatorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [worktrees, setWorktrees] = useState<WorktreeInfo[]>([]);
  const [branches, setBranches] = useState<BranchInfo[]>([]);
  const [checkoutState, setCheckoutState] = useState<{
    projectId: string | null;
    lookup: CheckoutLookup;
  }>({ projectId: null, lookup: LOADING_LOOKUP });
  const [apiBranch, setApiBranch] = useState<string | null>(null);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const buildParams = useCallback(() => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    return params;
  }, [projectId]);

  // Never throws: every outcome is a CheckoutLookup the dropdown can render.
  const fetchProjectCheckout =
    useCallback(async (): Promise<CheckoutLookup> => {
      if (!projectId) return { status: "none" };
      try {
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/checkouts`,
        );
        if (!response.ok) {
          return {
            status: "error",
            message: await readErrorMessage(
              response,
              `HTTP ${response.status}`,
            ),
          };
        }
        return readCheckoutLookup(await response.json());
      } catch (error) {
        return {
          status: "error",
          message:
            error instanceof Error && error.message
              ? error.message
              : "Checkout lookup failed",
        };
      }
    }, [projectId]);
  // A lookup for a different project is stale, so the current one is loading.
  const checkoutLookup: CheckoutLookup =
    checkoutState.projectId === projectId
      ? checkoutState.lookup
      : LOADING_LOOKUP;
  const mainRepoPath =
    checkoutLookup.status === "ready" ? checkoutLookup.rootPath : null;

  // Eagerly fetch current branch on mount / project change
  useEffect(() => {
    let stale = false;
    fetch(`/api/source-control/status?${buildParams()}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (stale || !data) return;
        if (data.current_branch) setApiBranch(data.current_branch);
      })
      .catch(() => {});
    void fetchProjectCheckout().then((lookup) => {
      if (!stale) setCheckoutState({ projectId, lookup });
    });
    return () => {
      stale = true;
    };
  }, [buildParams, fetchProjectCheckout, projectId]);

  // Click-outside-close
  useEffect(() => {
    if (!isOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [isOpen]);

  // Fetch worktrees + branches when dropdown opens
  const fetchDropdownData = useCallback(async () => {
    const params = buildParams();
    const checkoutPromise = fetchProjectCheckout();
    const [wtRes, brRes, statusRes] = await Promise.allSettled([
      fetch(`/api/source-control/worktrees?${params}`),
      fetch(`/api/source-control/branches?${params}`),
      fetch(`/api/source-control/status?${params}`),
    ]);

    if (wtRes.status === "fulfilled" && wtRes.value.ok) {
      const data = await wtRes.value.json();
      setWorktrees(data.worktrees || []);
    }
    if (brRes.status === "fulfilled" && brRes.value.ok) {
      const data = await brRes.value.json();
      setBranches(
        (data.branches || []).filter((b: BranchInfo) => !b.is_remote),
      );
    }
    if (statusRes.status === "fulfilled" && statusRes.value.ok) {
      const data = await statusRes.value.json();
      if (data.current_branch) setApiBranch(data.current_branch);
    }
    setCheckoutState({ projectId, lookup: await checkoutPromise });
  }, [buildParams, fetchProjectCheckout, projectId]);

  const handleToggle = () => {
    if (disabled) return;
    if (!isOpen) {
      setCheckoutError(null);
      fetchDropdownData();
    }
    setIsOpen(!isOpen);
  };

  const handleSelectWorktree = (path: string, id?: string) => {
    setCheckoutError(null);
    onWorktreeChange(path, id);
    setIsOpen(false);
  };

  const handleSelectBranch = async (branchName: string) => {
    setCheckoutError(null);
    if (!mainRepoPath) {
      setCheckoutError(
        checkoutLookup.status === "none"
          ? NO_CHECKOUT_MESSAGE
          : "Repository path unavailable",
      );
      return;
    }

    const params = buildParams();
    try {
      const response = await fetch(
        `/api/source-control/branches/checkout?${params}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ branch_name: branchName }),
        },
      );

      if (!response.ok) {
        setCheckoutError(
          await readErrorMessage(response, `Failed to switch to ${branchName}`),
        );
        return;
      }

      const data = await response.json();
      if (data.current_branch) setApiBranch(data.current_branch);
      onWorktreeChange(mainRepoPath);
      setIsOpen(false);
    } catch {
      setCheckoutError(`Failed to switch to ${branchName}`);
    }
  };

  const effectiveBranch = currentBranch ?? apiBranch;
  if (!effectiveBranch) return null;

  const isDetached = effectiveBranch.startsWith("detached:");
  const rawDisplayBranch = isDetached
    ? effectiveBranch.replace("detached:", "")
    : effectiveBranch;
  const displayBranch = compact
    ? truncateLabel(rawDisplayBranch, 10)
    : rawDisplayBranch;

  // Branch names that have worktrees (avoid duplicates)
  const worktreeBranches = new Set(
    worktrees
      .map((wt) => wt.branch_name)
      .filter((branchName): branchName is string => branchName !== null),
  );
  // Local branches without worktrees, excluding current
  const standaloneBranches = branches.filter(
    (b) => !b.is_remote && !worktreeBranches.has(b.name) && !b.is_current,
  );

  return (
    <div className="relative" ref={containerRef}>
      <Button
        type="button"
        variant={variant === "select" ? "outline" : "ghost"}
        size="sm"
        dense
        onClick={handleToggle}
        className={cn(
          coarseHitAreaCls,
          variant === "select"
            ? `inline-flex h-9 min-w-0 shrink-0 items-center gap-1 rounded-md border border-border bg-transparent px-2.5 py-2 text-sm transition-colors ${
                disabled
                  ? "cursor-not-allowed text-muted-foreground/50"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`
            : `flex min-h-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs transition-colors ${
                disabled
                  ? "cursor-not-allowed text-muted-foreground/50"
                  : "text-muted-foreground hover:bg-muted/60"
              }`,
        )}
        title={
          disabled
            ? "Attached session owns branch and worktree"
            : (worktreePath ?? "Current branch")
        }
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        disabled={disabled}
      >
        <BranchIcon />
        <span className={isDetached ? "italic" : ""}>{displayBranch}</span>
        <ChevronIcon />
      </Button>

      {isOpen && (
        <div className="absolute right-0 bottom-full z-20 mb-1 max-h-72 w-64 overflow-y-auto rounded-md border border-border bg-background shadow-lg">
          {/* Notices sit beside the listbox, never inside it: a listbox may
              only contain options and groups. */}
          {checkoutError && (
            <div
              role="alert"
              className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground"
            >
              {checkoutError}
            </div>
          )}

          {checkoutLookup.status === "loading" && (
            <div
              role="status"
              className="border-b border-border px-3 py-1.5 text-xs text-muted-foreground"
            >
              Looking up checkout...
            </div>
          )}
          {checkoutLookup.status === "none" && (
            <div
              role="status"
              className="border-b border-border px-3 py-1.5 text-xs text-muted-foreground"
            >
              {NO_CHECKOUT_MESSAGE}
            </div>
          )}
          {checkoutLookup.status === "error" && (
            <div
              role="alert"
              className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground"
            >
              Checkout lookup failed: {checkoutLookup.message}
            </div>
          )}

          <div role="listbox" aria-label="Switch branch or worktree">
            {/* Worktrees */}
            {worktrees.length > 0 && (
              <div role="group" aria-label="Worktrees">
                <div
                  aria-hidden="true"
                  className="border-b border-border px-3 py-1 text-[length:var(--text-2xs)] tracking-wider text-muted-foreground/50 uppercase"
                >
                  Worktrees
                </div>
                {worktrees.map((wt) => {
                  const isActive = worktreePath === wt.worktree_path;
                  return (
                    <Button
                      key={wt.id}
                      type="button"
                      variant="ghost"
                      size="sm"
                      dense
                      role="option"
                      aria-selected={isActive}
                      className={cn(
                        coarseHitAreaCls,
                        "flex min-h-0 w-full items-center justify-start gap-2 rounded-none border-0 px-3 py-1.5 text-left text-xs font-normal whitespace-normal hover:bg-muted",
                        isActive && "bg-accent/20 text-accent",
                      )}
                      onClick={() =>
                        handleSelectWorktree(wt.worktree_path, wt.id)
                      }
                      title={wt.worktree_path}
                    >
                      <WorktreeIcon />
                      <div className="min-w-0">
                        <div className="truncate font-medium">
                          {wt.branch_name ?? "detached"}
                        </div>
                        <div className="truncate text-[length:var(--text-2xs)] text-muted-foreground/60">
                          {wt.worktree_path}
                        </div>
                      </div>
                    </Button>
                  );
                })}
              </div>
            )}

            {/* Branches */}
            {standaloneBranches.length > 0 && (
              <div role="group" aria-label="Branches">
                <div
                  aria-hidden="true"
                  className="border-b border-border px-3 py-1 text-[length:var(--text-2xs)] tracking-wider text-muted-foreground/50 uppercase"
                >
                  Branches
                </div>
                {standaloneBranches.map((b) => (
                  <Button
                    key={b.name}
                    type="button"
                    variant="ghost"
                    size="sm"
                    dense
                    role="option"
                    aria-selected={false}
                    className={cn(
                      coarseHitAreaCls,
                      "flex min-h-0 w-full items-center justify-start gap-2 rounded-none border-0 px-3 py-1.5 text-left text-xs font-normal hover:bg-muted",
                    )}
                    onClick={() => {
                      void handleSelectBranch(b.name);
                    }}
                  >
                    <BranchIcon />
                    <span className="truncate">{b.name}</span>
                  </Button>
                ))}
              </div>
            )}
          </div>

          {worktrees.length === 0 && standaloneBranches.length === 0 && (
            <div className="px-3 py-2 text-xs text-muted-foreground">
              No other branches found
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BranchIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
    >
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  );
}

function WorktreeIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
    >
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0 opacity-50"
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}
