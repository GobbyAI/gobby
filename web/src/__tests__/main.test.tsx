import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppErrorBoundary } from "../components/app/AppErrorBoundary";

const { renderRoot } = vi.hoisted(() => ({ renderRoot: vi.fn() }));

vi.mock("react-dom/client", () => ({
  createRoot: vi.fn(() => ({ render: renderRoot })),
}));

vi.mock("../App", () => ({
  default: () => <div>App</div>,
}));

describe("application root", () => {
  beforeEach(() => {
    renderRoot.mockClear();
    document.body.innerHTML = '<div id="root"></div>';
  });

  it("mounts App inside the root error boundary", async () => {
    await import("../main");

    expect(renderRoot).toHaveBeenCalledOnce();
    const rootElement = renderRoot.mock.calls[0][0];
    expect(rootElement.type).toBe(AppErrorBoundary);
    expect(rootElement.props.activeTab).toBe("application");
    expect(rootElement.props.children).toBeTruthy();
  });
});
