import { useState, useEffect, useRef, useCallback } from "react";
import type { WorktreeInfo } from "../../hooks/useSourceControl";
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

async function readCheckoutError(
  response: Response,
  branchName: string,
): Promise<string> {
  try {
    const data: unknown = await response.json();
    const detail = (data as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
  } catch {
    // Fall through to status text.
  }
  return response.statusText || `Failed to switch to ${branchName}`;
}

function readCheckoutRoot(data: unknown): string | null {
  if (typeof data !== "object" || data === null || !("checkout" in data)) {
    return null;
  }
  const checkout = data.checkout;
  if (
    typeof checkout !== "object" ||
    checkout === null ||
    !("root_path" in checkout) ||
    typeof checkout.root_path !== "string"
  ) {
    return null;
  }
  return checkout.root_path;
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
    rootPath: string | null;
  }>({ projectId: null, rootPath: null });
  const [apiBranch, setApiBranch] = useState<string | null>(null);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const buildParams = useCallback(() => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    return params;
  }, [projectId]);

  const fetchProjectCheckout = useCallback(async (): Promise<string | null> => {
    if (!projectId) return null;
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/checkouts`,
    );
    if (!response.ok) return null;
    return readCheckoutRoot(await response.json());
  }, [projectId]);
  const mainRepoPath =
    checkoutState.projectId === projectId ? checkoutState.rootPath : undefined;

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
    fetchProjectCheckout()
      .then((rootPath) => {
        if (!stale) setCheckoutState({ projectId, rootPath });
      })
      .catch(() => {
        if (!stale) setCheckoutState({ projectId, rootPath: null });
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
    const [wtRes, brRes, statusRes, checkoutRes] = await Promise.allSettled([
      fetch(`/api/source-control/worktrees?${params}`),
      fetch(`/api/source-control/branches?${params}`),
      fetch(`/api/source-control/status?${params}`),
      fetchProjectCheckout(),
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
    setCheckoutState({
      projectId,
      rootPath: checkoutRes.status === "fulfilled" ? checkoutRes.value : null,
    });
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
      setCheckoutError("Repository path unavailable");
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
        setCheckoutError(await readCheckoutError(response, branchName));
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
        <div
          className="absolute right-0 bottom-full z-20 mb-1 max-h-72 w-64 overflow-y-auto rounded-md border border-border bg-background shadow-lg"
          role="listbox"
          aria-label="Switch branch or worktree"
        >
          {checkoutError && (
            <div
              role="alert"
              className="border-b border-border px-3 py-1.5 text-xs text-destructive-foreground"
            >
              {checkoutError}
            </div>
          )}

          {mainRepoPath === null && (
            <div
              role="status"
              className="border-b border-border px-3 py-1.5 text-xs text-muted-foreground"
            >
              No checkout registered for this project
            </div>
          )}

          {/* Worktrees */}
          {worktrees.length > 0 && (
            <>
              <div className="border-b border-border px-3 py-1 text-[length:var(--text-2xs)] tracking-wider text-muted-foreground/50 uppercase">
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
            </>
          )}

          {/* Branches */}
          {standaloneBranches.length > 0 && (
            <>
              <div className="border-b border-border px-3 py-1 text-[length:var(--text-2xs)] tracking-wider text-muted-foreground/50 uppercase">
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
            </>
          )}

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
