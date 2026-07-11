import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchAsk,
  fetchGraph,
  fetchPages,
  launchResearch,
  normalizeAskAnswer,
  normalizeBacklinks,
  normalizeGraph,
  normalizePage,
  normalizePages,
  savePage,
  summarizeWikiStatus,
} from "../WikiTabData";
import {
  askRetrievalEnvelope,
  askSynthesisEnvelope,
  backlinksEnvelope,
  degradedStatusEnvelope,
  graphEnvelope,
  healthEnvelope,
  pagesEnvelope,
  readEnvelope,
  statusEnvelope,
  writeConflictBody,
  writeSuccessEnvelope,
} from "./fixtures";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch(body: unknown, status = 200) {
  const mock = vi.fn().mockResolvedValue(jsonResponse(body, status));
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("normalizeGraph", () => {
  it("flattens the keyed edge lists and camel-cases node fields", () => {
    const graph = normalizeGraph(graphEnvelope.payload);
    expect(graph.degraded).toBe(false);
    expect(graph.degradedSources).toEqual([]);
    expect(graph.nodes).toHaveLength(6);
    expect(graph.nodes[0]).toEqual({
      id: "document-knowledge-concepts-gobby-md-f7725ae2",
      kind: "wiki_page",
      path: "knowledge/concepts/gobby.md",
      title: "Gobby",
    });
    expect(graph.edges).toHaveLength(4);
    const kinds = graph.edges.map((edge) => edge.kind).sort();
    expect(kinds).toEqual(["audit", "links", "links", "trust"]);
    const linkEdge = graph.edges.find((edge) => edge.rawTarget === "code/modules/src/gobby");
    expect(linkEdge?.source).toBe("document-code-files-src-gobby-runner-py-md-3b01f8e1");
    expect(linkEdge?.target).toBe("unresolved-code-modules-src-gobby-562632cc");
  });

  it("tolerates malformed payloads", () => {
    const graph = normalizeGraph({ nodes: "nope", edges: 7 });
    expect(graph.nodes).toEqual([]);
    expect(graph.edges).toEqual([]);
    expect(graph.degraded).toBe(false);
  });
});

describe("normalizePages", () => {
  it("splits pages and outputs with camel-cased metadata", () => {
    const { pages, outputs } = normalizePages(pagesEnvelope.payload);
    expect(pages).toHaveLength(9);
    expect(pages[0]).toEqual({
      path: "knowledge/concepts/gobby.md",
      title: "Gobby",
      tags: ["gwiki", "compiled", "entity"],
      contentHash: "6d98b22ffd529e5a4d32c3c3803206d2f5f034f62592b98cb726a9e56f6a0392",
      updatedAt: "2026-07-10T23:22:43.489247+00:00",
    });
    expect(outputs).toEqual([
      {
        path: "outputs/GRAPH_REPORT.md",
        size: 616527,
        modified: "2026-07-06T04:29:04.191503297+00:00",
      },
      {
        path: "outputs/recovery/task-17804/post-recovery.md",
        size: 2263,
        modified: "2026-07-10T17:14:49.332853035+00:00",
      },
    ]);
  });

  it("drops entries without a path and defaults titles from the path", () => {
    const { pages } = normalizePages({
      pages: [{ title: "orphan" }, { path: "knowledge/concepts/x.md" }],
    });
    expect(pages).toEqual([
      {
        path: "knowledge/concepts/x.md",
        title: "x",
        tags: [],
        contentHash: null,
        updatedAt: null,
      },
    ]);
  });
});

describe("normalizePage", () => {
  it("splits frontmatter from the body via js-yaml", () => {
    const page = normalizePage(readEnvelope.payload);
    expect(page.path).toBe("knowledge/concepts/gobby.md");
    expect(page.title).toBe("Gobby");
    expect(page.frontmatter).toMatchObject({
      title: "Gobby",
      aliases: ["Gobby", "gobby-cli"],
      source_kind: "concept",
      tags: ["gwiki", "compiled"],
    });
    expect(page.body.startsWith("# Gobby")).toBe(true);
    expect(page.body).not.toContain("---");
    expect(page.contentHash).toBe(readEnvelope.payload.content_hash);
    expect(page.status).toBe("found");
  });

  it("keeps the full content as body when there is no frontmatter", () => {
    const page = normalizePage({ content: "# Plain\n\nNo frontmatter.\n" });
    expect(page.frontmatter).toEqual({});
    expect(page.body).toBe("# Plain\n\nNo frontmatter.\n");
  });

  it("degrades malformed frontmatter to an empty object without losing content", () => {
    const content = "---\ntitle: [unclosed\n---\n\nBody survives.\n";
    const page = normalizePage({ content });
    expect(page.frontmatter).toEqual({});
    expect(page.body).toContain("Body survives.");
  });
});

describe("normalizeBacklinks", () => {
  it("camel-cases backlink rows", () => {
    expect(normalizeBacklinks(backlinksEnvelope.payload)).toEqual([
      {
        sourcePath: "knowledge/topics/contract-guardrails.md",
        targetPath: "knowledge/concepts/gobby.md",
        rawTarget: "knowledge/concepts/gobby",
      },
      {
        sourcePath: "recaps/2026-07-07.md",
        targetPath: "knowledge/concepts/gobby.md",
        rawTarget: "knowledge/concepts/gobby|Gobby",
      },
    ]);
  });

  it("returns an empty list for malformed payloads", () => {
    expect(normalizeBacklinks({ backlinks: "none" })).toEqual([]);
  });
});

describe("normalizeAskAnswer", () => {
  it("normalizes a retrieval-only response", () => {
    const result = normalizeAskAnswer(askRetrievalEnvelope.payload);
    expect(result.status).toBe("retrieved");
    expect(result.answer).toBeNull();
    expect(result.citations).toEqual([]);
    expect(result.groundingWarnings).toEqual([]);
    expect(result.hits).toHaveLength(1);
    expect(result.hits[0]).toMatchObject({
      title: "Session: c1c0c073",
      wikiPage: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
      snippet: "watcher debounce and poll intervals",
      sources: ["bm25", "semantic"],
    });
    expect(result.codeCitations).toEqual([
      { file: "code/files/src/gobby/wiki/watcher.py.md", line: null, symbol: "src/gobby/wiki/watcher.py" },
    ]);
  });

  it("extracts wikilink citations from the synthesized answer", () => {
    const result = normalizeAskAnswer(askSynthesisEnvelope.payload);
    expect(result.status).toBe("answered");
    expect(result.answer).toContain("The watcher polls");
    expect(result.model).toBe("test-model");
    expect(result.citations).toEqual([
      {
        target: "knowledge/concepts/gobby",
        title: "Gobby",
        resolvedPath: null,
      },
      {
        target: "code/files/src/gobby/wiki/watcher.py",
        title: "watcher.py",
        resolvedPath: null,
      },
    ]);
  });

  it("resolves citations when given a resolver", () => {
    const result = normalizeAskAnswer(askSynthesisEnvelope.payload, (target) =>
      target === "knowledge/concepts/gobby" ? "knowledge/concepts/gobby.md" : null,
    );
    expect(result.citations[0].resolvedPath).toBe("knowledge/concepts/gobby.md");
    expect(result.citations[1].resolvedPath).toBeNull();
  });

  it("surfaces grounding warnings from the citation check and payload warnings", () => {
    const result = normalizeAskAnswer(askSynthesisEnvelope.payload);
    expect(result.groundingWarnings).toContain(
      "Unsupported claim: The watcher restarts the daemon on every write.",
    );
    expect(result.groundingWarnings).toContain("semantic search degraded");
  });
});

describe("summarizeWikiStatus", () => {
  it("reports ready when every service is configured", () => {
    const summary = summarizeWikiStatus(statusEnvelope, healthEnvelope, null);
    expect(summary.state).toBe("ready");
    expect(summary.degradedServices).toEqual([]);
    expect(summary.services).toContainEqual({ name: "postgres", configured: true });
    expect(summary.brokenLinks).toBe(1);
    expect(summary.stalePages).toBe(1);
    expect(summary.uncompiledSources).toBe(1);
  });

  it("reports degraded with the unconfigured service names", () => {
    const summary = summarizeWikiStatus(degradedStatusEnvelope, healthEnvelope, null);
    expect(summary.state).toBe("degraded");
    expect(summary.degradedServices).toEqual(["embeddings", "falkordb"]);
  });

  it("reports unavailable when the gateway errored", () => {
    const summary = summarizeWikiStatus(null, null, "HTTP 503");
    expect(summary.state).toBe("unavailable");
    expect(summary.message).toBe("HTTP 503");
    expect(summary.services).toEqual([]);
  });
});

describe("fetchers", () => {
  it("fetchGraph composes scope and include parameters", async () => {
    const mock = mockFetch(graphEnvelope);
    // "knowledge" matches the route enum (_GRAPH_INCLUDE_VALUES); "wiki" 400s.
    const graph = await fetchGraph({ projectId: "p1" }, "knowledge");
    const url = String(mock.mock.calls[0][0]);
    expect(url).toContain("/api/wiki/graph?");
    expect(url).toContain("project=p1");
    expect(url).toContain("include=knowledge");
    expect(graph.nodes).toHaveLength(6);
  });

  it("fetchPages passes prefix and topic scope", async () => {
    const mock = mockFetch(pagesEnvelope);
    const { pages } = await fetchPages({ topic: "auth" }, "knowledge/");
    const url = String(mock.mock.calls[0][0]);
    expect(url).toContain("/api/wiki/pages?");
    expect(url).toContain("topic=auth");
    expect(url).toContain("prefix=knowledge%2F");
    expect(pages).toHaveLength(9);
  });

  it("fetchAsk posts nothing — it reads /api/wiki/ask with llm and signal", async () => {
    const mock = mockFetch(askRetrievalEnvelope);
    const controller = new AbortController();
    await fetchAsk({ projectId: "p1" }, { query: "how?", llm: true, signal: controller.signal });
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/wiki/ask?");
    expect(String(url)).toContain("query=how%3F");
    expect(String(url)).toContain("llm=true");
    expect(init.signal).toBe(controller.signal);
  });

  it("throws a typed error message on non-conflict failures", async () => {
    mockFetch({ detail: "wiki gateway offline" }, 503);
    await expect(fetchPages({}, undefined)).rejects.toThrow("wiki gateway offline");
  });
});

describe("savePage", () => {
  it("posts the write body and returns the new content hash", async () => {
    const mock = mockFetch(writeSuccessEnvelope);
    const result = await savePage(
      { projectId: "p1" },
      {
        path: "knowledge/topics/example.md",
        content: "# Example\n",
        mode: "upsert",
        expectedHash: "prev-hash",
      },
    );
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/wiki/write?");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      path: "knowledge/topics/example.md",
      content: "# Example\n",
      mode: "upsert",
      expected_hash: "prev-hash",
    });
    expect(result).toEqual({
      ok: true,
      path: "knowledge/topics/example.md",
      created: false,
      contentHash: "abcd1234",
    });
  });

  it("normalizes a 412 into a typed conflict result", async () => {
    mockFetch(writeConflictBody, 412);
    const result = await savePage(
      {},
      { path: "knowledge/topics/example.md", content: "x", expectedHash: "stale" },
    );
    expect(result).toEqual({
      ok: false,
      conflict: true,
      code: "precondition_failed",
      message: "expected hash does not match current page content",
    });
  });

  it("normalizes a 409 create collision into a typed conflict result", async () => {
    mockFetch(
      {
        detail: {
          ...writeConflictBody.detail,
          payload: { code: "already_exists" },
          error: { ...writeConflictBody.detail.error, message: "page already exists" },
        },
      },
      409,
    );
    const result = await savePage({}, { path: "a.md", content: "x", mode: "create" });
    expect(result).toEqual({
      ok: false,
      conflict: true,
      code: "already_exists",
      message: "page already exists",
    });
  });

  it("throws on non-conflict write failures", async () => {
    mockFetch({ detail: "boom" }, 502);
    await expect(savePage({}, { path: "a.md", content: "x" })).rejects.toThrow("boom");
  });
});

describe("launchResearch", () => {
  it("starts the wiki-research pipeline detached", async () => {
    const mock = mockFetch({
      status: "started",
      execution_id: "exec-1",
      pipeline_name: "wiki-research",
    });
    const launch = await launchResearch({ projectId: "p1" }, { query: "local-first sync" });
    const [url, init] = mock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe("/api/pipelines/run");
    expect(JSON.parse(String(init.body))).toEqual({
      name: "wiki-research",
      inputs: { query: "local-first sync" },
      project_id: "p1",
      background: true,
    });
    expect(launch).toEqual({ executionId: "exec-1", status: "started" });
  });
});
