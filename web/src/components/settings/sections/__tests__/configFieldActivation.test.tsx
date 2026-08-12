import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SettingsSectionFields } from "../SettingsSection";
import { TextConfigField } from "../configFields";

function makeFields(
  overrides: Partial<SettingsSectionFields> = {},
): SettingsSectionFields {
  const desired: Record<string, unknown> = {
    "service.live": "live-value",
    "service.restart": "desired-value",
    "service.failed": "failed-value",
    "service.managed": "catalog-key",
    "service.secret": "desired-secret",
  };
  const active: Record<string, unknown> = {
    "service.live": "live-value",
    "service.restart": "active-value",
    "service.failed": "old-value",
    "service.managed": "old-catalog-key",
    "service.secret": "active-secret",
  };
  return {
    getValue: (path) => desired[path],
    getActiveValue: (path) => active[path],
    setValue: vi.fn(),
    schema: {
      type: "object",
      properties: {
        "service.live": { type: "string", activation: "live" },
        "service.restart": { type: "string", activation: "restart_required" },
        "service.failed": { type: "string", activation: "live" },
        "service.managed": { type: "string", activation: "managed" },
        "service.secret": {
          type: "string",
          activation: "restart_required",
          secrecy: "reference",
        },
      },
    },
    secretKeys: [],
    pendingRestartKeys: ["service.restart", "service.secret"],
    failedLiveKeys: {
      "service.failed": { revision: 12, subscriber: "memory-runtime" },
    },
    isLoading: false,
    ...overrides,
  };
}

describe("configuration field activation", () => {
  it("renders_activation_class_and_pending_restart_state", () => {
    render(
      <>
        <TextConfigField
          fields={makeFields()}
          path="service.live"
          label="Live field"
          ariaLabel="Live field"
        />
        <TextConfigField
          fields={makeFields()}
          path="service.restart"
          label="Restart field"
          ariaLabel="Restart field"
        />
      </>,
    );

    expect(screen.getByText("Live activation")).toBeInTheDocument();
    expect(screen.getByText("Restart required")).toBeInTheDocument();
    expect(screen.getByText("Desired: desired-value")).toBeInTheDocument();
    expect(screen.getByText("Active: active-value")).toBeInTheDocument();
  });

  it("routes_managed_keys_and_shows_failed_live_status", () => {
    const setValue = vi.fn();
    const managedAction = vi.fn();
    const fields = makeFields({ setValue });
    render(
      <>
        <TextConfigField
          fields={fields}
          path="service.failed"
          label="Failed field"
          ariaLabel="Failed field"
        />
        <TextConfigField
          fields={fields}
          path="service.managed"
          label="Managed field"
          ariaLabel="Managed field"
          managedAction={managedAction}
        />
      </>,
    );

    expect(
      screen.getByText("Live apply failed in memory-runtime at revision 12"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Managed field"), {
      target: { value: "next-catalog-key" },
    });
    expect(managedAction).toHaveBeenCalledWith("next-catalog-key");
    expect(setValue).not.toHaveBeenCalledWith(
      "service.managed",
      "next-catalog-key",
    );
  });

  it("masks_reference_secret_status_from_schema_even_without_secret_key_metadata", () => {
    render(
      <TextConfigField
        fields={makeFields()}
        path="service.secret"
        label="Secret field"
        ariaLabel="Secret field"
      />,
    );

    expect(screen.getByText("Desired: ********")).toBeInTheDocument();
    expect(screen.getByText("Active: ********")).toBeInTheDocument();
    expect(
      screen.queryByText(/desired-secret|active-secret/),
    ).not.toBeInTheDocument();
  });
});
