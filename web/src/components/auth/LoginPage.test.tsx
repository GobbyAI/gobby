import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

describe("LoginPage", () => {
  it("leaves input outlines available for the global focus indicator", () => {
    render(<LoginPage onLogin={vi.fn()} />);

    expect(screen.getByLabelText("Email").style.outline).toBe("");
    expect(screen.getByLabelText("Password").style.outline).toBe("");
  });

  it("submits email credentials", async (): Promise<void> => {
    const onLogin = vi.fn().mockResolvedValue(null);
    render(<LoginPage onLogin={onLogin} />);

    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "operator@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "correct-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() =>
      expect(onLogin).toHaveBeenCalledWith(
        "operator@example.com",
        "correct-password",
        false,
      ),
    );
  });
});
