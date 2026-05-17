import { useEffect, useMemo } from "react";
import { monitorForElements } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";

import type { GobbyTask } from "../../hooks/useTasks";
import type { StageRegistryEntry } from "../../hooks/useStagesRegistry";
import { canonicalBoardStage } from "../../lib/stageActions";
import { isTaskClosed } from "../../lib/taskState";
import { isRetiredStageName } from "../../lib/taskNormalization";
import { TasksBoardColumn } from "./TasksBoardColumn";

interface TasksBoardViewProps {
  tasks: GobbyTask[];
  stagesRegistry: ReadonlyArray<StageRegistryEntry>;
  selectedTaskId: string | null;
  onSelectTask: (id: string) => void;
  onMoveTaskToStage: (taskId: string, targetStageName: string) => Promise<void>;
  /** Per-task move errors from the optimistic stage-move hook. */
  moveErrors?: Record<string, string | null>;
}

/**
 * D6 — the Jira-style board for the activity Tasks tab. Columns are lifecycle
 * stages (the decided board model), ordered by the registry; cards are the
 * tab's filtered tasks bucketed by their canonical board stage. Drag a card
 * onto a lane → a stage move via the injected optimistic hook. Selection is
 * shared with the List view so both drive the same D5 detail pane.
 */
export function TasksBoardView({
  tasks,
  stagesRegistry,
  selectedTaskId,
  onSelectTask,
  onMoveTaskToStage,
  moveErrors,
}: TasksBoardViewProps) {
  const orderedStages = useMemo(
    () =>
      stagesRegistry
        .filter((stage) => !isRetiredStageName(stage.name))
        .slice()
        .sort(
          (a, b) =>
            (a.sequence_order ?? a.position ?? 0) -
            (b.sequence_order ?? b.position ?? 0),
        ),
    [stagesRegistry],
  );

  const { byStage, unstaged } = useMemo(() => {
    const known = new Set(orderedStages.map((stage) => stage.name));
    const firstStage = orderedStages[0]?.name ?? null;
    const lastStage = orderedStages[orderedStages.length - 1]?.name ?? null;
    const grouped = new Map<string, GobbyTask[]>();
    const orphans: GobbyTask[] = [];
    for (const task of tasks) {
      const canonical = canonicalBoardStage(task)?.name ?? null;
      // Built tasks carry a real stage manifest. Unbuilt/backlog tasks have
      // no stages, so place them by honest lifecycle position — closed at the
      // terminal column, everything else at the first column — instead of a
      // junk Unstaged lane. Category is deliberately NOT used: it only governs
      // which stages a *build* skips, and an unbuilt task has no manifest.
      const stageName =
        canonical && known.has(canonical)
          ? canonical
          : isTaskClosed(task)
            ? lastStage
            : firstStage;
      if (stageName && known.has(stageName)) {
        const bucket = grouped.get(stageName);
        if (bucket) bucket.push(task);
        else grouped.set(stageName, [task]);
      } else {
        orphans.push(task);
      }
    }
    return { byStage: grouped, unstaged: orphans };
  }, [orderedStages, tasks]);

  useEffect(() => {
    return monitorForElements({
      canMonitor: ({ source }) => source.data.type === "activity-board-card",
      onDrop: ({ source, location }) => {
        const taskId =
          typeof source.data.taskId === "string" ? source.data.taskId : null;
        const target = location.current.dropTargets.find(
          (record) => record.data.type === "activity-board-column",
        );
        const stageName =
          typeof target?.data.stageName === "string"
            ? target.data.stageName
            : null;
        if (!taskId || !stageName) return;
        void onMoveTaskToStage(taskId, stageName).catch(() => {
          // Surfaced via moveErrors banner; hook already rolled back.
        });
      },
    });
  }, [onMoveTaskToStage]);

  const activeError = moveErrors
    ? Object.values(moveErrors).find((message) => Boolean(message)) ?? null
    : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {activeError && (
        <div
          role="alert"
          className="border-b border-border px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--color-error)]"
        >
          {activeError}
        </div>
      )}
      <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto overflow-y-hidden p-3">
        {unstaged.length > 0 && (
          <TasksBoardColumn
            stageName={null}
            title="Unstaged"
            tasks={unstaged}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
          />
        )}
        {orderedStages.map((stage) => (
          <TasksBoardColumn
            key={stage.name}
            stageName={stage.name}
            title={stage.display_name || stage.name}
            tasks={byStage.get(stage.name) ?? []}
            selectedTaskId={selectedTaskId}
            onSelectTask={onSelectTask}
          />
        ))}
      </div>
    </div>
  );
}
