import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { ConfigurationClient } from "../config";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface ServerState {
  revision: number;
  desired: Record<string, unknown>;
}

type Fetcher = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

interface ServerDouble {
  state: ServerState;
  fetcher: Mock<Fetcher>;
}

/**
 * A minimal reactive-config server double: GET returns the current snapshot,
 * PATCH commits when `expected_revision` matches and bumps the revision.
 */
function makeServer(initial: ServerState): ServerDouble {
  const state = { ...initial, desired: { ...initial.desired } };
  const fetcher: Mock<Fetcher> = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== "/api/config/values") {
        throw new Error(`Unexpected request: ${String(input)}`);
      }
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body)) as {
          expected_revision: number;
          values: Record<string, unknown>;
        };
        if (body.expected_revision !== state.revision) {
          return jsonResponse(
            {
              error: {
                code: "revision_conflict",
                message: "Configuration revision is stale",
                retryable: true,
              },
            },
            409,
          );
        }
        state.revision += 1;
        Object.assign(state.desired, body.values);
        return jsonResponse({
          committed: true,
          revision: state.revision,
          changed_keys: Object.keys(body.values),
          apply_status: "applied",
          pending_restart_keys: [],
          failed_live_keys: {},
        });
      }
      return jsonResponse({
        revision: state.revision,
        desired: { ...state.desired },
        active: { ...state.desired },
        secret_set: {},
        pending_restart_keys: [],
        failed_live_keys: {},
      });
    },
  );
  return { state, fetcher };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ConfigurationClient mutation convergence", () => {
  it("reflects_submitted_values_in_the_snapshot_after_patch", async () => {
    const { fetcher } = makeServer({
      revision: 4,
      desired: { memory: { enabled: true } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();

    const result = await client.patch({ memory: { enabled: false } });

    expect(result.kind).toBe("success");
    // No torn snapshot: the resolved patch has already refetched, so revision
    // and values advance together.
    expect(client.currentSnapshot?.revision).toBe(5);
    expect(client.currentSnapshot?.desired).toEqual({
      memory: { enabled: false },
    });
  });

  it("never_publishes_a_new_revision_with_stale_values", async () => {
    const { fetcher } = makeServer({
      revision: 4,
      desired: { memory: { enabled: true } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();

    const published: Array<{ revision: number; desired: unknown }> = [];
    client.subscribe((snapshot) => {
      published.push({
        revision: snapshot.revision,
        desired: snapshot.desired,
      });
    });
    await client.patch({ memory: { enabled: false } });

    const atFive = published.filter((snapshot) => snapshot.revision === 5);
    expect(atFive.length).toBeGreaterThan(0);
    for (const snapshot of atFive) {
      expect(snapshot.desired).toEqual({ memory: { enabled: false } });
    }
  });

  it("own_commit_ws_event_leaves_the_converged_snapshot_intact", async () => {
    const { fetcher } = makeServer({
      revision: 4,
      desired: { memory: { enabled: true } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();
    const result = await client.patch({ memory: { enabled: false } });
    expect(result.kind).toBe("success");
    const requestCountBeforeOwnCommit = fetcher.mock.calls.length;

    // The daemon broadcasts the committed revision to every client, including
    // the author. Observing it must not disturb (or be needed for) the
    // already-converged snapshot.
    client.observeRevision(5);
    await Promise.resolve();

    await vi.waitFor(() => {
      expect(client.currentSnapshot?.revision).toBe(5);
      expect(client.currentSnapshot?.desired).toEqual({
        memory: { enabled: false },
      });
    });
    expect(fetcher).toHaveBeenCalledTimes(requestCountBeforeOwnCommit);
  });

  it("advances_the_committed_revision_without_publishing_stale_values", async () => {
    const { state, fetcher } = makeServer({
      revision: 4,
      desired: { memory: { enabled: true } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();
    const serverFetcher = fetcher.getMockImplementation();
    let resolvePostCommitRefresh: (response: Response) => void = () => {
      throw new Error("post-commit refresh resolver was not installed");
    };
    let holdNextValuesRead = false;
    fetcher.mockImplementation(async (input, init) => {
      if (init?.method === "PATCH") {
        const response = await serverFetcher!(input, init);
        holdNextValuesRead = true;
        return response;
      }
      if (holdNextValuesRead) {
        holdNextValuesRead = false;
        return new Promise<Response>((resolve) => {
          resolvePostCommitRefresh = resolve;
        });
      }
      return serverFetcher!(input, init);
    });
    const published: number[] = [];
    client.subscribe((snapshot) => published.push(snapshot.revision));

    const patch = client.patch({ memory: { enabled: false } });
    await vi.waitFor(() => {
      expect(client.currentSnapshot?.revision).toBe(state.revision);
    });

    expect(client.currentSnapshot?.desired).toEqual({
      memory: { enabled: true },
    });
    expect(published).not.toContain(state.revision);

    resolvePostCommitRefresh(await serverFetcher!("/api/config/values"));
    await expect(patch).resolves.toMatchObject({ kind: "success" });
  });

  it("returns_success_and_uses_own_commit_event_when_the_post_patch_refetch_rejects", async () => {
    const { fetcher } = makeServer({
      revision: 4,
      desired: { memory: { enabled: true } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();
    const serverFetcher = fetcher.getMockImplementation();
    let rejectPostCommitRefresh: (reason?: unknown) => void = () => {
      throw new Error("post-commit refresh rejector was not installed");
    };
    let holdNextValuesRead = false;
    fetcher.mockImplementation(async (input, init) => {
      if (init?.method === "PATCH") {
        const response = await serverFetcher!(input, init);
        holdNextValuesRead = true;
        return response;
      }
      if (holdNextValuesRead) {
        holdNextValuesRead = false;
        return new Promise<Response>((_resolve, reject) => {
          rejectPostCommitRefresh = reject;
        });
      }
      return serverFetcher!(input, init);
    });

    const patch = client.patch({ memory: { enabled: false } });
    await vi.waitFor(() => {
      expect(client.currentSnapshot?.revision).toBe(5);
    });

    // The daemon's own-commit event commonly arrives while the PATCH-owned
    // refresh is still in flight. Reject that refresh only after observing it.
    client.observeRevision(5);
    await Promise.resolve();
    rejectPostCommitRefresh(new Error("post-commit refresh failed"));
    const result = await patch;

    expect(result.kind).toBe("success");
    await vi.waitFor(() => {
      expect(client.currentSnapshot?.revision).toBe(5);
      expect(client.currentSnapshot?.desired).toEqual({
        memory: { enabled: false },
      });
    });
  });

  it("returns_success_when_the_post_yaml_replace_refetch_rejects", async () => {
    let revision = 4;
    let rejectNextValuesRead = false;
    const fetcher: Mock<Fetcher> = vi.fn(async (input, init) => {
      if (String(input) === "/api/config/template" && init?.method === "PUT") {
        revision = 5;
        rejectNextValuesRead = true;
        return jsonResponse({
          committed: true,
          revision,
          changed_keys: ["memory.enabled"],
          apply_status: "applied",
          pending_restart_keys: [],
          failed_live_keys: {},
        });
      }
      if (String(input) === "/api/config/values") {
        if (rejectNextValuesRead) {
          rejectNextValuesRead = false;
          throw new Error("post-commit refresh failed");
        }
        return jsonResponse({
          revision,
          desired: { memory: { enabled: revision === 4 } },
          active: { memory: { enabled: revision === 4 } },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        });
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();

    const result = await client.saveTemplate("memory:\n  enabled: false\n");

    expect(result.kind).toBe("success");
    client.observeRevision(5);
    await vi.waitFor(() => {
      expect(client.currentSnapshot?.desired).toEqual({
        memory: { enabled: false },
      });
    });
  });

  it("observe_revision_ignores_malformed_ws_payloads", () => {
    const { fetcher } = makeServer({ revision: 1, desired: {} });
    const client = new ConfigurationClient(fetcher);

    for (const value of [
      undefined,
      null,
      "not-a-number",
      -1,
      1.5,
      Number.NaN,
    ]) {
      expect(() => client.observeRevision(value)).not.toThrow();
    }
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("reset_cancels_an_observed_revision_refresh_before_its_microtask", async () => {
    const { fetcher } = makeServer({ revision: 1, desired: {} });
    const client = new ConfigurationClient(fetcher);

    client.observeRevision(2);
    client.reset();
    await Promise.resolve();

    expect(fetcher).not.toHaveBeenCalled();
    expect(client.currentSnapshot).toBeNull();
  });

  it("reset_ignores_an_in_flight_refresh_continuation", async () => {
    let resolveRequest: (response: Response) => void = () => {
      throw new Error("request resolver was not installed");
    };
    const fetcher: Mock<Fetcher> = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
    );
    const client = new ConfigurationClient(fetcher);
    const refresh = client.fetchValues();

    client.reset();
    resolveRequest(
      jsonResponse({
        revision: 9,
        desired: { memory: { enabled: true } },
        active: { memory: { enabled: true } },
        secret_set: {},
        pending_restart_keys: [],
        failed_live_keys: {},
      }),
    );
    await refresh;

    expect(client.currentSnapshot).toBeNull();
  });

  it("patch_last_write_wins_retries_once_after_a_conflict", async () => {
    const { state, fetcher } = makeServer({
      revision: 4,
      desired: { ui_settings: { theme: "dark" } },
    });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();
    // Another writer commits first: the server is at 5 while the client
    // believes 4, so the first PATCH conflicts.
    state.revision = 5;

    const result = await client.patchLastWriteWins({
      ui_settings: { theme: "light" },
    });

    expect(result.kind).toBe("success");
    expect(state.desired).toEqual({ ui_settings: { theme: "light" } });
    const patches = fetcher.mock.calls.filter(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patches).toHaveLength(2);
  });

  it("patch_last_write_wins_does_not_loop_on_repeated_conflicts", async () => {
    const { state, fetcher } = makeServer({ revision: 4, desired: {} });
    const client = new ConfigurationClient(fetcher);
    await client.fetchValues();
    // The server stays permanently ahead of whatever the client refreshes to.
    const originalFetcher = fetcher.getMockImplementation();
    fetcher.mockImplementation(async (input, init) => {
      if (init?.method === "PATCH") state.revision += 1;
      return originalFetcher!(input, init);
    });

    const result = await client.patchLastWriteWins({
      ui_settings: { theme: "light" },
    });

    expect(result.kind).toBe("conflict");
    const patches = fetcher.mock.calls.filter(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patches).toHaveLength(2);
  });
});
