/**
 * Wiki envelope fixtures shaped from live captures:
 * - `gwiki pages/read/backlinks/ask/status/health --format json` against the
 *   real vault (2026-07-10), wrapped in the daemon gateway envelope
 *   `{ok, command, payload, stderr}` from GwikiGateway._success_envelope.
 * - Graph subset shaped from `wiki/outputs/graph.json` (1,846 nodes in the
 *   live vault) carrying every node kind and the links/trust/audit edge lists.
 */

export interface EnvelopeFixture {
  ok: boolean;
  command: string;
  payload: Record<string, unknown>;
  stderr: string;
}

const scope = { id: "d45545c5-ded5-4335-b115-0245752edacf", kind: "project" };

export const pagesEnvelope: EnvelopeFixture = {
  ok: true,
  command: "pages",
  stderr: "",
  payload: {
    command: "pages",
    scope,
    pages: [
      {
        content_hash: "6d98b22ffd529e5a4d32c3c3803206d2f5f034f62592b98cb726a9e56f6a0392",
        path: "knowledge/concepts/gobby.md",
        tags: ["gwiki", "compiled", "entity"],
        title: "Gobby",
        updated_at: "2026-07-10T23:22:43.489247+00:00",
      },
      {
        content_hash: "0db1596ac2f34b7cd2e04a491b6924063c4612ad4a1e699121471c61ac154580",
        path: "knowledge/concepts/gwiki.md",
        tags: ["gwiki", "compiled", "entity"],
        title: "Gwiki",
        updated_at: "2026-07-10T23:22:43.489247+00:00",
      },
      {
        content_hash: "aa11",
        path: "knowledge/topics/contract-guardrails.md",
        tags: [],
        title: "Contract guardrails",
        updated_at: "2026-07-08T10:00:00+00:00",
      },
      {
        content_hash: "bb22",
        path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        tags: [],
        title: "Session: c1c0c073",
        updated_at: "2026-07-09T10:00:00+00:00",
      },
      {
        content_hash: "cc33",
        path: "code/_architecture.md",
        tags: [],
        title: "Architecture Overview",
        updated_at: "2026-07-10T23:22:15.321100+00:00",
      },
      {
        content_hash: "dd44",
        path: "code/files/src/gobby/runner.py.md",
        tags: [],
        title: "src/gobby/runner.py",
        updated_at: "2026-07-10T23:22:15.337368+00:00",
      },
      {
        content_hash: "ee55",
        path: "code/files/crates/gwiki/src/recap.rs.md",
        tags: [],
        title: "crates/gwiki/src/recap.rs",
        updated_at: "2026-07-10T23:22:15.337368+00:00",
      },
      {
        content_hash: "ff66",
        path: "recaps/2026-07-07.md",
        tags: [],
        title: "2026-07-07",
        updated_at: "2026-07-07T23:59:00+00:00",
      },
      {
        content_hash: "0077",
        path: "_index.md",
        tags: [],
        title: "Wiki Index",
        updated_at: "2026-07-01T00:00:00+00:00",
      },
    ],
    outputs: [
      {
        modified: "2026-07-06T04:29:04.191503297+00:00",
        path: "outputs/GRAPH_REPORT.md",
        size: 616527,
      },
      {
        modified: "2026-07-10T17:14:49.332853035+00:00",
        path: "outputs/recovery/task-17804/post-recovery.md",
        size: 2263,
      },
    ],
  },
};

export const graphEnvelope: EnvelopeFixture = {
  ok: true,
  command: "graph",
  stderr: "",
  payload: {
    command: "graph",
    degraded: false,
    degraded_sources: [],
    nodes: [
      {
        id: "document-knowledge-concepts-gobby-md-f7725ae2",
        kind: "wiki_page",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/concepts/gobby.md",
        title: "Gobby",
      },
      {
        id: "document-code-files-src-gobby-runner-py-md-3b01f8e1",
        kind: "code",
        scope_kind: "project",
        scope_id: scope.id,
        path: "code/files/src/gobby/runner.py.md",
        title: "src/gobby/runner.py",
      },
      {
        id: "document-knowledge-sources-src-8218-md-b6d2ad9b",
        kind: "document",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        title: "Session: c1c0c073",
      },
      {
        id: "source-node-56b79c2d",
        kind: "source",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        title: "Session: c1c0c073",
      },
      {
        id: "citation-code-files-src-gobby-runner-py-md-47000a83",
        kind: "citation",
        scope_kind: "project",
        scope_id: scope.id,
        path: null,
        title: null,
      },
      {
        id: "unresolved-code-modules-src-gobby-562632cc",
        kind: "unresolved_target",
        scope_kind: "project",
        scope_id: scope.id,
        path: null,
        title: "code/modules/src/gobby",
      },
    ],
    edges: {
      links: [
        {
          source: "document-code-files-src-gobby-runner-py-md-3b01f8e1",
          target: "unresolved-code-modules-src-gobby-562632cc",
          kind: "links",
          raw_target: "code/modules/src/gobby",
        },
        {
          source: "document-knowledge-concepts-gobby-md-f7725ae2",
          target: "document-knowledge-sources-src-8218-md-b6d2ad9b",
          kind: "links",
          raw_target: "knowledge/sources/src-82182128d032cefe-session-c1c0c073",
        },
      ],
      imports: [],
      calls: [],
      callers: [],
      trust: [
        {
          source: "document-knowledge-sources-src-8218-md-b6d2ad9b",
          target: "source-node-56b79c2d",
          kind: "trust",
        },
      ],
      audit: [
        {
          source: "citation-code-files-src-gobby-runner-py-md-47000a83",
          target: "document-code-files-src-gobby-runner-py-md-3b01f8e1",
          kind: "audit",
        },
      ],
    },
    analytics: {
      bridges: [],
      centrality: [],
      communities: [],
      god_nodes: [],
      hotspots: [],
      unexpected_links: [],
    },
  },
};

export const readEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "knowledge/concepts/gobby.md" },
    wiki_path: "knowledge/concepts/gobby.md",
    absolute_path: "/Users/josh/Projects/gobby/wiki/knowledge/concepts/gobby.md",
    title: "Gobby",
    content:
      '---\ntitle: "Gobby"\naliases:\n  - "Gobby"\n  - "gobby-cli"\nsource_kind: "concept"\ntags:\n  - gwiki\n  - compiled\n---\n\n# Gobby\n\nGobby is a local-first daemon.\n',
    content_format: "markdown",
    content_hash: "6d98b22ffd529e5a4d32c3c3803206d2f5f034f62924063c4612ad4a1e699121",
    byte_len: 5903,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

export const backlinksEnvelope: EnvelopeFixture = {
  ok: true,
  command: "backlinks",
  stderr: "",
  payload: {
    command: "backlinks",
    scope,
    page: "knowledge/concepts/gobby",
    backlinks: [
      {
        source_path: "knowledge/topics/contract-guardrails.md",
        target_path: "knowledge/concepts/gobby.md",
        raw_target: "knowledge/concepts/gobby",
      },
      {
        source_path: "recaps/2026-07-07.md",
        target_path: "knowledge/concepts/gobby.md",
        raw_target: "knowledge/concepts/gobby|Gobby",
      },
    ],
  },
};

/** Retrieval-only ask (live capture; `ai`/`synthesis` absent without --llm). */
export const askRetrievalEnvelope: EnvelopeFixture = {
  ok: true,
  command: "ask",
  stderr: "",
  payload: {
    command: "ask",
    scope,
    query: "how does the wiki watcher work",
    status: "retrieved",
    degraded: false,
    degraded_sources: [],
    hits: [
      {
        title: "Session: c1c0c073",
        fusion_key:
          "project:d45545c5:knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        result_type: "wiki",
        score: 0.03278688524590164,
        snippet: "watcher debounce and poll intervals",
        source_path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        wiki_page: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        sources: ["bm25", "semantic"],
        explanations: [
          { rank: 1, score: 0.01639344262295082, source: "bm25" },
          { rank: 1, score: 0.01639344262295082, source: "semantic" },
        ],
      },
    ],
    sources: ["bm25", "code/INDEX.md"],
    code_citations: [
      { file: "code/files/src/gobby/wiki/watcher.py.md", symbol: "src/gobby/wiki/watcher.py" },
    ],
    evidence: [
      {
        excerpt_chars: 2503,
        source_path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        wiki_page: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
      },
    ],
    prompt_token_budget: 12000,
    prompt_tokens_estimated: 4547,
    truncated: false,
    truncated_components: [],
    warnings: [],
    hint: null,
  },
};

/**
 * Synthesized ask: retrieval payload plus `ai` + `synthesis`, shaped from the
 * representative output pinned in crates/gwiki/tests/cli_contract.rs.
 */
export const askSynthesisEnvelope: EnvelopeFixture = {
  ok: true,
  command: "ask",
  stderr: "",
  payload: {
    ...askRetrievalEnvelope.payload,
    status: "answered",
    warnings: ["semantic search degraded"],
    ai: {
      requested: true,
      requested_mode: "auto",
      route: "local",
      status: "ok",
      model: "test-model",
      error: null,
    },
    synthesis: {
      answer:
        "The watcher polls [[knowledge/concepts/gobby|Gobby]] roots and debounces writes; see [[code/files/src/gobby/wiki/watcher.py]] for the loop.",
      model: "test-model",
      citation_check: {
        status: "unsupported_claims",
        checked_claims: 2,
        unsupported_claims: ["The watcher restarts the daemon on every write."],
      },
    },
  },
};

export const statusEnvelope: EnvelopeFixture = {
  ok: true,
  command: "status",
  stderr: "",
  payload: {
    command: "status",
    daemon_url: "http://localhost:60887",
    runtime: "postgres",
    scope,
    services: {
      embeddings: {
        api_base: "http://localhost:1234/v1",
        configured: true,
        model: "nomic-embed-text",
      },
      falkordb: { configured: true, host: "127.0.0.1", port: 16379 },
      postgres: { configured: true },
      qdrant: { configured: true, url: "http://localhost:6333" },
    },
    status: "datastore-ready",
  },
};

export const degradedStatusEnvelope: EnvelopeFixture = {
  ok: true,
  command: "status",
  stderr: "",
  payload: {
    ...statusEnvelope.payload,
    services: {
      embeddings: { configured: false },
      falkordb: { configured: false, host: "127.0.0.1", port: 16379 },
      postgres: { configured: true },
      qdrant: { configured: true, url: "http://localhost:6333" },
    },
  },
};

export const healthEnvelope: EnvelopeFixture = {
  ok: true,
  command: "health",
  stderr: "",
  payload: {
    command: "health",
    scope,
    root: "/Users/josh/Projects/gobby/wiki",
    json_path: "meta/health/latest.json",
    text_path: "meta/health/latest.md",
    broken_links: [
      {
        kind: "wikilink",
        line: 119,
        path: "code/deprecations.md",
        target: "code/files/tests/agents/test_sync.py",
      },
    ],
    duplicate_concepts: [],
    duplicate_sources: [],
    stale_citations: [],
    stale_pages: ["knowledge/concepts/awk.md"],
    uncited_sources: [],
    uncompiled_sources: ["knowledge/sources/src-uncompiled.md"],
    page_confidence: {
      average_score: 69,
      low_confidence: [],
      low_confidence_count: 0,
      scored_pages: 99,
    },
  },
};

/** 412 body: FastAPI wraps GwikiCommandError.to_envelope() in {"detail": ...}. */
export const writeConflictBody = {
  detail: {
    ok: false,
    command: "page-write",
    status: "failed",
    payload: { code: "precondition_failed" },
    stderr: "",
    error: {
      type: "command",
      returncode: 2,
      message: "expected hash does not match current page content",
    },
  },
};

export const writeSuccessEnvelope: EnvelopeFixture = {
  ok: true,
  command: "page-write",
  stderr: "",
  payload: {
    command: "page-write",
    scope,
    path: "knowledge/topics/example.md",
    created: false,
    bytes: 42,
    content_hash: "abcd1234",
    changed_paths: ["knowledge/topics/example.md"],
  },
};

/** GET /api/wiki/sources — record shape from useWiki's sourceRecordsFromEnvelope. */
export const sourcesEnvelope: EnvelopeFixture = {
  ok: true,
  command: "sources",
  stderr: "",
  payload: {
    command: "sources",
    scope,
    sources: [
      {
        id: "src-0001",
        title: "Session: 019efb0c",
        wiki_path: "knowledge/sources/src-0001.md",
        raw_path: "raw/sessions/019efb0c.jsonl",
      },
      {
        id: "src-0002",
        title: "Design notes",
        wiki_path: "knowledge/sources/src-0002.md",
        url: "https://example.com/design-notes",
      },
    ],
  },
};
