import { Heading } from "../shared/Heading";
import {
  WORKFLOWS_CONTENT_CLS,
  WORKFLOWS_EMPTY_CLS,
  WORKFLOWS_PAGE_CLS,
  WORKFLOWS_TOOLBAR_CLS,
  WORKFLOWS_TOOLBAR_LEFT_CLS,
  WORKFLOWS_TOOLBAR_TITLE_CLS,
} from "./workflows-styles";

interface WorkflowsPageProps {
  projectId?: string;
}

export function WorkflowsPage(_props: WorkflowsPageProps = {}) {
  return (
    <main className={WORKFLOWS_PAGE_CLS}>
      <div className={WORKFLOWS_TOOLBAR_CLS}>
        <div className={WORKFLOWS_TOOLBAR_LEFT_CLS}>
          <Heading level={1} className={WORKFLOWS_TOOLBAR_TITLE_CLS}>
            Workflows
          </Heading>
        </div>
      </div>
      <div className={WORKFLOWS_CONTENT_CLS}>
        <div className={WORKFLOWS_EMPTY_CLS}>
          Workflow definitions are managed from Activity.
        </div>
      </div>
    </main>
  );
}
