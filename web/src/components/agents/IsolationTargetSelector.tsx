import { useEffect, useState } from "react";
import { FormField } from "../ui/FormField";
import { NativeSelect } from "../ui/NativeSelect";

interface WorktreeItem {
  id: string;
  branch_name: string | null;
  worktree_path: string;
  status: string;
}

interface CloneItem {
  id: string;
  branch_name: string | null;
  clone_path: string;
  status?: string;
}

interface IsolationTargetSelectorProps {
  checkoutMode: string;
  worktreeId: string | null;
  cloneId: string | null;
  onWorktreeIdChange: (id: string | null) => void;
  onCloneIdChange: (id: string | null) => void;
}

export function IsolationTargetSelector({
  checkoutMode,
  worktreeId,
  cloneId,
  onWorktreeIdChange,
  onCloneIdChange,
}: IsolationTargetSelectorProps) {
  const [worktrees, setWorktrees] = useState<WorktreeItem[]>([]);
  const [clones, setClones] = useState<CloneItem[]>([]);

  useEffect(() => {
    if (checkoutMode === "worktree") {
      fetch("/api/source-control/worktrees?status=active")
        .then((response) => response.json())
        .then((data) => setWorktrees(data.worktrees || []))
        .catch(() => setWorktrees([]));
    } else if (checkoutMode === "clone") {
      fetch("/api/source-control/clones")
        .then((response) => response.json())
        .then((data) => setClones(data.clones || []))
        .catch(() => setClones([]));
    }
  }, [checkoutMode]);

  if (checkoutMode === "worktree" && worktrees.length > 0) {
    return (
      <FormField label="Worktree">
        {({ id, describedBy, invalid }) => (
          <NativeSelect
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            value={worktreeId || ""}
            onChange={(event) => onWorktreeIdChange(event.target.value || null)}
          >
            <option value="">New worktree</option>
            {worktrees.map((worktree) => (
              <option key={worktree.id} value={worktree.id}>
                {worktree.branch_name ?? "detached"} ({worktree.id.slice(0, 8)})
              </option>
            ))}
          </NativeSelect>
        )}
      </FormField>
    );
  }

  if (checkoutMode === "clone" && clones.length > 0) {
    return (
      <FormField label="Clone">
        {({ id, describedBy, invalid }) => (
          <NativeSelect
            id={id}
            aria-describedby={describedBy}
            error={invalid}
            value={cloneId || ""}
            onChange={(event) => onCloneIdChange(event.target.value || null)}
          >
            <option value="">New clone</option>
            {clones.map((clone) => (
              <option key={clone.id} value={clone.id}>
                {clone.branch_name ?? "detached"} ({clone.id.slice(0, 8)})
              </option>
            ))}
          </NativeSelect>
        )}
      </FormField>
    );
  }

  return null;
}
