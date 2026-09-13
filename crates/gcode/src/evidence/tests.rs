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

fn request(binding: &RepositoryBinding, operation: EvidenceOperation) -> EvidenceRequest {
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
    fn for_source(binding: &RepositoryBinding) -> Self {
        let hash = gobby_core::indexing::content_hash(SOURCE.as_bytes());
        let files = vec![FileFact {
            id: FileId::new("src/lib.rs"),
            path: "src/lib.rs".to_string(),
            language: "rust".to_string(),
            symbol_count: 3,
            content_hash: hash.clone(),
        }];
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
            project_id: binding.project_id.clone(),
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

fn source_repo() -> anyhow::Result<(tempfile::TempDir, RepositoryBinding)> {
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
    let binding = RepositoryBinding {
        project_id: "project-1".to_string(),
        tree_oid: git(repo, &["rev-parse", "HEAD^{tree}"])?,
        commit_oid,
    };
    Ok((temporary, binding))
}

#[test]
#[serial_test::serial(evidence_git)]
fn test_live_evidence_contract() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let facts = FakeFacts::for_source(&binding);
    let library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts.clone()))?;

    let range_request = request(
        &binding,
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
        &binding,
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
            &binding,
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
            &binding,
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
            &binding,
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
        &binding,
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
            &binding,
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
    assert_eq!(excluded_graph.code(), "invalid_selector");

    let mut path_selector = graph_selector(GraphQuery::DirectedPath, source_symbol);
    path_selector.target = Some(EntitySelector::SymbolId {
        id: "beta".to_string(),
    });
    let path = library.query(request(
        &binding,
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
        &binding,
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
                &binding,
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
        &binding,
        EvidenceOperation::Graph {
            graph: bounded_view,
        },
    ))?;
    assert_eq!(bounded_view.items.len(), 3);
    assert_eq!(bounded_view.completeness, Completeness::Complete);

    let mut ambiguous_facts = FakeFacts::for_source(&binding);
    ambiguous_facts.edges[0].provenance = "AMBIGUOUS".to_string();
    let ambiguous_library =
        EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(ambiguous_facts))?;
    let ambiguous = ambiguous_library.query(request(
        &binding,
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
    let hybrid_library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts))?
        .with_hybrid(Arc::new(FailingHybrid {
            identity: hybrid_identity.clone(),
        }));
    let mut hybrid_selector = search_selector(SearchLane::Hybrid, "alpha");
    hybrid_selector.hybrid_identity = Some(hybrid_identity);
    let error = hybrid_library
        .query(request(
            &binding,
            EvidenceOperation::Search {
                search: hybrid_selector,
            },
        ))
        .expect_err("semantic failure must not fall back");
    assert_eq!(error.code(), "semantic_failure");

    let mut stale_facts = FakeFacts::for_source(&binding);
    stale_facts.symbols[0].byte_end = SOURCE.len() + 1;
    let stale_library =
        EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(stale_facts))?;
    let error = stale_library
        .query(request(
            &binding,
            EvidenceOperation::Read {
                read: ReadSelector::Symbol {
                    path: "src/lib.rs".to_string(),
                    qualified_name: "crate::alpha".to_string(),
                },
            },
        ))
        .expect_err("stale symbol offsets must fail");
    assert_eq!(error.code(), "stale_range");

    let mut shifted_facts = FakeFacts::for_source(&binding);
    shifted_facts.symbols[1].byte_start += 1;
    let shifted_library =
        EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(shifted_facts))?;
    let error = shifted_library
        .query(request(
            &binding,
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
            &binding,
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
        &binding,
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
    Ok(())
}

fn assert_continuation_binding(library: &EvidenceLibrary) -> anyhow::Result<()> {
    let mut metadata = request(
        &library.binding,
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
        &library.binding,
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
    let root_snapshot = provenance::load_commit_binding(repo, &root)?;
    assert!(root_snapshot.parent_oids.is_empty());
    assert_eq!(root_snapshot.comparison_kind, ComparisonKind::EmptyTree);
    assert!(root_snapshot.changed_paths.iter().any(|change| {
        change.status == ChangeStatus::Added && change.new_path.as_deref() == Some("old.txt")
    }));
    git(repo, &["mv", "old.txt", "renamed.txt"])?;
    let renamed = commit(repo, "rename")?;
    let rename_snapshot = provenance::load_commit_binding(repo, &renamed)?;
    assert!(rename_snapshot.changed_paths.iter().any(|change| {
        change.status == ChangeStatus::Renamed
            && change.old_path.as_deref() == Some("old.txt")
            && change.new_path.as_deref() == Some("renamed.txt")
            && change.old_blob_oid == change.new_blob_oid
    }));

    git(repo, &["rm", "--quiet", "renamed.txt"])?;
    let deleted = commit(repo, "delete")?;
    let delete_snapshot = provenance::load_commit_binding(repo, &deleted)?;
    assert!(delete_snapshot.changed_paths.iter().any(|change| {
        change.status == ChangeStatus::Deleted && change.old_path.as_deref() == Some("renamed.txt")
    }));

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
    let merge_snapshot = provenance::load_commit_binding(repo, &merge)?;
    assert_eq!(merge_snapshot.parent_oids.len(), 2);
    assert_eq!(merge_snapshot.comparison_parent_oid, first_parent);
    assert!(merge_snapshot.changed_paths.iter().any(|change| {
        change.status == ChangeStatus::Modified && change.new_path.as_deref() == Some("shared.txt")
    }));
    Ok(())
}
#[test]
fn hybrid_search_reports_union_truncation() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let mut facts = FakeFacts::for_source(&binding);
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
    let library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts))?
        .with_hybrid(Arc::new(StaticHybrid {
            identity: identity.clone(),
            symbol_ids: vec!["semantic-one".to_string(), "semantic-two".to_string()],
        }));
    let mut selector = search_selector(SearchLane::Hybrid, "needle");
    selector.limit = 3;
    selector.hybrid_identity = Some(identity);

    let response = library.query(request(
        &binding,
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
    let (temporary, binding) = source_repo()?;
    let facts = FakeFacts::for_source(&binding);
    let library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts))?;

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
                &binding,
                EvidenceOperation::Graph { graph: selector },
            ))
            .expect_err("target is valid only for directed_path");
        assert_eq!(error.code(), "invalid_selector", "query {query:?}");
    }
    Ok(())
}

#[test]
fn dirty_indexed_files_and_documentation_are_citable() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let root = temporary.path();
    let dirty = "pub fn edited() {}\n";
    std::fs::write(root.join("src/lib.rs"), dirty)?;
    std::fs::create_dir_all(root.join("docs/guides"))?;
    let docs = "Use postgresql://gobby:gobby@localhost for local development.\n";
    std::fs::write(root.join("docs/guides/cli-commands.md"), docs)?;
    let mut facts = FakeFacts::for_source(&binding);
    facts.files[0].content_hash = gobby_core::indexing::content_hash(dirty.as_bytes());
    facts.files.push(FileFact {
        id: FileId::new("docs/guides/cli-commands.md"),
        path: "docs/guides/cli-commands.md".into(),
        language: "markdown".into(),
        symbol_count: 0,
        content_hash: gobby_core::indexing::content_hash(docs.as_bytes()),
    });
    let library = EvidenceLibrary::new(root, binding.clone(), Arc::new(facts))?;
    for (path, expected) in [("src/lib.rs", dirty), ("docs/guides/cli-commands.md", docs)] {
        let response = library.query(request(
            &binding,
            EvidenceOperation::Read {
                read: ReadSelector::Range {
                    path: path.into(),
                    start_line: 1,
                    end_line: 1,
                },
            },
        ))?;
        let EvidenceItem::Source(source) = &response.items[0] else {
            panic!("source evidence");
        };
        assert_eq!(source.excerpt, expected);
        assert_eq!(
            source.content_hash,
            gobby_core::indexing::content_hash(expected.as_bytes())
        );
    }
    std::fs::write(root.join("src/lib.rs"), "changed after indexing\n")?;
    let error = library
        .query(request(
            &binding,
            EvidenceOperation::Read {
                read: ReadSelector::Range {
                    path: "src/lib.rs".into(),
                    start_line: 1,
                    end_line: 1,
                },
            },
        ))
        .expect_err("unindexed edit cannot carry the old hash");
    assert_eq!(error.code(), "stale_range");
    Ok(())
}

#[test]
fn evidence_rejects_foreign_index_and_unsafe_provenance() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let mut facts = FakeFacts::for_source(&binding);
    facts.project_id = "another-project".into();
    assert!(EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts)).is_err());
    let mut malformed = request(
        &binding,
        EvidenceOperation::Read {
            read: ReadSelector::CommitMetadata,
        },
    );
    malformed.binding.commit_oid = "--output=/tmp/foreign".into();
    assert_eq!(
        validate_request_shape(&malformed)
            .expect_err("full oid required")
            .code(),
        "invalid_selector"
    );
    Ok(())
}
