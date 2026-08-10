/**
 * Wiki envelope fixtures shaped from live captures:
 * - `gwiki pages/read/backlinks/status/health --format json` against the
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

/** Read fixture with wikilinks and a Citations section (§3.1 browse tests). */
export const browseReadGobbyEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "knowledge/concepts/gobby.md" },
    wiki_path: "knowledge/concepts/gobby.md",
    title: "Gobby",
    content:
      '---\ntitle: "Gobby"\nsource_kind: "concept"\ntags:\n  - gwiki\n  - compiled\n---\n\n# Gobby\n\nGobby is a local-first daemon. See [[knowledge/concepts/gwiki|Gwiki]] and [[missing/page|Missing]].\n\n## Citations\n\n- [[knowledge/sources/src-82182128d032cefe-session-c1c0c073|Session: c1c0c073]]\n',
    content_format: "markdown",
    content_hash: "6d98b22ffd529e5a4d32c3c3803206d2",
    byte_len: 260,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

export const browseReadGwikiEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "knowledge/concepts/gwiki.md" },
    wiki_path: "knowledge/concepts/gwiki.md",
    title: "Gwiki",
    content:
      '---\ntitle: "Gwiki"\nsource_kind: "concept"\ntags:\n  - gwiki\n---\n\n# Gwiki\n\nGwiki compiles the wiki.\n',
    content_format: "markdown",
    content_hash: "0db1596ac2f34b7cd2e04a491b692406",
    byte_len: 96,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

/** Ambiguous title read (§3.1 match picker). */
export const browseAmbiguousReadEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "ambiguous",
    requested: { kind: "title", value: "gobby" },
    wiki_path: null,
    title: null,
    content: "",
    content_format: "markdown",
    content_hash: null,
    byte_len: 0,
    truncated: false,
    candidates: [
      { path: "knowledge/concepts/gobby.md", title: "Gobby" },
      { path: "code/files/src/gobby/runner.py.md", title: "src/gobby/runner.py" },
    ],
    degradations: [],
  },
};

/**
 * Graph subset for browse tests: one links edge whose unresolved target's
 * raw_target equals the Gobby page path — the "unresolved mentions" input.
 */
export const browseGraphEnvelope: EnvelopeFixture = {
  ok: true,
  command: "graph",
  stderr: "",
  payload: {
    command: "graph",
    degraded: false,
    degraded_sources: [],
    nodes: [
      {
        id: "document-knowledge-concepts-gwiki-md-11aa22bb",
        kind: "wiki_page",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/concepts/gwiki.md",
        title: "Gwiki",
      },
      {
        id: "unresolved-knowledge-concepts-gobby-9cc00dd1",
        kind: "unresolved_target",
        scope_kind: "project",
        scope_id: scope.id,
        path: null,
        title: "knowledge/concepts/gobby",
      },
    ],
    edges: {
      links: [
        {
          source: "document-knowledge-concepts-gwiki-md-11aa22bb",
          target: "unresolved-knowledge-concepts-gobby-9cc00dd1",
          kind: "links",
          raw_target: "knowledge/concepts/gobby",
        },
      ],
      imports: [],
      calls: [],
      callers: [],
      trust: [],
      audit: [],
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

/** Watcher-reindexed Gobby variant — new hash + body (§3.2 conflict tests). */
export const browseReadGobbyChangedEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "knowledge/concepts/gobby.md" },
    wiki_path: "knowledge/concepts/gobby.md",
    title: "Gobby",
    content:
      '---\ntitle: "Gobby"\nsource_kind: "concept"\ntags:\n  - gwiki\n---\n\n# Gobby\n\nGobby was reindexed by the watcher while an editor was open.\n',
    content_format: "markdown",
    content_hash: "e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5",
    byte_len: 132,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

/** 409 body: mode="create" against an existing page (§3.2 inline conflict). */
export const alreadyExistsBody = {
  detail: {
    ok: false,
    command: "page-write",
    status: "failed",
    payload: { code: "already_exists" },
    stderr: "",
    error: {
      type: "command",
      returncode: 2,
      message: "page already exists at knowledge/concepts/gobby.md",
    },
  },
};

/** not_found read — drives the §3.2 create-this-page affordance. */
export const notFoundReadEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "not_found",
    requested: { kind: "path", value: "knowledge/concepts/gwiki.md" },
    candidates: [],
    degradations: [],
  },
};

/** Generated code page read — §3.2 read-only affordance tests. */
export const browseReadCodeEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "code/_architecture.md" },
    wiki_path: "code/_architecture.md",
    title: "Architecture Overview",
    content: "# Architecture Overview\n\nGenerated codewiki page.\n",
    content_format: "markdown",
    content_hash: "cc33",
    byte_len: 50,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

/**
 * Full-kind graph for the §4.1 graph view: wiki pages, code pages, a
 * source/citation pair, an unresolved target, every edge layer (including a
 * `callers` edge the scene must always drop), centrality degrees, and two
 * communities.
 */
export const graphViewEnvelope: EnvelopeFixture = {
  ok: true,
  command: "graph",
  stderr: "",
  payload: {
    command: "graph",
    degraded: false,
    degraded_sources: [],
    nodes: [
      {
        id: "document-knowledge-concepts-gobby-md-aa01",
        kind: "wiki_page",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/concepts/gobby.md",
        title: "Gobby",
      },
      {
        id: "document-knowledge-concepts-gwiki-md-aa02",
        kind: "wiki_page",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/concepts/gwiki.md",
        title: "Gwiki",
      },
      {
        id: "document-code-files-src-gobby-runner-py-md-aa03",
        kind: "code",
        scope_kind: "project",
        scope_id: scope.id,
        path: "code/files/src/gobby/runner.py.md",
        title: "src/gobby/runner.py",
      },
      {
        id: "document-code-files-src-gobby-watcher-py-md-aa04",
        kind: "code",
        scope_kind: "project",
        scope_id: scope.id,
        path: "code/files/src/gobby/watcher.py.md",
        title: "src/gobby/watcher.py",
      },
      {
        id: "source-src-8218-aa05",
        kind: "source",
        scope_kind: "project",
        scope_id: scope.id,
        path: "knowledge/sources/src-82182128d032cefe-session-c1c0c073.md",
        title: "Session: c1c0c073",
      },
      {
        id: "citation-runner-aa06",
        kind: "citation",
        scope_kind: "project",
        scope_id: scope.id,
        path: null,
        title: null,
      },
      {
        id: "unresolved-code-modules-src-gobby-aa07",
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
          source: "document-knowledge-concepts-gobby-md-aa01",
          target: "document-knowledge-concepts-gwiki-md-aa02",
          kind: "links",
          raw_target: "knowledge/concepts/gwiki",
        },
        {
          source: "document-knowledge-concepts-gwiki-md-aa02",
          target: "unresolved-code-modules-src-gobby-aa07",
          kind: "links",
          raw_target: "code/modules/src/gobby",
        },
      ],
      imports: [
        {
          source: "document-code-files-src-gobby-runner-py-md-aa03",
          target: "document-code-files-src-gobby-watcher-py-md-aa04",
          kind: "imports",
        },
      ],
      calls: [
        {
          source: "document-code-files-src-gobby-runner-py-md-aa03",
          target: "document-code-files-src-gobby-watcher-py-md-aa04",
          kind: "calls",
        },
      ],
      callers: [
        {
          source: "document-code-files-src-gobby-watcher-py-md-aa04",
          target: "document-code-files-src-gobby-runner-py-md-aa03",
          kind: "callers",
        },
      ],
      trust: [
        {
          source: "document-knowledge-concepts-gobby-md-aa01",
          target: "source-src-8218-aa05",
          kind: "trust",
        },
      ],
      audit: [
        {
          source: "citation-runner-aa06",
          target: "document-code-files-src-gobby-runner-py-md-aa03",
          kind: "audit",
        },
      ],
    },
    analytics: {
      bridges: [],
      centrality: [
        {
          node: { id: "document-knowledge-concepts-gobby-md-aa01", kind: "wiki_page" },
          degree: 5,
          score: 0.5,
        },
        {
          node: { id: "document-knowledge-concepts-gwiki-md-aa02", kind: "wiki_page" },
          degree: 2,
          score: 0.2,
        },
      ],
      communities: [
        {
          id: "community-1",
          nodes: [
            { id: "document-knowledge-concepts-gobby-md-aa01", kind: "wiki_page" },
            { id: "document-knowledge-concepts-gwiki-md-aa02", kind: "wiki_page" },
          ],
          weight: 2.0,
        },
        {
          id: "community-2",
          nodes: [
            { id: "document-code-files-src-gobby-runner-py-md-aa03", kind: "code" },
            { id: "document-code-files-src-gobby-watcher-py-md-aa04", kind: "code" },
          ],
          weight: 1.0,
        },
      ],
      god_nodes: [],
      hotspots: [],
      unexpected_links: [],
    },
  },
};

/** Code page with mermaid + highlighted fences — §4.2 reader affordances. */
export const browseReadRunnerEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    scope,
    status: "found",
    requested: { kind: "path", value: "code/files/src/gobby/runner.py.md" },
    wiki_path: "code/files/src/gobby/runner.py.md",
    title: "src/gobby/runner.py",
    content:
      "# src/gobby/runner.py\n\nDaemon entry point.\n\n```mermaid\ngraph TD; A-->B;\n```\n\n```python\nclass GobbyRunner:\n    pass\n```\n",
    content_format: "markdown",
    content_hash: "dd44",
    byte_len: 130,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};
