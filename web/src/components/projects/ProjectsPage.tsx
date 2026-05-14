import { useState, useMemo, useCallback } from "react";
import { TabBar } from "../shared/TabBar";
import { useProjects } from "../../hooks/useProjects";
import { useSourceControl } from "../../hooks/useSourceControl";
import { CodeGraphExplorer } from "../code-graph/CodeGraphExplorer";
import { ProjectSettings } from "./ProjectSettings";
import { ProjectSummary } from "./ProjectSummary";
import { SourceControlView } from "../source-control/SourceControlView";
import { PullRequestsView } from "../source-control/PullRequestsView";
import { IssuesView } from "../source-control/IssuesView";
import { CICDView } from "../source-control/CICDView";
import { FilesTab } from "../activity/FilesTab";
import { Heading } from '../shared/Heading'

const PAGE_CLS = "flex flex-1 flex-col overflow-hidden";
const PAGE_HEADER_CLS = "shrink-0 px-6 max-md:px-3";
const PAGE_CONTENT_CLS = "flex-1 overflow-y-auto px-6 py-4";
const EMPTY_CLS =
  "flex items-center justify-center p-12 text-[length:var(--text-base)] text-[var(--text-muted)]";

type ProjectsTab =
  | "overview"
  | "files"
  | "graph"
  | "source-control"
  | "issues"
  | "prs"
  | "cicd"
  | "settings";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "files", label: "Files" },
  { id: "source-control", label: "Source Control" },
  { id: "issues", label: "Issues" },
  { id: "prs", label: "PR" },
  { id: "cicd", label: "CI/CD" },
  { id: "settings", label: "Settings" },
];

interface ProjectsPageProps {
  projectId?: string | null;
}

export function ProjectsPage({ projectId }: ProjectsPageProps = {}) {
  const [activeTab, setActiveTab] = useState<ProjectsTab>("overview");
  const {
    allProjects,
    isLoading: _isLoading,
    selectedProject,
    updateProject,
    deleteProject,
  } = useProjects();

  const sc = useSourceControl(projectId ?? null);

  const activeProject = useMemo(() => {
    if (selectedProject) return selectedProject;
    if (projectId) return allProjects.find((p) => p.id === projectId) ?? null;
    return null;
  }, [selectedProject, projectId, allProjects]);
  const headingText = activeProject ? `Project: ${activeProject.display_name}` : "Projects";

  const handleSave = useCallback(
    async (fields: Record<string, string | string[] | null>) => {
      if (!activeProject) return false;
      return updateProject(activeProject.id, fields);
    },
    [activeProject, updateProject],
  );

  const handleDelete = useCallback(async () => {
    if (!activeProject) return false;
    return deleteProject(activeProject.id);
  }, [activeProject, deleteProject]);

  const renderSettingsTab = () => {
    if (!activeProject) {
      return (
        <div className={EMPTY_CLS}>
          Select a project from the header to configure settings.
        </div>
      );
    }
    return (
      <ProjectSettings
        project={activeProject}
        onSave={handleSave}
        onDelete={handleDelete}
      />
    );
  };

  return (
    <main className={PAGE_CLS}>
      <Heading level={1} className="sr-only">{headingText}</Heading>
      <div className={PAGE_HEADER_CLS}>
        <TabBar
          tabs={TABS}
          activeTab={activeTab}
          onTabChange={(id) => setActiveTab(id as ProjectsTab)}
        />
      </div>

      <div className={PAGE_CONTENT_CLS}>
        {activeTab === "overview" &&
          (activeProject ? (
            <ProjectSummary project={activeProject} />
          ) : (
            <div className={EMPTY_CLS}>
              Select a project from the header to view overview.
            </div>
          ))}

        {activeTab === "files" && <FilesTab projectId={projectId ?? null} layout="responsive-split" />}

        {activeTab === "graph" && (
          <CodeGraphExplorer projectId={projectId ?? null} />
        )}

        {activeTab === "source-control" && (
          <SourceControlView
            branches={sc.branches}
            worktrees={sc.worktrees}
            clones={sc.clones}
            currentBranch={sc.status?.current_branch || null}
            fetchCommits={sc.fetchCommits}
            fetchDiff={sc.fetchDiff}
            onSyncWorktree={sc.syncWorktree}
            onDeleteWorktree={sc.deleteWorktree}
            onSyncClone={sc.syncClone}
            onDeleteClone={sc.deleteClone}
            onCleanupWorktrees={sc.cleanupWorktrees}
          />
        )}

        {activeTab === "issues" && (
          <IssuesView
            issues={sc.issues}
            githubAvailable={sc.status?.github_available || false}
            fetchIssues={sc.fetchIssues}
            fetchIssueDetail={sc.fetchIssueDetail}
          />
        )}

        {activeTab === "prs" && (
          <PullRequestsView
            prs={sc.prs}
            githubAvailable={sc.status?.github_available || false}
            fetchPrs={sc.fetchPrs}
            fetchPrDetail={sc.fetchPrDetail}
          />
        )}

        {activeTab === "cicd" && (
          <CICDView
            runs={sc.ciRuns}
            githubAvailable={sc.status?.github_available || false}
          />
        )}

        {activeTab === "settings" && renderSettingsTab()}
      </div>
    </main>
  );
}
