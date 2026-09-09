use std::io::Write as _;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;

use crate::codewiki_facts::{
    ContentFact, FileFact, FileId, GraphBounds as FactsGraphBounds, GraphEdge, GraphEdgeKind,
    GraphOutcome, GraphScopeMode, GrepContextLineFact, GrepHit, GrepOutcome, GrepQuery,
    GrepSpanFact, ScopeSelector, ScopedGraph, SearchQuery, SymbolFact,
};

use super::*;

const SOURCE: &str =
    "use std::fmt;\n\nfn alpha() {\n    beta();\n    gamma();\n}\n\nfn beta() {}\nfn gamma() {}\n";

fn git(repo: &Path, args: &[&str]) -> anyhow::Result<String> {
    let output = Command::new("git")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .arg("-C")
        .arg(repo)
        .args(args)
        .output()?;
    anyhow::ensure!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

fn git_input(repo: &Path, args: &[&str], input: &[u8]) -> anyhow::Result<String> {
    let mut child = Command::new("git")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .arg("-C")
        .arg(repo)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child.stdin.take().expect("piped stdin").write_all(input)?;
    let output = child.wait_with_output()?;
    anyhow::ensure!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

fn commit(repo: &Path, message: &str) -> anyhow::Result<String> {
    git(
        repo,
        &[
            "-c",
            "user.name=Evidence Test",
            "-c",
            "user.email=evidence@example.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        ],
    )?;
    git(repo, &["rev-parse", "HEAD"])
}

fn initialize_repo(repo: &Path) -> anyhow::Result<()> {
    git(repo, &["init", "--quiet", "-b", "main"])?;
    Ok(())
}

fn request(binding: &SnapshotBinding, operation: EvidenceOperation) -> EvidenceRequest {
    EvidenceRequest {
        schema_version: EVIDENCE_SCHEMA_VERSION,
        binding: binding.clone(),
        operation,
        max_bytes: DEFAULT_MAX_BYTES,
        continuation: None,
    }
}

fn search_selector(lane: SearchLane, query: &str) -> SearchSelector {
    SearchSelector {
        lane,
        query: query.to_string(),
        paths: Vec::new(),
        language: None,
        kind: None,
        limit: DEFAULT_RESULT_LIMIT,
        hybrid_identity: None,
    }
}

fn graph_selector(query: GraphQuery, source: EntitySelector) -> GraphSelector {
    GraphSelector {
        query,
        source: Some(source),
        target: None,
        direction: None,
        depth: DEFAULT_GRAPH_DEPTH,
        relations: Vec::new(),
        limit: DEFAULT_RESULT_LIMIT,
    }
}

#[derive(Clone)]
struct FakeFacts {
    project_id: String,
    files: Vec<FileFact>,
    symbols: Vec<SymbolFact>,
    edges: Vec<GraphEdge>,
}

impl FakeFacts {
    fn from_snapshot(snapshot: &Snapshot) -> Self {
        let files = snapshot
            .eligible_entries()
            .map(|entry| FileFact {
                id: FileId::new(&entry.path),
                path: entry.path.clone(),
                language: entry.language.clone().unwrap_or_else(|| "text".to_string()),
                symbol_count: i64::from(entry.path == "src/lib.rs") * 3,
                content_hash: entry.content_hash.clone().expect("eligible content hash"),
            })
            .collect::<Vec<_>>();
        let hash = snapshot
            .entry("src/lib.rs")
            .expect("source entry")
            .content_hash
            .clone()
            .expect("source hash");
        let symbols = ["alpha", "beta", "gamma"]
            .into_iter()
            .map(|name| symbol(name, &hash))
            .collect::<Vec<_>>();
        let edges = vec![
            graph_edge("alpha", "beta", "beta", &hash, GraphEdgeKind::Call),
            graph_edge("alpha", "gamma", "gamma", &hash, GraphEdgeKind::Call),
            graph_edge("gamma", "beta", "beta", &hash, GraphEdgeKind::Call),
            GraphEdge {
                source: "src/lib.rs".to_string(),
                target: "std::fmt".to_string(),
                kind: GraphEdgeKind::Import,
                rel: "IMPORTS".to_string(),
                source_kind: "file".to_string(),
                target_kind: "module".to_string(),
                source_name: "src/lib.rs".to_string(),
                target_name: "std::fmt".to_string(),
                source_file: "src/lib.rs".to_string(),
                target_file: "std::fmt".to_string(),
                owner_path: "src/lib.rs".to_string(),
                owner_hash: hash,
                provenance: "EXTRACTED".to_string(),
            },
        ];
        Self {
            project_id: snapshot.binding().project_id.clone(),
            files,
            symbols,
            edges,
        }
    }
}

impl EvidenceFacts for FakeFacts {
    fn project_id(&self) -> &str {
        &self.project_id
    }

    fn files(&self) -> anyhow::Result<Vec<FileFact>> {
        Ok(self.files.clone())
    }

    fn search_symbols(&self, query: &SearchQuery) -> anyhow::Result<FactPage<SymbolFact>> {
        let needle = query.text.to_lowercase();
        let mut items = self
            .symbols
            .iter()
            .filter(|symbol| {
                (symbol.name.to_lowercase().contains(&needle)
                    || symbol.qualified_name.to_lowercase().contains(&needle))
                    && query.kind.as_ref().is_none_or(|kind| symbol.kind == *kind)
                    && query
                        .language
                        .as_ref()
                        .is_none_or(|language| symbol.language == *language)
            })
            .cloned()
            .collect::<Vec<_>>();
        items.sort_by(|left, right| left.id.cmp(&right.id));
        let truncated = items.len() > query.limit;
        items.truncate(query.limit);
        Ok(FactPage { items, truncated })
    }

    fn search_content(&self, query: &SearchQuery) -> anyhow::Result<FactPage<ContentFact>> {
        let needle = query.text.to_lowercase();
        let mut items = SOURCE
            .lines()
            .enumerate()
            .filter(|(_, line)| line.to_lowercase().contains(&needle))
            .map(|(index, _)| ContentFact {
                path: "src/lib.rs".to_string(),
                line_start: index + 1,
                line_end: index + 1,
            })
            .collect::<Vec<_>>();
        let truncated = items.len() > query.limit;
        items.truncate(query.limit);
        Ok(FactPage { items, truncated })
    }

    fn grep(&self, query: &GrepQuery) -> anyhow::Result<GrepOutcome> {
        let matcher = if query.fixed_strings {
            None
        } else {
            Some(
                regex::RegexBuilder::new(&query.pattern)
                    .case_insensitive(query.ignore_case)
                    .build()?,
            )
        };
        let needle = if query.ignore_case {
            query.pattern.to_lowercase()
        } else {
            query.pattern.clone()
        };
        let mut hits = SOURCE
            .lines()
            .enumerate()
            .filter_map(|(index, line)| {
                let comparison = if query.ignore_case {
                    line.to_lowercase()
                } else {
                    line.to_string()
                };
                let matched = matcher.as_ref().map_or_else(
                    || comparison.contains(&needle),
                    |regex| regex.is_match(line),
                );
                matched.then(|| GrepHit {
                    path: "src/lib.rs".to_string(),
                    line: index + 1,
                    text: line.to_string(),
                    spans: Vec::<GrepSpanFact>::new(),
                    before: Vec::<GrepContextLineFact>::new(),
                    after: Vec::<GrepContextLineFact>::new(),
                })
            })
            .collect::<Vec<_>>();
        let truncated = hits.len() > query.limit;
        hits.truncate(query.limit);
        Ok(GrepOutcome {
            scanned_chunks: 1,
            matched_lines: hits.len(),
            truncated,
            hits,
        })
    }

    fn symbols_for_file(&self, path: &str) -> anyhow::Result<Vec<SymbolFact>> {
        Ok(self
            .symbols
            .iter()
            .filter(|symbol| symbol.file_path == path)
            .cloned()
            .collect())
    }

    fn symbol_by_id(&self, id: &str) -> anyhow::Result<Option<SymbolFact>> {
        Ok(self.symbols.iter().find(|symbol| symbol.id == id).cloned())
    }

    fn graph_edges(
        &self,
        seed: &ScopeSelector,
        kind: GraphEdgeKind,
        bounds: FactsGraphBounds,
        _mode: GraphScopeMode,
    ) -> anyhow::Result<ScopedGraph> {
        let anchors = seed.symbol_ids();
        let mut incoming = self
            .edges
            .iter()
            .filter(|edge| {
                edge.kind == kind && (anchors.is_empty() || anchors.contains(&edge.target))
            })
            .cloned()
            .collect::<Vec<_>>();
        let mut outgoing = self
            .edges
            .iter()
            .filter(|edge| {
                edge.kind == kind && (anchors.is_empty() || anchors.contains(&edge.source))
            })
            .cloned()
            .collect::<Vec<_>>();
        let incoming_truncated =
            bounds.incoming_limit > 0 && incoming.len() > bounds.incoming_limit;
        let outgoing_truncated =
            bounds.outgoing_limit > 0 && outgoing.len() > bounds.outgoing_limit;
        incoming.truncate(bounds.incoming_limit);
        outgoing.truncate(bounds.outgoing_limit);
        let mut edges = outgoing;
        edges.extend(incoming);
        let outcome = if edges.is_empty() {
            GraphOutcome::Empty
        } else if incoming_truncated || outgoing_truncated {
            GraphOutcome::Truncated(edges)
        } else {
            GraphOutcome::Available(edges)
        };
        Ok(ScopedGraph {
            outcome,
            incoming_truncated,
            outgoing_truncated,
        })
    }
}

struct FailingHybrid {
    identity: HybridIdentity,
}

struct StaticHybrid {
    identity: HybridIdentity,
    symbol_ids: Vec<String>,
}

impl HybridSearch for FailingHybrid {
    fn effective_identity(&self) -> std::result::Result<HybridIdentity, String> {
        Ok(self.identity.clone())
    }

    fn search_symbol_ids(
        &self,
        _selector: &SearchSelector,
    ) -> std::result::Result<FactPage<String>, String> {
        Err("audited endpoint unavailable".to_string())
    }
}

impl HybridSearch for StaticHybrid {
    fn effective_identity(&self) -> std::result::Result<HybridIdentity, String> {
        Ok(self.identity.clone())
    }

    fn search_symbol_ids(
        &self,
        _selector: &SearchSelector,
    ) -> std::result::Result<FactPage<String>, String> {
        Ok(FactPage {
            items: self.symbol_ids.clone(),
            truncated: false,
        })
    }
}

fn symbol(name: &str, file_hash: &str) -> SymbolFact {
    let marker = format!("fn {name}");
    let byte_start = SOURCE.find(&marker).expect("symbol marker");
    let byte_end = if name == "alpha" {
        SOURCE[byte_start..].find("\n}\n").expect("alpha close") + byte_start + 2
    } else {
        SOURCE[byte_start..]
            .find('\n')
            .map(|end| byte_start + end)
            .unwrap_or(SOURCE.len())
    };
    let line_start = 1 + SOURCE[..byte_start]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count();
    let line_end = 1 + SOURCE[..byte_end.saturating_sub(1)]
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count();
    SymbolFact {
        id: name.to_string(),
        file: FileId::new("src/lib.rs"),
        file_path: "src/lib.rs".to_string(),
        name: name.to_string(),
        qualified_name: format!("crate::{name}"),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start,
        byte_end,
        line_start,
        line_end,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: file_hash.to_string(),
        content_hash: gobby_core::indexing::content_hash(&SOURCE.as_bytes()[byte_start..byte_end]),
        summary: None,
    }
}

fn graph_edge(
    source: &str,
    target: &str,
    target_name: &str,
    owner_hash: &str,
    kind: GraphEdgeKind,
) -> GraphEdge {
    GraphEdge {
        source: source.to_string(),
        target: target.to_string(),
        kind,
        rel: "CALLS".to_string(),
        source_kind: "symbol".to_string(),
        target_kind: "symbol".to_string(),
        source_name: source.to_string(),
        target_name: target_name.to_string(),
        source_file: "src/lib.rs".to_string(),
        target_file: "src/lib.rs".to_string(),
        owner_path: "src/lib.rs".to_string(),
        owner_hash: owner_hash.to_string(),
        provenance: "EXTRACTED".to_string(),
    }
}

fn source_repo() -> anyhow::Result<(tempfile::TempDir, Snapshot)> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::create_dir(repo.join("src"))?;
    std::fs::write(repo.join("src/lib.rs"), SOURCE)?;
    git(repo, &["add", "src/lib.rs"])?;
    let root = commit(repo, "root")?;

    std::fs::write(repo.join("binary.bin"), b"plain prefix\0binary")?;
    git(repo, &["add", "binary.bin"])?;
    let link_oid = git_input(repo, &["hash-object", "-w", "--stdin"], b"src/lib.rs")?;
    git(
        repo,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("120000,{link_oid},source-link"),
        ],
    )?;
    git(
        repo,
        &[
            "update-index",
            "--add",
            "--cacheinfo",
            &format!("160000,{root},vendor/dependency"),
        ],
    )?;
    let commit_oid = commit(repo, "tracked exclusions")?;
    let snapshot = Snapshot::prepare(repo, "project-1", &commit_oid)?;
    Ok((temporary, snapshot))
}

#[test]
#[serial_test::serial(evidence_git)]
fn test_pinned_evidence_contract() -> anyhow::Result<()> {
    let (temporary, snapshot) = source_repo()?;
    let repo = temporary.path();
    let facts = FakeFacts::from_snapshot(&snapshot);
    let library = EvidenceLibrary::new(snapshot.clone(), Arc::new(facts.clone()))?;

    let verified = Snapshot::verify(
        repo,
        snapshot.binding().clone(),
        snapshot.inventory().clone(),
    )?;
    assert_eq!(verified.binding(), snapshot.binding());

    let exclusions = snapshot.exclusions();
    assert!(
        exclusions
            .iter()
            .any(|entry| entry.exclusion == Some(ExclusionReason::Binary))
    );
    assert!(
        exclusions
            .iter()
            .any(|entry| entry.exclusion == Some(ExclusionReason::Symlink))
    );
    assert!(
        exclusions
            .iter()
            .any(|entry| entry.exclusion == Some(ExclusionReason::Gitlink))
    );

    std::fs::write(repo.join("src/lib.rs"), "fn dirty_live_parent() {}\n")?;
    let range_request = request(
        snapshot.binding(),
        EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: "src/lib.rs".to_string(),
                start_line: 3,
                end_line: 6,
            },
        },
    );
    let mut encoded_request = serde_json::to_value(&range_request)?;
    assert_eq!(encoded_request["operation"], "read");
    let decoded_request: EvidenceRequest = serde_json::from_value(encoded_request.clone())?;
    assert_eq!(decoded_request, range_request);
    encoded_request["unexpected"] = serde_json::json!(true);
    assert!(serde_json::from_value::<EvidenceRequest>(encoded_request).is_err());
    let first = library.query(range_request.clone())?;
    let repeated = library.query(range_request)?;
    assert_eq!(first, repeated);
    let EvidenceItem::Source(source) = &first.items[0] else {
        panic!("expected source evidence")
    };
    assert_eq!(
        source.excerpt,
        "fn alpha() {\n    beta();\n    gamma();\n}\n"
    );
    assert_eq!(source.line_start, 3);
    assert_eq!(source.line_end, 6);
    assert_eq!(source.byte_end - source.byte_start, source.excerpt.len());
    assert_eq!(
        source.excerpt_hash,
        gobby_core::indexing::content_hash(source.excerpt.as_bytes())
    );

    let symbol_response = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Read {
            read: ReadSelector::Symbol {
                path: "src/lib.rs".to_string(),
                qualified_name: "crate::alpha".to_string(),
            },
        },
    ))?;
    assert_eq!(symbol_response.items.len(), 1);

    for lane in [
        SearchLane::Symbol,
        SearchLane::LexicalSymbol,
        SearchLane::Literal,
        SearchLane::Regex,
        SearchLane::Content,
    ] {
        let query = if lane == SearchLane::Symbol {
            "alpha"
        } else {
            "beta"
        };
        let response = library.query(request(
            snapshot.binding(),
            EvidenceOperation::Search {
                search: search_selector(lane, query),
            },
        ))?;
        assert!(!response.items.is_empty(), "lane {lane:?}");
        assert!(response.complete, "lane {lane:?}");
    }

    let source_symbol = EntitySelector::SymbolId {
        id: "alpha".to_string(),
    };
    for graph_query in [GraphQuery::Callees, GraphQuery::ScopedView] {
        let response = library.query(request(
            snapshot.binding(),
            EvidenceOperation::Graph {
                graph: graph_selector(graph_query, source_symbol.clone()),
            },
        ))?;
        assert!(!response.items.is_empty(), "graph {graph_query:?}");
        let expected_direction = if graph_query == GraphQuery::ScopedView {
            GraphDirection::Both
        } else {
            GraphDirection::Outgoing
        };
        assert!(response.items.iter().all(|item| {
            matches!(item, EvidenceItem::Graph(edge) if edge.direction == expected_direction)
        }));
        if graph_query == GraphQuery::ScopedView {
            assert!(response.items.iter().any(|item| {
                matches!(
                    item,
                    EvidenceItem::Graph(edge)
                        if edge.from.id == "gamma" && edge.to.id == "beta"
                )
            }));
        }
    }
    for graph_query in [GraphQuery::Callers, GraphQuery::Usages] {
        let response = library.query(request(
            snapshot.binding(),
            EvidenceOperation::Graph {
                graph: graph_selector(
                    graph_query,
                    EntitySelector::SymbolId {
                        id: "beta".to_string(),
                    },
                ),
            },
        ))?;
        assert!(!response.items.is_empty(), "graph {graph_query:?}");
        assert!(response.items.iter().all(|item| {
            matches!(
                item,
                EvidenceItem::Graph(edge) if edge.direction == GraphDirection::Incoming
            )
        }));
    }
    let imports = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Graph {
            graph: graph_selector(
                GraphQuery::Imports,
                EntitySelector::Path {
                    path: "src/lib.rs".to_string(),
                },
            ),
        },
    ))?;
    assert!(!imports.items.is_empty());
    assert!(matches!(
        &imports.items[0],
        EvidenceItem::Graph(edge) if edge.direction == GraphDirection::Outgoing
    ));
    let excluded_graph = library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Graph {
                graph: graph_selector(
                    GraphQuery::Imports,
                    EntitySelector::Path {
                        path: "source-link".to_string(),
                    },
                ),
            },
        ))
        .expect_err("excluded graph roots cannot reach indexed facts");
    assert_eq!(excluded_graph.code(), "excluded_path");

    let mut path_selector = graph_selector(GraphQuery::DirectedPath, source_symbol);
    path_selector.target = Some(EntitySelector::SymbolId {
        id: "beta".to_string(),
    });
    let path = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Graph {
            graph: path_selector,
        },
    ))?;
    assert_eq!(path.items.len(), 1);

    let mut truncated_selector = graph_selector(
        GraphQuery::Callers,
        EntitySelector::SymbolId {
            id: "beta".to_string(),
        },
    );
    truncated_selector.limit = 1;
    let truncated = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Graph {
            graph: truncated_selector,
        },
    ))?;
    assert_eq!(truncated.completeness, Completeness::TruncatedTraversal);
    assert!(!truncated.complete);

    let mut contradictory_direction = graph_selector(
        GraphQuery::Callers,
        EntitySelector::SymbolId {
            id: "beta".to_string(),
        },
    );
    contradictory_direction.direction = Some(GraphDirection::Outgoing);
    assert_eq!(
        library
            .query(request(
                snapshot.binding(),
                EvidenceOperation::Graph {
                    graph: contradictory_direction
                },
            ))
            .expect_err("callers cannot be labeled outgoing")
            .code(),
        "invalid_selector"
    );

    let mut bounded_view = graph_selector(
        GraphQuery::ScopedView,
        EntitySelector::SymbolId {
            id: "alpha".to_string(),
        },
    );
    bounded_view.limit = 3;
    let bounded_view = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Graph {
            graph: bounded_view,
        },
    ))?;
    assert_eq!(bounded_view.items.len(), 3);
    assert_eq!(bounded_view.completeness, Completeness::Complete);

    let mut ambiguous_facts = FakeFacts::from_snapshot(&snapshot);
    ambiguous_facts.edges[0].provenance = "AMBIGUOUS".to_string();
    let ambiguous_library = EvidenceLibrary::new(snapshot.clone(), Arc::new(ambiguous_facts))?;
    let ambiguous = ambiguous_library.query(request(
        snapshot.binding(),
        EvidenceOperation::Graph {
            graph: graph_selector(
                GraphQuery::Callees,
                EntitySelector::SymbolId {
                    id: "alpha".to_string(),
                },
            ),
        },
    ))?;
    assert!(matches!(
        &ambiguous.items[0],
        EvidenceItem::Graph(edge) if edge.provenance == GraphProvenance::Unresolved
    ));

    let hybrid_identity = HybridIdentity {
        endpoint: "https://embedding.invalid/v1".to_string(),
        model: "fixed-model".to_string(),
        dimension: 768,
        index_id: "index-7".to_string(),
    };
    let hybrid_library = EvidenceLibrary::new(snapshot.clone(), Arc::new(facts))?.with_hybrid(
        Arc::new(FailingHybrid {
            identity: hybrid_identity.clone(),
        }),
    );
    let mut hybrid_selector = search_selector(SearchLane::Hybrid, "alpha");
    hybrid_selector.hybrid_identity = Some(hybrid_identity);
    let error = hybrid_library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Search {
                search: hybrid_selector,
            },
        ))
        .expect_err("semantic failure must not fall back");
    assert_eq!(error.code(), "semantic_failure");

    let mut stale_facts = FakeFacts::from_snapshot(&snapshot);
    stale_facts.symbols[0].byte_end = SOURCE.len() + 1;
    let stale_library = EvidenceLibrary::new(snapshot.clone(), Arc::new(stale_facts))?;
    let error = stale_library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Read {
                read: ReadSelector::Symbol {
                    path: "src/lib.rs".to_string(),
                    qualified_name: "crate::alpha".to_string(),
                },
            },
        ))
        .expect_err("stale symbol offsets must fail");
    assert_eq!(error.code(), "stale_range");

    let mut shifted_facts = FakeFacts::from_snapshot(&snapshot);
    shifted_facts.symbols[1].byte_start += 1;
    let shifted_library = EvidenceLibrary::new(snapshot.clone(), Arc::new(shifted_facts))?;
    let error = shifted_library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Read {
                read: ReadSelector::Symbol {
                    path: "src/lib.rs".to_string(),
                    qualified_name: "crate::beta".to_string(),
                },
            },
        ))
        .expect_err("same-line stale symbol offsets must fail");
    assert_eq!(error.code(), "stale_range");

    let error = library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Read {
                read: ReadSelector::Range {
                    path: "../outside".to_string(),
                    start_line: 1,
                    end_line: 1,
                },
            },
        ))
        .expect_err("unsafe path must fail");
    assert_eq!(error.code(), "unsafe_path");

    let mut narrow = request(
        snapshot.binding(),
        EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: "src/lib.rs".to_string(),
                start_line: 1,
                end_line: 9,
            },
        },
    );
    narrow.max_bytes = 1;
    assert_eq!(
        library
            .query(narrow)
            .expect_err("whole item is oversized")
            .code(),
        "narrowing_required"
    );

    assert_commit_metadata_contract()?;
    assert_continuation_binding(&library)?;
    assert_missing_blob_is_typed()?;
    Ok(())
}

fn assert_continuation_binding(library: &EvidenceLibrary) -> anyhow::Result<()> {
    let mut metadata = request(
        library.snapshot().binding(),
        EvidenceOperation::Read {
            read: ReadSelector::CommitMetadata,
        },
    );
    let full = library.query(metadata.clone())?;
    let largest = full
        .items
        .iter()
        .map(serde_json::to_vec)
        .collect::<std::result::Result<Vec<_>, _>>()?
        .into_iter()
        .map(|item| item.len())
        .max()
        .unwrap_or(1);
    metadata.max_bytes = largest + 1;
    let first = library.query(metadata.clone())?;
    let continuation = first.continuation.clone().expect("metadata must paginate");
    assert_eq!(first.completeness, Completeness::Paginated);
    metadata.continuation = Some(continuation.clone());
    let second = library.query(metadata)?;
    assert_ne!(first.items, second.items);

    let mut changed = request(
        library.snapshot().binding(),
        EvidenceOperation::Read {
            read: ReadSelector::Range {
                path: "src/lib.rs".to_string(),
                start_line: 1,
                end_line: 1,
            },
        },
    );
    changed.continuation = Some(continuation);
    assert_eq!(
        library.query(changed).expect_err("bound cursor").code(),
        "continuation_mismatch"
    );
    Ok(())
}

fn assert_commit_metadata_contract() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::write(repo.join("old.txt"), "root\n")?;
    git(repo, &["add", "old.txt"])?;
    let root = commit(repo, "root")?;
    let root_snapshot = Snapshot::prepare(repo, "metadata-project", &root)?;
    assert!(root_snapshot.binding().commit.parent_oids.is_empty());
    assert_eq!(
        root_snapshot.binding().commit.comparison_kind,
        ComparisonKind::EmptyTree
    );
    assert!(
        root_snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.status == ChangeStatus::Added
                    && change.new_path.as_deref() == Some("old.txt")
            })
    );
    git(
        repo,
        &[
            "-c",
            "user.name=Evidence Test",
            "-c",
            "user.email=evidence@example.invalid",
            "tag",
            "-a",
            "pinned-root",
            &root,
            "-m",
            "pinned root",
        ],
    )?;
    let tag_oid = git(repo, &["rev-parse", "pinned-root^{tag}"])?;
    let tagged_snapshot = Snapshot::prepare(repo, "metadata-project", &tag_oid)?;
    assert_eq!(tagged_snapshot.binding().commit_oid, root);

    git(repo, &["mv", "old.txt", "renamed.txt"])?;
    let renamed = commit(repo, "rename")?;
    let rename_snapshot = Snapshot::prepare(repo, "metadata-project", &renamed)?;
    assert!(
        rename_snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.status == ChangeStatus::Renamed
                    && change.old_path.as_deref() == Some("old.txt")
                    && change.new_path.as_deref() == Some("renamed.txt")
                    && change.old_blob_oid == change.new_blob_oid
            })
    );

    git(repo, &["rm", "--quiet", "renamed.txt"])?;
    let deleted = commit(repo, "delete")?;
    let delete_snapshot = Snapshot::prepare(repo, "metadata-project", &deleted)?;
    assert!(
        delete_snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.status == ChangeStatus::Deleted
                    && change.old_path.as_deref() == Some("renamed.txt")
            })
    );

    std::fs::write(repo.join("shared.txt"), "base\n")?;
    git(repo, &["add", "shared.txt"])?;
    let base = commit(repo, "merge base")?;
    git(repo, &["branch", "feature", &base])?;
    git(repo, &["checkout", "--quiet", "feature"])?;
    std::fs::write(repo.join("shared.txt"), "feature\n")?;
    git(repo, &["add", "shared.txt"])?;
    commit(repo, "feature change")?;
    git(repo, &["checkout", "--quiet", "main"])?;
    std::fs::write(repo.join("main.txt"), "main\n")?;
    git(repo, &["add", "main.txt"])?;
    let first_parent = commit(repo, "main change")?;
    git(
        repo,
        &[
            "-c",
            "user.name=Evidence Test",
            "-c",
            "user.email=evidence@example.invalid",
            "merge",
            "--quiet",
            "--no-ff",
            "feature",
            "-m",
            "merge",
        ],
    )?;
    let merge = git(repo, &["rev-parse", "HEAD"])?;
    let merge_snapshot = Snapshot::prepare(repo, "metadata-project", &merge)?;
    assert_eq!(merge_snapshot.binding().commit.parent_oids.len(), 2);
    assert_eq!(
        merge_snapshot.binding().commit.comparison_parent_oid,
        first_parent
    );
    assert!(
        merge_snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.status == ChangeStatus::Modified
                    && change.new_path.as_deref() == Some("shared.txt")
            })
    );
    Ok(())
}

fn assert_missing_blob_is_typed() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::write(repo.join("only.txt"), "only\n")?;
    git(repo, &["add", "only.txt"])?;
    let commit_oid = commit(repo, "only")?;
    let snapshot = Snapshot::prepare(repo, "missing-project", &commit_oid)?;
    let blob = snapshot
        .entry("only.txt")?
        .blob_oid
        .as_ref()
        .expect("blob oid")
        .clone();
    let object = repo.join(".git/objects").join(&blob[..2]).join(&blob[2..]);
    std::fs::remove_file(object)?;
    let error = snapshot
        .read_blob("only.txt")
        .expect_err("missing blob must fail");
    assert_eq!(error.code(), "missing_git_object");
    Ok(())
}

#[test]
#[serial_test::serial(evidence_git)]
fn snapshot_verification_rejects_changed_inventory() -> anyhow::Result<()> {
    let (temporary, snapshot) = source_repo()?;
    let mut inventory = snapshot.inventory().clone();
    inventory.entries[0].mode = "100755".to_string();
    let error = Snapshot::verify(temporary.path(), snapshot.binding().clone(), inventory)
        .expect_err("changed manifest must fail");
    assert_eq!(error.code(), "inventory_mismatch");
    Ok(())
}

#[test]
#[serial_test::serial(evidence_git)]
fn incomplete_index_is_not_reported_as_empty_repository() -> anyhow::Result<()> {
    let (_temporary, snapshot) = source_repo()?;
    let facts = FakeFacts {
        project_id: snapshot.binding().project_id.clone(),
        files: Vec::new(),
        symbols: Vec::new(),
        edges: Vec::new(),
    };
    let library = EvidenceLibrary::new(snapshot.clone(), Arc::new(facts))?;
    let error = library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Search {
                search: search_selector(SearchLane::Literal, "missing"),
            },
        ))
        .expect_err("incomplete index must fail");
    assert_eq!(error.code(), "index_incomplete");
    Ok(())
}

#[test]
fn snapshot_ignores_commit_and_blob_replacement_refs() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;

    std::fs::write(repo.join("source.txt"), "original source\n")?;
    git(repo, &["add", "source.txt"])?;
    let original_commit = commit(repo, "original")?;
    let original_tree = git(repo, &["rev-parse", &format!("{original_commit}^{{tree}}")])?;
    let original_blob = git(
        repo,
        &["rev-parse", &format!("{original_commit}:source.txt")],
    )?;

    std::fs::write(repo.join("source.txt"), "replacement source\n")?;
    git(repo, &["add", "source.txt"])?;
    let replacement_commit = commit(repo, "replacement")?;
    let replacement_blob = git(
        repo,
        &["rev-parse", &format!("{replacement_commit}:source.txt")],
    )?;
    git(repo, &["replace", &original_commit, &replacement_commit])?;
    git(repo, &["replace", &original_blob, &replacement_blob])?;

    let snapshot = Snapshot::prepare(repo, "project-replacements", &original_commit)?;
    assert_eq!(snapshot.binding().commit_oid, original_commit);
    assert_eq!(snapshot.binding().tree_oid, original_tree);
    assert!(snapshot.binding().commit.parent_oids.is_empty());
    assert_eq!(
        snapshot.binding().commit.comparison_kind,
        ComparisonKind::EmptyTree
    );
    assert_eq!(snapshot.binding().commit.changed_paths.len(), 1);
    assert_eq!(
        snapshot.binding().commit.changed_paths[0]
            .new_blob_oid
            .as_deref(),
        Some(original_blob.as_str())
    );
    assert_eq!(
        snapshot.entry("source.txt")?.blob_oid.as_deref(),
        Some(original_blob.as_str())
    );
    assert_eq!(snapshot.read_blob("source.txt")?, b"original source\n");
    Ok(())
}

#[test]
#[serial_test::serial(evidence_git)]
fn snapshot_ignores_ambient_git_repository_and_config_overrides() -> anyhow::Result<()> {
    let source = tempfile::tempdir()?;
    initialize_repo(source.path())?;
    std::fs::write(source.path().join("source.txt"), "trusted source\n")?;
    git(source.path(), &["add", "source.txt"])?;
    let commit_oid = commit(source.path(), "trusted")?;

    let contaminant = tempfile::tempdir()?;
    initialize_repo(contaminant.path())?;
    std::fs::write(contaminant.path().join("source.txt"), "ambient source\n")?;
    git(contaminant.path(), &["add", "source.txt"])?;
    commit(contaminant.path(), "ambient")?;
    let git_dir = contaminant.path().join(".git");
    let object_dir = git_dir.join("objects");
    let index_file = git_dir.join("index");
    let marker = contaminant.path().join("external-diff-ran");

    let git_dir = git_dir.to_string_lossy().into_owned();
    let work_tree = contaminant.path().to_string_lossy().into_owned();
    let object_dir = object_dir.to_string_lossy().into_owned();
    let index_file = index_file.to_string_lossy().into_owned();
    let marker_command = format!("touch {}", marker.display());
    let snapshot = temp_env::with_vars(
        [
            ("GIT_DIR", Some(git_dir.as_str())),
            ("GIT_COMMON_DIR", Some(git_dir.as_str())),
            ("GIT_WORK_TREE", Some(work_tree.as_str())),
            ("GIT_INDEX_FILE", Some(index_file.as_str())),
            ("GIT_OBJECT_DIRECTORY", Some(object_dir.as_str())),
            (
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                Some(object_dir.as_str()),
            ),
            ("GIT_CONFIG_COUNT", Some("2")),
            ("GIT_CONFIG_KEY_0", Some("diff.external")),
            ("GIT_CONFIG_VALUE_0", Some(marker_command.as_str())),
            ("GIT_CONFIG_KEY_1", Some("core.useReplaceRefs")),
            ("GIT_CONFIG_VALUE_1", Some("true")),
            ("GIT_EXTERNAL_DIFF", Some(marker_command.as_str())),
            ("GIT_NO_LAZY_FETCH", Some("0")),
            ("GIT_NO_REPLACE_OBJECTS", Some("0")),
        ],
        || Snapshot::prepare(source.path(), "project-ambient", &commit_oid),
    )?;

    assert_eq!(snapshot.read_blob("source.txt")?, b"trusted source\n");
    assert!(!marker.exists(), "repository-configured diff command ran");
    Ok(())
}

#[cfg(unix)]
#[test]
fn snapshot_preserves_unsafe_raw_changed_paths_as_exclusions() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::create_dir(repo.join("src"))?;
    std::fs::write(repo.join("src/lib.rs"), "pub fn visible() {}\n")?;
    git(repo, &["add", "src/lib.rs"])?;
    let unsafe_blob = git_input(repo, &["hash-object", "-w", "--stdin"], b"excluded\n")?;
    let mut index_record = format!("100644 {unsafe_blob}\t").into_bytes();
    index_record.extend_from_slice(b"unsafe-\xff\\name");
    index_record.push(0);
    let collision_blob = git_input(repo, &["hash-object", "-w", "--stdin"], b"collision\n")?;
    index_record.extend_from_slice(format!("100644 {collision_blob}\t").as_bytes());
    index_record.extend_from_slice(b"unsafe-\\xff\\x5cname");
    index_record.push(0);
    git_input(repo, &["update-index", "-z", "--index-info"], &index_record)?;
    let commit_oid = commit(repo, "unsafe tracked path")?;

    let snapshot = Snapshot::prepare(repo, "project-unsafe", &commit_oid)?;
    let escaped_path = "unsafe-\\xff\\x5cname";
    let excluded = snapshot
        .inventory()
        .entries
        .iter()
        .find(|entry| entry.path == escaped_path)
        .expect("escaped unsafe path remains in inventory");
    assert_eq!(excluded.exclusion, Some(ExclusionReason::UnsafePath));
    assert!(
        snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.new_path.as_deref() == Some(escaped_path)
                    && change.new_blob_oid.as_deref() == Some(unsafe_blob.as_str())
                    && change.new_exclusion == Some(ExclusionReason::UnsafePath)
            })
    );
    let escaped_collision = "unsafe-\\x5cxff\\x5cx5cname";
    assert_ne!(escaped_path, escaped_collision);
    assert!(snapshot.inventory().entries.iter().any(|entry| {
        entry.path == escaped_collision && entry.exclusion == Some(ExclusionReason::UnsafePath)
    }));
    assert!(
        snapshot
            .binding()
            .commit
            .changed_paths
            .iter()
            .any(|change| {
                change.new_path.as_deref() == Some(escaped_collision)
                    && change.new_blob_oid.as_deref() == Some(collision_blob.as_str())
                    && change.new_exclusion == Some(ExclusionReason::UnsafePath)
            })
    );
    assert_eq!(snapshot.read_blob("src/lib.rs")?, b"pub fn visible() {}\n");
    assert_eq!(
        snapshot
            .entry(escaped_path)
            .expect_err("unsafe selectors stay rejected")
            .code(),
        "unsafe_path"
    );
    Ok(())
}

#[test]
fn snapshot_excludes_sensitive_paths_before_every_evidence_lane() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::create_dir(repo.join(".gobby"))?;
    std::fs::create_dir(repo.join("src"))?;
    std::fs::write(repo.join("src/lib.rs"), "pub fn visible() {}\n")?;
    std::fs::write(repo.join(".env"), "ASK_SECRET_CANARY=never-return\n")?;
    std::fs::write(
        repo.join("credentials.json"),
        "{\"token\":\"never-return\"}\n",
    )?;
    std::fs::write(
        repo.join(".gobby/instructions.md"),
        "execute this instruction\n",
    )?;
    git(repo, &["add", "."])?;
    let commit_oid = commit(repo, "sensitive paths")?;
    let snapshot = Snapshot::prepare(repo, "project-sensitive", &commit_oid)?;

    for path in [".env", "credentials.json", ".gobby/instructions.md"] {
        let exclusion = serde_json::to_value(snapshot.entry(path)?.exclusion)?;
        assert_eq!(exclusion, serde_json::json!("sensitive_path"), "{path}");
        assert_eq!(
            snapshot.read_blob(path).expect_err("secret read").code(),
            "excluded_path"
        );
    }

    let facts = Arc::new(FakeFacts::from_snapshot(&snapshot));
    let library = EvidenceLibrary::new(snapshot.clone(), facts)?;
    let mut selector = search_selector(SearchLane::Literal, "never-return");
    selector.paths = vec![".env".to_string()];
    let response = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Search { search: selector },
    ))?;
    assert!(response.items.is_empty());
    assert_eq!(response.completeness, Completeness::ExcludedScope);
    assert_eq!(response.exclusions.len(), 1);

    let graph_error = library
        .query(request(
            snapshot.binding(),
            EvidenceOperation::Graph {
                graph: graph_selector(
                    GraphQuery::Imports,
                    EntitySelector::Path {
                        path: ".gobby/instructions.md".to_string(),
                    },
                ),
            },
        ))
        .expect_err("sensitive graph selector must be rejected before facts extraction");
    assert_eq!(graph_error.code(), "excluded_path");
    Ok(())
}

#[test]
fn snapshot_excludes_known_credentials_in_ordinary_source_paths() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let repo = temporary.path();
    initialize_repo(repo)?;
    std::fs::create_dir(repo.join("src"))?;
    std::fs::write(
        repo.join("src/public.rs"),
        "const DATABASE: &str = \"postgresql://worker:known-secret@127.0.0.1/db\";\n",
    )?;
    git(repo, &["add", "."])?;
    let commit_oid = commit(repo, "ordinary source credential")?;
    let snapshot = Snapshot::prepare(repo, "project-sensitive-content", &commit_oid)?;

    let entry = snapshot.entry("src/public.rs")?;
    assert_eq!(
        serde_json::to_value(entry.exclusion)?,
        serde_json::json!("sensitive_content")
    );
    assert_eq!(
        snapshot
            .read_blob("src/public.rs")
            .expect_err("credential-bearing source must not be citeable")
            .code(),
        "excluded_path"
    );
    Ok(())
}

#[test]
fn hybrid_search_reports_union_truncation() -> anyhow::Result<()> {
    let (_temporary, snapshot) = source_repo()?;
    let mut facts = FakeFacts::from_snapshot(&snapshot);
    let seed = facts.symbols[0].clone();
    facts.symbols = [
        ("lexical-one", "needle_one"),
        ("lexical-two", "needle_two"),
        ("semantic-one", "other_one"),
        ("semantic-two", "other_two"),
    ]
    .into_iter()
    .map(|(id, name)| SymbolFact {
        id: id.to_string(),
        name: name.to_string(),
        qualified_name: format!("crate::{name}"),
        ..seed.clone()
    })
    .collect();
    let identity = HybridIdentity {
        endpoint: "https://embedding.invalid/v1".to_string(),
        model: "fixed-model".to_string(),
        dimension: 768,
        index_id: "index-union".to_string(),
    };
    let library = EvidenceLibrary::new(snapshot.clone(), Arc::new(facts))?.with_hybrid(Arc::new(
        StaticHybrid {
            identity: identity.clone(),
            symbol_ids: vec!["semantic-one".to_string(), "semantic-two".to_string()],
        },
    ));
    let mut selector = search_selector(SearchLane::Hybrid, "needle");
    selector.limit = 3;
    selector.hybrid_identity = Some(identity);

    let response = library.query(request(
        snapshot.binding(),
        EvidenceOperation::Search { search: selector },
    ))?;
    assert_eq!(response.items.len(), 3);
    assert_eq!(response.completeness, Completeness::TruncatedIndex);
    assert!(!response.complete);
    assert!(response.continuation.is_none());
    Ok(())
}

#[test]
fn non_directed_graph_queries_reject_target_selectors() -> anyhow::Result<()> {
    let (_temporary, snapshot) = source_repo()?;
    let facts = FakeFacts::from_snapshot(&snapshot);
    let library = EvidenceLibrary::new(snapshot.clone(), Arc::new(facts))?;

    for query in [
        GraphQuery::Callers,
        GraphQuery::Callees,
        GraphQuery::Usages,
        GraphQuery::Imports,
        GraphQuery::ScopedView,
    ] {
        let source = if query == GraphQuery::Imports {
            EntitySelector::Path {
                path: "src/lib.rs".to_string(),
            }
        } else {
            EntitySelector::SymbolId {
                id: "alpha".to_string(),
            }
        };
        let mut selector = graph_selector(query, source);
        selector.target = Some(EntitySelector::SymbolId {
            id: "beta".to_string(),
        });
        let error = library
            .query(request(
                snapshot.binding(),
                EvidenceOperation::Graph { graph: selector },
            ))
            .expect_err("target is valid only for directed_path");
        assert_eq!(error.code(), "invalid_selector", "query {query:?}");
    }
    Ok(())
}
