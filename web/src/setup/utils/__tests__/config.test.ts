import { parse } from "yaml";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { patchPorts } from "../config.js";

const mocks = vi.hoisted(() => ({
  existsSync: vi.fn(),
  mkdirSync: vi.fn(),
  readFileSync: vi.fn(),
  writeFileSync: vi.fn(),
}));

vi.mock("fs", () => ({
  default: {
    existsSync: mocks.existsSync,
    mkdirSync: mocks.mkdirSync,
    readFileSync: mocks.readFileSync,
    writeFileSync: mocks.writeFileSync,
  },
  existsSync: mocks.existsSync,
  mkdirSync: mocks.mkdirSync,
  readFileSync: mocks.readFileSync,
  writeFileSync: mocks.writeFileSync,
}));

vi.mock("../state.js", () => ({ getGobbyHome: () => "/tmp/gobby-test" }));

describe("patchPorts", () => {
  beforeEach(() => {
    mocks.existsSync.mockReset();
    mocks.mkdirSync.mockReset();
    mocks.readFileSync.mockReset();
    mocks.writeFileSync.mockReset();
    mocks.existsSync.mockReturnValue(true);
    mocks.readFileSync.mockReturnValue("bind_host: 0.0.0.0\n");
  });

  it("cannot persist a non-local bind host without firewall configuration", () => {
    patchPorts(60887, 60888, 60889, false);

    const written = mocks.writeFileSync.mock.calls[0]?.[1];
    expect(typeof written).toBe("string");
    expect(parse(written as string)).toMatchObject({
      bind_host: "127.0.0.1",
      daemon_port: 60887,
      websocket_port: 60888,
      ui_port: 60889,
    });
  });

  it("preserves an explicitly firewalled bind host", () => {
    patchPorts(60887, 60888, 60889, true);

    const written = mocks.writeFileSync.mock.calls[0]?.[1];
    expect(parse(written as string)).toMatchObject({ bind_host: "0.0.0.0" });
  });
});
