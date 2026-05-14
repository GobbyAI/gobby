import { lazy, type ReactElement } from "react";
import { Heading } from '../shared/Heading'

export const MemoryPage = lazy(() =>
  import("../memory/MemoryPage").then((m) => ({
    default: m.MemoryPage,
  })),
);
export const ProjectsPage = lazy(() =>
  import("../projects/ProjectsPage").then((m) => ({
    default: m.ProjectsPage,
  })),
);
export const TasksPage = lazy(() =>
  import("../tasks/TasksPage").then((m) => ({
    default: m.TasksPage,
  })),
);
export const SkillsPage = lazy(() =>
  import("../skills/SkillsPage").then((m) => ({
    default: m.SkillsPage,
  })),
);
export const McpPage = lazy(() =>
  import("../mcp/McpPage").then((m) => ({ default: m.McpPage })),
);
export const IntegrationsPage = lazy(() =>
  import("../integrations/IntegrationsPage").then((m) => ({
    default: m.IntegrationsPage,
  })),
);
export const CronJobsPage = lazy(() =>
  import("../CronJobsPage").then((m) => ({
    default: m.CronJobsPage,
  })),
);
export const ConfigurationPage = lazy(() =>
  import("../ConfigurationPage").then((m) => ({
    default: m.ConfigurationPage,
  })),
);
export const WorkflowsPage = lazy(() =>
  import("../workflows/WorkflowsPage").then((m) => ({
    default: m.WorkflowsPage,
  })),
);
export const ReportsPage = lazy(() =>
  import("../workflows/ReportsPage").then((m) => ({
    default: m.ReportsPage,
  })),
);
export const DashboardPage = lazy(() =>
  import("../dashboard/DashboardPage").then((m) => ({
    default: m.DashboardPage,
  })),
);
export const TracesPage = lazy(() =>
  import("../traces/TracesPage").then((m) => ({
    default: m.TracesPage,
  })),
);

export function ComingSoonPage({ title }: { title: string }): ReactElement {
  return (
    <main className="coming-soon-page">
      <div className="coming-soon-content">
        <Heading level={2}>{title}</Heading>
        <p>Coming Soon</p>
      </div>
    </main>
  );
}
