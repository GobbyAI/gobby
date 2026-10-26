//! Node half of the primary index. Discovers files, hashes them, and parses the paths the
//! hub asks for. Nothing here writes: explicit runs only read the indexed-path list through
//! `conn`, and every write happens in `primary`.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use postgres::Client;

use crate::config::Context;
use crate::db;
use crate::index::semantic::SemanticCallResolver;
use crate::index::{chunker, hasher, languages, parser, walker};
use crate::node::{FileFacts, FileHash, ParsedFacts, ParsedFileFacts};

use super::file::{ExplicitFileRoute, create_semantic_resolver_if_needed};
use super::lifecycle::current_file_state;
use super::pipeline::{discovery_options, explicit_route_with_discovery_options};
use super::selector_blob::{self, HeadBlobs};
use super::types::{IndexRequest, UnsupportedFileType};
use super::util::{
    effective_excludes, filter_discovered_paths, relative_path, requested_relative_path,
    unsupported_file_types,
};

/// The kind of run a scan serves. A failed parse is recorded differently for each.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ScanKind {
    Discovered,
    Explicit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Route {
    Ast,
    ContentOnly,
}

#[derive(Debug)]
struct RoutedFile {
    abs: PathBuf,
    route: Route,
}

/// What one node pass knows about the project: its routed files, their hashes, and the
/// outcomes already decided while routing.
pub(crate) struct NodeScan {
    pub(crate) kind: ScanKind,
    /// Files the run covers, before any stale check: the scanned count of the run.
    pub(crate) scanned: usize,
    /// Routed explicit files. Discovered runs leave this at zero.
    pub(crate) routed: usize,
    pub(crate) unsupported: Vec<UnsupportedFileType>,
    /// Explicit files skipped without a repository-relative path, so they have no state row.
    pub(crate) extra_skipped: usize,
    root: PathBuf,
    project_id: String,
    excludes: Vec<String>,
    head_blobs: HeadBlobs,
    import_context: parser::ImportResolutionContext,
    semantic_resolver: Option<Box<dyn SemanticCallResolver>>,
    routes: HashMap<String, RoutedFile>,
    hashes: Vec<FileHash>,
    unhashed: Vec<String>,
    preset: Vec<ParsedFileFacts>,
}

impl NodeScan {
    /// Discovery for whole and filtered runs, routing for explicit files.
    pub(crate) fn for_request(
        conn: &mut Client,
        request: &IndexRequest,
        ctx: &Context,
    ) -> anyhow::Result<Self> {
        if request.explicit_files.is_empty() {
            Self::discover(request, ctx)
        } else {
            Self::explicit(conn, request, ctx)
        }
    }

    /// Every hashed routed file, in discovery order.
    pub(crate) fn file_hashes(&self) -> &[FileHash] {
        &self.hashes
    }

    /// Outcomes decided during routing: absent explicit paths and skipped explicit routes.
    pub(crate) fn preset(&self) -> Vec<ParsedFileFacts> {
        self.preset.clone()
    }

    /// Routed paths that exist but have no hash and are not parsed by this run. Each is marked
    /// present, so the orphan sweep keeps its state.
    pub(crate) fn unhashed_kept(&self, full: bool) -> Vec<ParsedFileFacts> {
        match self.kind {
            ScanKind::Discovered if !full => self
                .unhashed
                .iter()
                .map(|path| ParsedFileFacts {
                    path: path.clone(),
                    facts: FileFacts::Unhashed,
                })
                .collect(),
            ScanKind::Discovered | ScanKind::Explicit => Vec::new(),
        }
    }

    /// Routed paths with no hash, so the plan cannot judge them. Explicit runs parse them.
    /// A discovered run parses them only when full, since a full run parses every file.
    pub(crate) fn unhashed_to_parse(&self, full: bool) -> Vec<String> {
        match self.kind {
            ScanKind::Explicit => self.unhashed.clone(),
            ScanKind::Discovered if full => self.unhashed.clone(),
            ScanKind::Discovered => Vec::new(),
        }
    }

    fn discover(request: &IndexRequest, ctx: &Context) -> anyhow::Result<Self> {
        let root = request.project_root.as_path();
        let head_blobs = HeadBlobs::read(root);
        let excludes_ref = effective_excludes(&ctx.indexing.extra_excludes);
        let (mut candidates, mut content_only) =
            walker::discover_files_with_options(root, &excludes_ref, discovery_options(ctx));
        if let Some(filter) = request.path_filter.as_deref() {
            candidates = filter_discovered_paths(root, filter, candidates);
            content_only = filter_discovered_paths(root, filter, content_only);
        }
        let unsupported = unsupported_file_types(root, &content_only);
        let scanned = candidates.len() + content_only.len();
        let import_context = parser::build_import_resolution_context(root, &candidates);
        let semantic_resolver =
            create_semantic_resolver_if_needed(root, &candidates, request.require_cpp_semantics)?;
        let current = current_file_state(root, &candidates, &content_only, &head_blobs);

        let mut routes = HashMap::new();
        let mut order = Vec::new();
        for (paths, route) in [
            (&candidates, Route::Ast),
            (&content_only, Route::ContentOnly),
        ] {
            for abs in paths {
                if let Ok(rel) = relative_path(abs, root) {
                    order.push(rel.clone());
                    routes.insert(
                        rel,
                        RoutedFile {
                            abs: abs.clone(),
                            route,
                        },
                    );
                }
            }
        }
        let (hashes, unhashed) = hash_routed(&order, &current.hashes, &current.clean_blobs);

        Ok(Self {
            kind: ScanKind::Discovered,
            scanned,
            routed: 0,
            unsupported,
            extra_skipped: 0,
            root: root.to_path_buf(),
            project_id: ctx.project_id.clone(),
            excludes: excludes_ref.iter().map(|s| s.to_string()).collect(),
            head_blobs,
            import_context,
            semantic_resolver,
            routes,
            hashes,
            unhashed,
            preset: Vec::new(),
        })
    }

    fn explicit(conn: &mut Client, request: &IndexRequest, ctx: &Context) -> anyhow::Result<Self> {
        let root = request.project_root.as_path();
        let head_blobs = HeadBlobs::read(root);
        let excludes_ref = effective_excludes(&ctx.indexing.extra_excludes);
        let mut preset = Vec::new();
        let mut extra_skipped = 0;
        let mut routed = 0;
        let mut order = Vec::new();
        let mut routes = HashMap::new();
        let mut ast_files = Vec::new();
        let mut content_files = Vec::new();
        for fp in &request.explicit_files {
            let abs = if fp.is_absolute() {
                fp.clone()
            } else {
                root.join(fp)
            };
            if !abs.exists() {
                preset.push(ParsedFileFacts {
                    path: requested_relative_path(root, fp),
                    facts: FileFacts::Absent,
                });
                continue;
            }
            let route = match explicit_route_with_discovery_options(
                root,
                &abs,
                &excludes_ref,
                discovery_options(ctx),
            ) {
                ExplicitFileRoute::Ast => Route::Ast,
                ExplicitFileRoute::ContentOnly => Route::ContentOnly,
                ExplicitFileRoute::Skip => {
                    match relative_path(&abs, root) {
                        Ok(path) => preset.push(ParsedFileFacts {
                            path,
                            facts: FileFacts::Removed,
                        }),
                        Err(_) => extra_skipped += 1,
                    }
                    continue;
                }
            };
            routed += 1;
            match route {
                Route::Ast => ast_files.push(abs.clone()),
                Route::ContentOnly => content_files.push(abs.clone()),
            }
            match relative_path(&abs, root) {
                Ok(rel) => {
                    order.push(rel.clone());
                    routes.insert(rel, RoutedFile { abs, route });
                }
                Err(_) => extra_skipped += 1,
            }
        }

        let mut seen = HashSet::new();
        let mut import_candidates: Vec<PathBuf> =
            db::list_indexed_file_paths(conn, &ctx.project_id, &ctx.machine_id)?
                .into_iter()
                .map(|path| root.join(path))
                .filter(|path| seen.insert(path.clone()))
                .collect();
        for path in &ast_files {
            if seen.insert(path.clone()) {
                import_candidates.push(path.clone());
            }
        }
        let import_context = parser::build_import_resolution_context(root, &import_candidates);
        let semantic_resolver =
            create_semantic_resolver_if_needed(root, &ast_files, request.require_cpp_semantics)?;

        let mut hashes = Vec::new();
        let mut unhashed = Vec::new();
        for rel in &order {
            match selector_blob::read_snapshot(&routes[rel].abs, rel, &head_blobs) {
                Ok(snapshot) => hashes.push(FileHash {
                    path: rel.clone(),
                    content_hash: snapshot.content_hash,
                    git_blob: snapshot.clean_blob,
                }),
                Err(_) => unhashed.push(rel.clone()),
            }
        }

        Ok(Self {
            kind: ScanKind::Explicit,
            scanned: request.explicit_files.len(),
            routed,
            unsupported: unsupported_file_types(root, &content_files),
            extra_skipped,
            root: root.to_path_buf(),
            project_id: ctx.project_id.clone(),
            excludes: excludes_ref.iter().map(|s| s.to_string()).collect(),
            head_blobs,
            import_context,
            semantic_resolver,
            routes,
            hashes,
            unhashed,
            preset,
        })
    }

    /// Parses the named paths. A path the scan did not route is absent, or excluded if it
    /// still exists.
    pub(crate) fn parse_paths(&mut self, paths: &[String]) -> anyhow::Result<Vec<ParsedFileFacts>> {
        let mut parsed = Vec::with_capacity(paths.len());
        for rel in paths {
            let facts = match self
                .routes
                .get(rel)
                .map(|file| (file.abs.clone(), file.route))
            {
                Some((abs, Route::Ast)) => self.parse_ast(&abs, rel)?,
                Some((abs, Route::ContentOnly)) => self.parse_content(&abs, rel),
                None if self.root.join(rel).exists() => FileFacts::Skipped,
                None => FileFacts::Absent,
            };
            parsed.push(ParsedFileFacts {
                path: rel.clone(),
                facts,
            });
        }
        Ok(parsed)
    }

    /// Parses one source file as the local index always has. The parser declining a file
    /// removes it on a discovered run and only skips it on an explicit run.
    fn parse_ast(&mut self, abs: &Path, rel: &str) -> anyhow::Result<FileFacts> {
        let Some(parse_result) = parser::parse_file_with_semantic(
            abs,
            &self.project_id,
            &self.root,
            self.excludes.as_slice(),
            &self.import_context,
            self.semantic_resolver.as_deref_mut(),
        )?
        else {
            return Ok(match self.kind {
                ScanKind::Discovered => FileFacts::Removed,
                ScanKind::Explicit => FileFacts::Skipped,
            });
        };
        let language = languages::detect_language(&abs.to_string_lossy())
            .unwrap_or("unknown")
            .to_string();
        let content_hash = hasher::content_hash(&parse_result.source);
        let byte_size = parse_result.source.len();
        let chunks = chunker::chunk_file_content(
            &parse_result.source,
            rel,
            &self.project_id,
            &content_hash,
            Some(language.as_str()),
        );
        let git_blob = self.head_blobs.clean_blob(rel, &parse_result.source);
        Ok(FileFacts::Parsed {
            language,
            content_hash,
            byte_size,
            git_blob,
            facts: ParsedFacts {
                symbols: parse_result.symbols,
                imports: parse_result.imports,
                calls: parse_result.calls,
                inheritance: parse_result.inheritance,
                chunks,
            },
        })
    }

    /// Reads one content-only file. A file the walker would not index is skipped.
    fn parse_content(&self, abs: &Path, rel: &str) -> FileFacts {
        if !walker::is_content_indexable(&self.root, abs, self.excludes.as_slice()) {
            return FileFacts::Skipped;
        }
        let source = match std::fs::read(abs) {
            Ok(source) => source,
            Err(error) => {
                log::debug!(
                    "skipping content-only index for unreadable file {}: {error}",
                    abs.display()
                );
                return FileFacts::Skipped;
            }
        };
        let language = walker::content_language(abs);
        let content_hash = hasher::content_hash(&source);
        let chunks = chunker::chunk_file_content(
            &source,
            rel,
            &self.project_id,
            &content_hash,
            Some(language.as_str()),
        );
        let git_blob = self.head_blobs.clean_blob(rel, &source);
        FileFacts::ContentOnly {
            language,
            content_hash,
            byte_size: source.len(),
            git_blob,
            chunks,
        }
    }
}

/// Pairs each routed path with its hash in discovery order. A path with no hash goes to the
/// unhashed list instead.
fn hash_routed(
    order: &[String],
    hashes: &HashMap<String, String>,
    clean_blobs: &HashMap<String, String>,
) -> (Vec<FileHash>, Vec<String>) {
    let mut found = Vec::new();
    let mut unhashed = Vec::new();
    for rel in order {
        match hashes.get(rel) {
            Some(content_hash) => found.push(FileHash {
                path: rel.clone(),
                content_hash: content_hash.clone(),
                git_blob: clean_blobs.get(rel).cloned(),
            }),
            None => unhashed.push(rel.clone()),
        }
    }
    (found, unhashed)
}
