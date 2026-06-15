import { lazy, type ReactElement } from "react";
import { Heading } from '../shared/Heading'

export const DashboardPage = lazy(() =>
  import("../dashboard/DashboardPage").then((m) => ({
    default: m.DashboardPage,
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
