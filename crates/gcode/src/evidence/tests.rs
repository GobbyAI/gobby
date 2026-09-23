use std::io::Write as _;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::Arc;

use crate::codewiki_facts::{
    CommunityFact, ContentFact, FileFact, FileId, GraphBounds as FactsGraphBounds, GraphEdge,
    GraphEdgeKind, GraphOutcome, GraphScopeMode, GrepContextLineFact, GrepHit, GrepOutcome,
    GrepQuery, GrepSpanFact, ProjectCommunities, ScopeSelector, ScopedGraph, SearchQuery,
    SymbolFact,
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
    communities: ProjectCommunities,
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
            communities: ProjectCommunities::default(),
        }
    }
}

impl EvidenceFacts for FakeFacts {
    fn project_id(&self) -> &str {
        &self.project_id
    }

    fn project_communities(&self) -> anyhow::Result<ProjectCommunities> {
        Ok(self.communities.clone())
    }

    fn files(&self) -> anyhow::Result<Vec<FileFact>> {
        Ok(self.files.clone())
    }

    fn search_symbols(&self, query: &SearchQuery) -> anyhow::Result<FactPage<SymbolFact>> {
        let needle = query.text.to_lowercase();
        // Mirror the index's glob post-filter so scope handling is exercised.
        let patterns = crate::search::fts::compile_patterns(&query.paths)?;
        let mut items = self
            .symbols
            .iter()
            .filter(|symbol| {
                (symbol.name.to_lowercase().contains(&needle)
                    || symbol.qualified_name.to_lowercase().contains(&needle))
                    && (patterns.is_empty()
                        || patterns
                            .iter()
                            .any(|pattern| pattern.matches(&symbol.file_path)))
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
    assert_eq!(
        source.numbered_excerpt,
        "3| fn alpha() {\n4|     beta();\n5|     gamma();\n6| }\n"
    );
    assert!(first.warnings.is_empty());
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
            read: ReadSelector::CommitMetadata { commit_oid: None },
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

#[test]
fn commit_metadata_can_read_history_without_rebinding_the_source_index() -> anyhow::Result<()> {
    let (temporary, mut binding) = source_repo()?;
    let repo = temporary.path();
    std::fs::write(repo.join("history.txt"), "historical value\n")?;
    git(repo, &["add", "history.txt"])?;
    binding.commit_oid = commit(repo, "historical change")?;
    binding.tree_oid = git(repo, &["rev-parse", "HEAD^{tree}"])?;
    std::fs::write(repo.join("history.txt"), "current value\n")?;
    std::fs::write(repo.join("later.txt"), "later\n")?;
    git(repo, &["add", "history.txt", "later.txt"])?;
    let later = commit(repo, "later change")?;
    let current_binding = RepositoryBinding {
        commit_oid: later.clone(),
        tree_oid: git(repo, &["rev-parse", "HEAD^{tree}"])?,
        ..binding.clone()
    };
    let library = EvidenceLibrary::new(
        repo,
        current_binding.clone(),
        Arc::new(FakeFacts::for_source(&current_binding)),
    )?;
    let response = library.query(request(
        &current_binding,
        EvidenceOperation::Read {
            read: ReadSelector::CommitMetadata {
                commit_oid: Some(binding.commit_oid.clone()),
            },
        },
    ))?;
    assert_eq!(response.binding, current_binding);
    assert!(!response.items.is_empty());
    for item in response.items {
        let EvidenceItem::CommitMetadata(item) = item else {
            panic!("expected commit metadata");
        };
        assert_eq!(item.commit_oid, binding.commit_oid);
        assert_ne!(item.commit_oid, later);
        assert!(item.patch.contains("+historical value"));
        assert!(!item.patch.contains("current value"));
        assert_eq!(
            item.record_hash,
            super::source::canonical_hash(&(&item.changed_path, &item.patch))?
        );
        assert_ne!(
            item.changed_path.and_then(|path| path.new_path),
            Some("later.txt".to_string())
        );
    }
    for invalid in ["HEAD", "--all", "", "not-a-commit"] {
        assert!(matches!(
            read::validate_selector(&ReadSelector::CommitMetadata {
                commit_oid: Some(invalid.to_string()),
            }),
            Err(EvidenceError::InvalidSelector { .. })
        ));
    }
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
fn symbol_search_honors_directory_scopes() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let library = EvidenceLibrary::new(
        temporary.path(),
        binding.clone(),
        Arc::new(FakeFacts::for_source(&binding)),
    )?;
    for (scope, expected_items) in [("src", 1), ("src/lib.rs", 1), ("lib", 0)] {
        let mut selector = search_selector(SearchLane::Symbol, "alpha");
        selector.paths = vec![scope.to_string()];
        let response = library.query(request(
            &binding,
            EvidenceOperation::Search { search: selector },
        ))?;
        assert_eq!(response.items.len(), expected_items, "scope {scope}");
    }
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
            read: ReadSelector::CommitMetadata { commit_oid: None },
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

fn range_read(start_line: usize, end_line: usize) -> EvidenceOperation {
    EvidenceOperation::Read {
        read: ReadSelector::Range {
            path: "src/lib.rs".into(),
            start_line,
            end_line,
        },
    }
}

#[test]
fn range_read_past_end_of_file_stops_at_last_line_with_warning() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let facts = FakeFacts::for_source(&binding);
    let library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts))?;

    let response = library.query(request(&binding, range_read(8, 20)))?;
    assert_eq!(response.completeness, Completeness::Complete);
    let EvidenceItem::Source(source) = &response.items[0] else {
        panic!("source evidence")
    };
    assert_eq!((source.line_start, source.line_end), (8, 9));
    assert_eq!(source.excerpt, "fn beta() {}\nfn gamma() {}\n");
    assert_eq!(
        source.numbered_excerpt,
        "8| fn beta() {}\n9| fn gamma() {}\n"
    );
    assert_eq!(
        response.warnings,
        vec![EvidenceWarning {
            code: "range_clamped_to_end_of_file".into(),
            message: "requested lines 8..20; file ends at line 9".into(),
            path: Some("src/lib.rs".into()),
        }]
    );

    let error = library
        .query(request(&binding, range_read(10, 12)))
        .expect_err("a range starting past the file has nothing to clamp");
    assert_eq!(error.code(), "invalid_selector");
    assert!(
        error.to_string().contains("src/lib.rs has 9 line(s)"),
        "{error}"
    );
    Ok(())
}

#[test]
fn index_derived_ranges_past_end_of_file_stay_stale() -> anyhow::Result<()> {
    let (temporary, binding) = source_repo()?;
    let facts = FakeFacts::for_source(&binding);
    let library = EvidenceLibrary::new(temporary.path(), binding, Arc::new(facts))?;
    let error = read::source_for_lines(&library, "src/lib.rs", 8, 20, None)
        .expect_err("search and graph lines past the file mean stale facts");
    assert_eq!(error.code(), "stale_range");
    Ok(())
}

#[test]
fn numbered_excerpt_numbers_every_line_including_an_unterminated_last_line() {
    assert_eq!(
        read::numbered_excerpt("fn beta() {}\nfn gamma() {}", 8),
        "8| fn beta() {}\n9| fn gamma() {}"
    );
    assert_eq!(read::numbered_excerpt("\n\n", 1), "1| \n2| \n");
    assert_eq!(read::numbered_excerpt("", 1), "");
}

fn community_binding() -> RepositoryBinding {
    RepositoryBinding {
        project_id: "project-1".to_string(),
        commit_oid: "a".repeat(40),
        tree_oid: "b".repeat(40),
    }
}

/// A deterministic-labeled community of `size` members named `c{id}/m000.py` upward.
fn community(community_id: i32, label: &str, size: usize) -> CommunityFact {
    CommunityFact {
        community_id,
        label: label.to_string(),
        label_deterministic: format!("c{community_id}"),
        label_source: "deterministic".to_string(),
        label_confidence: None,
        label_stale: false,
        size,
        cohesion: 0.5,
        internal_edges: size,
        member_signature: format!("signature-{community_id}"),
        members: (0..size)
            .map(|index| format!("c{community_id}/m{index:03}.py"))
            .collect(),
        representatives: Vec::new(),
        boundary: Vec::new(),
    }
}

/// Every member of every community is an indexed file except `missing`.
fn community_library(
    binding: &RepositoryBinding,
    refreshed: bool,
    communities: Vec<CommunityFact>,
    missing: &[&str],
) -> anyhow::Result<(tempfile::TempDir, EvidenceLibrary)> {
    let temporary = tempfile::tempdir()?;
    let files = communities
        .iter()
        .flat_map(|community| community.members.iter())
        .filter(|path| !missing.contains(&path.as_str()))
        .map(|path| FileFact {
            id: FileId::new(path.clone()),
            path: path.clone(),
            language: "python".to_string(),
            symbol_count: 0,
            content_hash: format!("hash:{path}"),
        })
        .collect();
    let facts = FakeFacts {
        project_id: binding.project_id.clone(),
        files,
        symbols: Vec::new(),
        edges: Vec::new(),
        communities: ProjectCommunities {
            refreshed,
            communities,
        },
    };
    let library = EvidenceLibrary::new(temporary.path(), binding.clone(), Arc::new(facts))?;
    Ok((temporary, library))
}

fn communities_request(
    binding: &RepositoryBinding,
    communities: CommunitiesSelector,
) -> EvidenceRequest {
    request(binding, EvidenceOperation::Communities { communities })
}

fn community_items(response: &EvidenceResponse) -> Vec<&CommunityEvidence> {
    response
        .items
        .iter()
        .map(|item| match item {
            EvidenceItem::Community(community) => community,
            other => panic!("community evidence, got {other:?}"),
        })
        .collect()
}

fn community_ids(response: &EvidenceResponse) -> Vec<i32> {
    community_items(response)
        .iter()
        .map(|community| community.community_id)
        .collect()
}

fn warning_codes(response: &EvidenceResponse) -> Vec<&str> {
    response
        .warnings
        .iter()
        .map(|warning| warning.code.as_str())
        .collect()
}

fn community_member(path: &str) -> CommunityMember {
    CommunityMember {
        path: path.to_string(),
        content_hash: format!("hash:{path}"),
    }
}

#[test]
fn communities_list_orders_by_size_then_id() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) = community_library(
        &binding,
        true,
        vec![
            community(1, "nine", 9),
            community(10, "ten b", 10),
            community(2, "hundred", 100),
            community(9, "ten a", 10),
            community(3, "eleven", 11),
        ],
        &[],
    )?;

    let response = library.query(communities_request(
        &binding,
        CommunitiesSelector::default(),
    ))?;
    assert_eq!(community_ids(&response), vec![2, 3, 9, 10, 1]);
    assert_eq!(response.completeness, Completeness::Complete);
    assert!(
        community_items(&response)
            .iter()
            .all(|community| community.members.is_empty())
    );
    assert!(
        response
            .items
            .iter()
            .all(|item| item.canonical_key().starts_with("3\0"))
    );

    let limited = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            limit: Some(2),
            ..CommunitiesSelector::default()
        },
    ))?;
    assert_eq!(community_ids(&limited), vec![2, 3]);
    assert_eq!(limited.completeness, Completeness::TruncatedIndex);
    Ok(())
}

#[test]
fn communities_detail_bounds_members_and_flags_truncation() -> anyhow::Result<()> {
    let binding = community_binding();
    let mut selected = community(7, "Parsing", 6);
    selected.members = [
        "pkg/e.py",
        "pkg/d.py",
        "pkg/c.py",
        "pkg/b.py",
        "pkg/a.py",
        "pkg/gone.py",
    ]
    .map(String::from)
    .to_vec();
    selected.representatives = vec!["pkg/c.py".to_string(), "pkg/a.py".to_string()];
    selected.boundary = vec![(8, 3), (99, 1)];
    let (_temporary, library) = community_library(
        &binding,
        true,
        vec![selected, community(8, "Rendering", 2)],
        &["pkg/gone.py"],
    )?;

    let response = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            community_id: Some(7),
            max_members: Some(3),
            ..CommunitiesSelector::default()
        },
    ))?;
    let items = community_items(&response);
    assert_eq!(items.len(), 1);
    assert_eq!(response.completeness, Completeness::Complete);
    assert_eq!(items[0].size, 6);
    assert_eq!(
        items[0].members,
        vec![
            community_member("pkg/c.py"),
            community_member("pkg/a.py"),
            community_member("pkg/b.py"),
        ]
    );
    assert!(items[0].members_truncated);
    assert_eq!(
        items[0].boundary,
        vec![
            CommunityBoundary {
                other_community_id: 8,
                label: "Rendering".to_string(),
                import_count: 3,
            },
            CommunityBoundary {
                other_community_id: 99,
                label: String::new(),
                import_count: 1,
            },
        ]
    );
    assert_eq!(
        response
            .warnings
            .iter()
            .map(|warning| (warning.code.as_str(), warning.path.as_deref()))
            .collect::<Vec<_>>(),
        vec![("community_member_not_in_snapshot", None)]
    );

    let by_path = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            path: Some("pkg/d.py".to_string()),
            max_members: Some(5),
            ..CommunitiesSelector::default()
        },
    ))?;
    let items = community_items(&by_path);
    assert_eq!(community_ids(&by_path), vec![7]);
    assert_eq!(items[0].members.len(), 5);
    assert!(!items[0].members_truncated);
    Ok(())
}

#[test]
fn communities_detail_reports_snapshot_misses_in_one_warning() -> anyhow::Result<()> {
    let binding = community_binding();
    let selected = community(9, "Large", 40);
    let missing = selected.members[..35]
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>();
    let (_temporary, library) =
        community_library(&binding, true, vec![selected.clone()], &missing)?;

    let response = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            community_id: Some(9),
            max_members: Some(2),
            ..CommunitiesSelector::default()
        },
    ))?;
    let items = community_items(&response);
    assert_eq!(
        items[0].members,
        vec![
            community_member("c9/m035.py"),
            community_member("c9/m036.py")
        ]
    );
    assert!(items[0].members_truncated);
    assert_eq!(
        warning_codes(&response),
        vec!["community_member_not_in_snapshot"]
    );
    assert!(
        response.warnings[0]
            .message
            .ends_with(": 35 (first c9/m000.py)"),
        "{}",
        response.warnings[0].message
    );
    Ok(())
}

#[test]
fn communities_without_rows_is_complete_empty_with_hint() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) = community_library(&binding, false, Vec::new(), &[])?;

    for selector in [
        CommunitiesSelector::default(),
        CommunitiesSelector {
            community_id: Some(1),
            ..CommunitiesSelector::default()
        },
    ] {
        let response = library.query(communities_request(&binding, selector))?;
        assert_eq!(response.completeness, Completeness::CompleteEmpty);
        assert!(response.items.is_empty());
        assert_eq!(
            response.warnings,
            vec![EvidenceWarning {
                code: "community_partition_missing".to_string(),
                message: crate::communities::MISSING_PARTITION_HINT.to_string(),
                path: None,
            }]
        );
    }
    Ok(())
}

#[test]
fn communities_refreshed_but_empty_is_complete_empty() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) = community_library(&binding, true, Vec::new(), &[])?;

    for selector in [
        CommunitiesSelector::default(),
        CommunitiesSelector {
            label: Some("auth".to_string()),
            ..CommunitiesSelector::default()
        },
    ] {
        let response = library.query(communities_request(&binding, selector))?;
        assert_eq!(response.completeness, Completeness::CompleteEmpty);
        assert!(response.items.is_empty());
        assert!(response.warnings.is_empty());
    }
    Ok(())
}

#[test]
fn communities_ambiguous_label_returns_every_match_with_warning() -> anyhow::Result<()> {
    let binding = community_binding();
    let mut first = community(1, "Auth Flow", 3);
    first.label_deterministic = "src/auth".to_string();
    let mut second = community(2, "auth flow", 4);
    second.label_deterministic = "src/login".to_string();
    let mut storage = community(3, "Storage", 3);
    storage.label_deterministic = "src/storage".to_string();
    let mut tools = community(4, "Storage Tools", 3);
    tools.label_deterministic = "src/tools".to_string();
    let mut payments = community(5, "Payments", 2);
    payments.label_deterministic = "src/payments".to_string();
    payments.members = vec!["src/billing.py".to_string(), "src/ledger.py".to_string()];
    let (_temporary, library) = community_library(
        &binding,
        true,
        vec![first, second, storage, tools, payments],
        &[],
    )?;
    let by_label = |label: &str| {
        library.query(communities_request(
            &binding,
            CommunitiesSelector {
                label: Some(label.to_string()),
                ..CommunitiesSelector::default()
            },
        ))
    };

    let ambiguous = by_label("AUTH FLOW")?;
    assert_eq!(community_ids(&ambiguous), vec![2, 1]);
    assert_eq!(
        warning_codes(&ambiguous),
        vec!["community_selector_ambiguous"]
    );

    let exact = by_label("storage")?;
    assert_eq!(community_ids(&exact), vec![3]);
    assert!(exact.warnings.is_empty());

    let deterministic = by_label("src/tools")?;
    assert_eq!(community_ids(&deterministic), vec![4]);

    let substring = by_label("flow")?;
    assert_eq!(community_ids(&substring), vec![2, 1]);
    assert_eq!(
        warning_codes(&substring),
        vec!["community_selector_ambiguous"]
    );

    let member_only = by_label("billing")?;
    assert_eq!(member_only.completeness, Completeness::CompleteEmpty);
    assert!(member_only.items.is_empty());
    assert!(member_only.warnings.is_empty());
    Ok(())
}

#[test]
fn communities_below_min_size_is_complete_empty() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) = community_library(
        &binding,
        true,
        vec![
            community(1, "pair", 2),
            community(2, "triple", 3),
            community(3, "single", 1),
        ],
        &[],
    )?;

    let above = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            min_size: Some(4),
            ..CommunitiesSelector::default()
        },
    ))?;
    assert_eq!(above.completeness, Completeness::CompleteEmpty);
    assert!(above.items.is_empty());

    let singleton = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            community_id: Some(3),
            ..CommunitiesSelector::default()
        },
    ))?;
    assert_eq!(singleton.completeness, Completeness::CompleteEmpty);
    assert!(singleton.items.is_empty());

    let listed = library.query(communities_request(
        &binding,
        CommunitiesSelector::default(),
    ))?;
    assert_eq!(community_ids(&listed), vec![2, 1]);
    Ok(())
}

#[test]
fn communities_evidence_id_survives_relabel() -> anyhow::Result<()> {
    let binding = community_binding();
    let evidence_id =
        |binding: &RepositoryBinding, fact: CommunityFact| -> anyhow::Result<String> {
            let (_temporary, library) = community_library(binding, true, vec![fact], &[])?;
            let response =
                library.query(communities_request(binding, CommunitiesSelector::default()))?;
            assert_eq!(response.items.len(), 1);
            Ok(response.items[0].evidence_id().to_string())
        };
    let original = community(4, "Before", 3);
    let mut relabeled = original.clone();
    relabeled.label = "After".to_string();
    relabeled.label_source = "model".to_string();
    relabeled.label_confidence = Some(0.9);
    let mut regrouped = original.clone();
    regrouped.member_signature = "signature-changed".to_string();
    let mut rebound = binding.clone();
    rebound.commit_oid = "c".repeat(40);

    let original_id = evidence_id(&binding, original.clone())?;
    assert!(original_id.starts_with("com:"), "{original_id}");
    assert_eq!(evidence_id(&binding, relabeled)?, original_id);
    assert_ne!(evidence_id(&binding, regrouped)?, original_id);
    assert_ne!(evidence_id(&rebound, original)?, original_id);
    Ok(())
}

#[test]
fn communities_selector_rejects_two_keys() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) =
        community_library(&binding, true, vec![community(1, "Auth", 2)], &[])?;

    for selector in [
        CommunitiesSelector {
            community_id: Some(1),
            label: Some("Auth".to_string()),
            ..CommunitiesSelector::default()
        },
        CommunitiesSelector {
            community_id: Some(1),
            path: Some("c1/m000.py".to_string()),
            ..CommunitiesSelector::default()
        },
        CommunitiesSelector {
            label: Some("Auth".to_string()),
            path: Some("c1/m000.py".to_string()),
            ..CommunitiesSelector::default()
        },
    ] {
        let error = library
            .query(communities_request(&binding, selector.clone()))
            .expect_err(&format!("two selector keys must be rejected: {selector:?}"));
        assert_eq!(error.code(), "invalid_selector");
    }
    Ok(())
}

#[test]
fn communities_selector_rejects_invalid_bounds() -> anyhow::Result<()> {
    let binding = community_binding();
    let (_temporary, library) =
        community_library(&binding, true, vec![community(1, "Auth", 2)], &[])?;

    for (selector, code) in [
        (
            CommunitiesSelector {
                community_id: Some(1),
                max_members: Some(0),
                ..CommunitiesSelector::default()
            },
            "invalid_selector",
        ),
        (
            CommunitiesSelector {
                community_id: Some(1),
                max_members: Some(501),
                ..CommunitiesSelector::default()
            },
            "invalid_selector",
        ),
        (
            CommunitiesSelector {
                limit: Some(0),
                ..CommunitiesSelector::default()
            },
            "invalid_selector",
        ),
        (
            CommunitiesSelector {
                label: Some("  ".to_string()),
                ..CommunitiesSelector::default()
            },
            "invalid_selector",
        ),
        (
            CommunitiesSelector {
                path: Some("../outside.py".to_string()),
                ..CommunitiesSelector::default()
            },
            "unsafe_path",
        ),
    ] {
        let error = library
            .query(communities_request(&binding, selector.clone()))
            .expect_err(&format!("selector must be rejected: {selector:?}"));
        assert_eq!(error.code(), code, "{selector:?}");
    }
    let widest = library.query(communities_request(
        &binding,
        CommunitiesSelector {
            community_id: Some(1),
            max_members: Some(500),
            ..CommunitiesSelector::default()
        },
    ))?;
    assert_eq!(community_ids(&widest), vec![1]);
    Ok(())
}
