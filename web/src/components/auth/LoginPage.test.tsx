import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

describe("LoginPage", () => {
  it("leaves input outlines available for the global focus indicator", () => {
    render(<LoginPage credentialsConfigured onLogin={vi.fn()} />);

    expect(screen.getByLabelText("Username").style.outline).toBe("");
    expect(screen.getByLabelText("Password").style.outline).toBe("");
  });

  it("shows daemon-host setup guidance when credentials are not configured", () => {
    render(<LoginPage credentialsConfigured={false} onLogin={vi.fn()} />);

    expect(screen.getByText("gobby auth credentials")).toBeInTheDocument();
    expect(screen.getByText(/daemon host/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Username")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Sign in" }),
    ).not.toBeInTheDocument();
  });
});
