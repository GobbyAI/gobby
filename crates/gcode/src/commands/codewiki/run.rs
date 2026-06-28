use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use gobby_core::ai::AiNoticeKind;
use gobby_core::config::AiRouting;

use crate::commands::scope;
use crate::config::{self, Context};
use crate::db;
use crate::models::Symbol;
use crate::output::{self, Format};
use crate::visibility;

use super::{
    BuiltDoc, CodewikiAiOptions, CodewikiInput, CodewikiProgress, CodewikiRunSummary,
    DEFAULT_OUT_DIR, DocPruneScope, DocSink, LeadingChunk, MAX_EDGE_LIMIT, ReusePlan,
    build_audit_context, build_codewiki_changes_doc, build_codewiki_index_snapshot,
    build_feature_catalog_doc, build_system_model, build_truth_digest, fetch_codewiki_graph_edges,
    generation, in_scope, io, is_core_file, read_ownership_meta, resolve_text_generator,
    resolve_text_verifier, resolve_tool_loop_generator, write_ownership_meta, write_truth_digest,
};

// CLI entry point: each parameter maps to a distinct codewiki flag, so the
// argument count tracks the command surface rather than hidden coupling.
#[allow(clippy::too_many_arguments)]
pub fn run(
    ctx: &Context,
    out: Option<String>,
    scope_args: Vec<String>,
    ai: CodewikiAiOptions,
    edge_limit: usize,
    include_docs: bool,
    since: Option<String>,
    format: Format,
    verbose: bool,
) -> anyhow::Result<()> {
    validate_edge_limit(edge_limit)?;
    let ai_depth = ai.depth;

    let mut progress = CodewikiProgress::stderr(verbose && !ctx.quiet);

    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let scopes = scope_args
        .iter()
        .map(|value| scope::normalize_file_arg(ctx, value))
        .collect::<Vec<_>>();
    progress.emit("loading indexed files");
    let files = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .filter(|file| should_document_file(&file.file_path, include_docs))
        .map(|file| file.file_path)
        .filter(|file| in_scope(file, &scopes))
        .collect::<Vec<_>>();
    let symbols = load_symbols_for_codewiki(&files, &mut progress, |paths| {
        visibility::visible_symbols_for_files(&mut conn, ctx, paths)
    })?;

    progress.emit(format!(
        "fetching graph edges for {} files and {} symbols (limit {})",
        files.len(),
        symbols.len(),
        edge_limit
    ));
    progress.emit("loading leading content chunks");
    let leading_chunks = load_leading_chunks(&mut conn, ctx, &files)?;

    let graph = fetch_codewiki_graph_edges(ctx, &files, &symbols, edge_limit)?;
    let input = CodewikiInput {
        files,
        graph_edges: graph.edges,
        graph_availability: graph.availability,
        symbols,
        leading_chunks,
    };
    // Deterministic workspace model (#891), read straight off the project's
    // Cargo manifests. Seeds the architecture page's model-derived Mermaid
    // diagrams. A partial/empty model simply omits diagrams — never an error.
    let system_model = build_system_model(&ctx.project_root);
    // Deterministic feature catalog (#888), built from the pinned CLI contract
    // JSONs + dispatch resolver. Read straight off the project root; missing or
    // unparseable contracts simply omit that binary's section — never an error.
    let feature_catalog = build_feature_catalog_doc(&ctx.project_root, &input.files);
    // Deterministic audit context (#889): scans the documented source for
    // deprecation markers and the test-gated symbol set. Drives the per-symbol
    // deprecation badge, the `code/deprecations.md` page, and the file page's
    // test-count collapse. Read straight off the project root; unreadable files
    // are skipped — never an error, never degrading.
    let audit_context = build_audit_context(&ctx.project_root, &input);
    let resolved_generator = resolve_text_generator(ctx, &ai);
    let mut ai_notices = AiRunNotices::default();
    ai_notices.warn_once(ctx, resolved_generator.notice_kind());
    let ai_outcome = resolved_generator.ai_outcome();
    let no_generator_reason = resolved_generator.no_generator_reason;
    let mut generator = resolved_generator.generator;
    // Lane B aggregate generator (#978): resolved for the `tool_chat` capability,
    // threaded alongside the Lane A generator. `None` when no tool-chat route
    // resolves (AI off) — aggregates then fall back to the Lane A path. The run's
    // resolved graph availability lets the executor's graph tools return an
    // explicit graph-unavailable result instead of an empty one.
    let mut tool_loop_generator = resolve_tool_loop_generator(ctx, &ai, input.graph_availability);
    let mut verifier = resolve_text_verifier(ctx, &ai);
    let ai_enabled = generator.is_some();
    let ai_mode = if ai_outcome.route == AiRouting::Off
        && !ai_outcome.fallback
        && no_generator_reason.is_none()
    {
        "off"
    } else {
        ai_depth.mode_label()
    };
    let out_dir = out.unwrap_or_else(|| DEFAULT_OUT_DIR.to_string());
    let out_path = Path::new(&out_dir);
    let doc_scope = DocPruneScope::from_scopes(&scopes);
    // `--since <ref>` scopes regeneration to the files git reports changed since
    // the ref plus their dependents, instead of a full content-hash scan of
    // every page (Leaf H, #893). A source page whose own sources and neighbors
    // are all unchanged-since-ref is left exactly as it is; keyed aggregate
    // pages (architecture/infrastructure/features/audit) still re-check their
    // model digest, so a manifest/contract change rebuilds them even here.
    let since_changed = match since.as_deref() {
        Some(since_ref) => {
            progress.emit(format!("scoping to git changes since {since_ref}"));
            Some(git_changed_files(&ctx.project_root, since_ref)?)
        }
        None => None,
    };
    if doc_scope.is_unscoped() {
        progress.emit("reading metadata and hashing snapshot");
    } else {
        progress.emit("reading metadata for scoped write");
    }
    let previous_meta = if doc_scope.is_unscoped() {
        Some(io::read_codewiki_meta(out_path)?)
    } else {
        None
    };
    let index_snapshot = if doc_scope.is_unscoped() {
        Some(build_codewiki_index_snapshot(&ctx.project_root, &input)?)
    } else {
        None
    };
    let mut ownership_meta = if doc_scope.is_unscoped() {
        Some(read_ownership_meta(out_path)?)
    } else {
        None
    };
    let mut reuse_plan = ReusePlan::load_with_since_and_ai_outcome(
        &ctx.project_root,
        out_path,
        ai_mode,
        since_changed.clone(),
        ai_outcome,
    )?;
    let mut reuse = Some(&mut reuse_plan);
    let mut sink =
        DocSink::open_with_prune_scope(&ctx.project_root, out_path, ai_mode, doc_scope.clone())?
            .with_ai_outcome(ai_outcome)
            .with_since(since_changed);
    let mut generated_pages = 0_usize;
    let mut module_count = 0_usize;
    let mut file_count = 0_usize;
    // Persist each doc and its meta entry as soon as it is built, so a killed
    // run keeps everything generated so far and a re-run resumes from disk.
    let mut emit = |doc: BuiltDoc| -> anyhow::Result<()> {
        generated_pages += 1;
        if doc.path.starts_with("code/modules/") {
            module_count += 1;
        }
        if doc.path.starts_with("code/files/") {
            file_count += 1;
        }
        sink.persist(&doc)?;
        Ok(())
    };
    generation::generate_hierarchical_docs_with_ownership(
        &input,
        ownership_meta
            .as_mut()
            .map(|meta| (ctx.project_root.as_path(), meta)),
        Some(&system_model),
        feature_catalog.as_ref(),
        Some(&audit_context),
        generator.as_deref_mut(),
        tool_loop_generator.as_deref_mut(),
        verifier.as_deref_mut(),
        ai_depth,
        &mut reuse,
        &mut progress,
        &doc_scope,
        &mut emit,
    )?;
    if let Some(index_snapshot) = index_snapshot.as_ref() {
        progress.emit("generating changes docs");
        emit(BuiltDoc::healthy(
            "code/_changes.md",
            build_codewiki_changes_doc(
                previous_meta
                    .as_ref()
                    .and_then(|meta| meta.index_snapshot.as_ref()),
                index_snapshot,
            )?,
        ))?;
    }
    if let Some(ownership_meta) = ownership_meta.as_ref() {
        write_ownership_meta(out_path, ownership_meta)?;
    }
    let symbol_count = input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .count();
    // Surface degraded pages (a failed AI pass fell back to the structural
    // body, #900) instead of letting them hide silently in the meta cache. Read
    // before `finish` consumes the sink.
    let degraded_pages = sink.degraded_docs().to_vec();
    if !degraded_pages.is_empty() && !ctx.quiet {
        ai_notices.warn_once(ctx, Some(AiNoticeKind::GenerationFailed));
        // Warn on stderr at parity with the per-file "text generation failed ...
        // record degraded: true" line (text/generation.rs), so a degraded
        // curated/aggregate pass is visible regardless of --verbose rather than
        // only summarized in the run result.
        eprintln!(
            "codewiki: {} page(s) degraded to structural fallback (AI content \
             pass failed): {}",
            degraded_pages.len(),
            degraded_pages.join(", ")
        );
    }
    let changed_paths = sink.finish(index_snapshot)?;
    let skipped = generated_pages.saturating_sub(changed_paths.len());
    if doc_scope.is_unscoped() {
        let truth_digest =
            build_truth_digest(&system_model, &ctx.project_id, file_count, module_count);
        write_truth_digest(out_path, &doc_scope, &truth_digest)?;
    }

    let summary = CodewikiRunSummary {
        command: "codewiki",
        project_id: ctx.project_id.clone(),
        project_root: ctx.project_root.display().to_string(),
        out_dir,
        generated_pages,
        changed_paths,
        skipped,
        files: file_count,
        modules: module_count,
        symbols: symbol_count,
        ai_enabled,
        degraded_pages,
    };
    match format {
        Format::Json => output::print_json(&summary),
        Format::Text => {
            if doc_scope.is_unscoped() {
                output::print_text(&format!(
                    "wrote {} file docs, {} module docs, and repo.md to {}",
                    summary.files, summary.modules, summary.out_dir
                ))
            } else {
                output::print_text(&format!(
                    "wrote {} scoped file docs and {} scoped module docs to {}",
                    summary.files, summary.modules, summary.out_dir
                ))
            }
        }
    }?;

    Ok(())
}

#[derive(Default)]
struct AiRunNotices {
    emitted: BTreeSet<AiNoticeKind>,
}

impl AiRunNotices {
    fn warn_once(&mut self, ctx: &Context, notice: Option<AiNoticeKind>) {
        if ctx.quiet {
            return;
        }
        let Some(notice) = notice else {
            return;
        };
        if !self.emitted.insert(notice) {
            return;
        }
        let message = match notice {
            AiNoticeKind::AutoFallbackToDirect => {
                "codewiki: AI auto routing could not use the daemon; falling back to Direct generation"
            }
            AiNoticeKind::AutoFallbackToOff => {
                "codewiki: AI auto routing found no daemon or usable Direct config; writing structural docs"
            }
            AiNoticeKind::NoGenerator => {
                "codewiki: AI generation was requested but no usable generator is configured; writing structural docs"
            }
            AiNoticeKind::GenerationFailed => {
                "codewiki: AI generation failed; affected pages record degraded status"
            }
        };
        eprintln!("{message}");
    }
}

/// Repair-only entry for `codewiki --repair-citations`: re-anchors every
/// generated page's `[file:line]` citations against the current index and
/// rewrites only the pages whose citations changed. No generation, no AI/LLM
/// calls. Loads the full visible symbol set (like [`run`]) so a citation to any
/// indexed file can resolve, then prints the [`super::CitationRepairSummary`].
pub fn run_repair(ctx: &Context, out: Option<String>, format: Format) -> anyhow::Result<()> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let files = visibility::visible_tree(&mut conn, ctx)?
        .into_iter()
        .map(|file| file.file_path)
        .collect::<Vec<_>>();
    let symbols = visibility::visible_symbols_for_files(&mut conn, ctx, &files)?;
    let out_dir = out.unwrap_or_else(|| DEFAULT_OUT_DIR.to_string());
    let summary = super::repair_citations(Path::new(&out_dir), &symbols)?;
    match format {
        Format::Json => output::print_json(&summary),
        Format::Text => output::print_text(&format!(
            "scanned {} pages; repaired {} pages, {} citations; {} unresolved",
            summary.pages_scanned,
            summary.pages_repaired,
            summary.citations_repaired,
            summary.citations_unresolved,
        )),
    }?;
    Ok(())
}

pub(crate) fn validate_edge_limit(edge_limit: usize) -> anyhow::Result<()> {
    if (1..=MAX_EDGE_LIMIT).contains(&edge_limit) {
        return Ok(());
    }
    anyhow::bail!("codewiki --edge-limit must be between 1 and {MAX_EDGE_LIMIT}, got {edge_limit}")
}

/// Repo-relative paths git reports changed between `since_ref` and the working
/// tree — the change set that drives `--since` incremental regeneration (Leaf H,
/// #893). An invalid ref or a missing git binary is surfaced as an error rather
/// than silently falling back to a full scan, so a typo'd `--since` fails loudly.
pub(crate) fn git_changed_files(
    project_root: &Path,
    since_ref: &str,
) -> anyhow::Result<std::collections::BTreeSet<String>> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(project_root)
        .args(["diff", "--name-only", "--relative", since_ref])
        .output()
        .map_err(|err| anyhow::anyhow!("failed to run git diff for --since {since_ref}: {err}"))?;
    if !output.status.success() {
        anyhow::bail!(
            "git diff --name-only --relative {since_ref} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(str::to_string)
        .collect())
}

/// codewiki documents code and structured config — any file the indexer
/// recognizes as an AST or json/yaml language. Content-only files (markdown,
/// plain text, license/lock files) are gwiki's domain, so codewiki skips them.
fn documents_file(file_path: &str) -> bool {
    crate::index::languages::detect_language(file_path).is_some()
}

/// Whether codewiki should emit a file doc for `file_path`. Content-only files
/// are skipped unless the caller opts back in with `--include-docs`.
pub(crate) fn should_document_file(file_path: &str, include_docs: bool) -> bool {
    include_docs || documents_file(file_path)
}

pub(crate) fn load_symbols_for_codewiki(
    files: &[String],
    progress: &mut CodewikiProgress,
    mut load_symbols: impl FnMut(&[String]) -> anyhow::Result<Vec<Symbol>>,
) -> anyhow::Result<Vec<Symbol>> {
    progress.emit(format!("loading symbols for {} files", files.len()));
    load_symbols(files)
}

/// Loads each file's first indexed content chunk (`chunk_index = 0`) from the
/// hub. Overlay scopes prefer overlay rows and fall back to the parent
/// project for files the overlay has not re-indexed.
fn load_leading_chunks(
    conn: &mut postgres::Client,
    ctx: &Context,
    files: &[String],
) -> anyhow::Result<BTreeMap<String, LeadingChunk>> {
    let mut chunks = BTreeMap::new();
    if files.is_empty() {
        return Ok(chunks);
    }
    let project_ids = match &ctx.index_scope {
        config::ProjectIndexScope::Single => vec![ctx.project_id.clone()],
        config::ProjectIndexScope::Overlay {
            overlay_project_id,
            parent_project_id,
            ..
        } => vec![overlay_project_id.clone(), parent_project_id.clone()],
    };
    for project_id in project_ids {
        let rows = conn.query(
            "SELECT file_path,
                    line_start::BIGINT AS line_start,
                    line_end::BIGINT AS line_end,
                    content
             FROM code_content_chunks
             WHERE project_id = $1 AND file_path = ANY($2) AND chunk_index = 0",
            &[&project_id, &files],
        )?;
        for row in rows {
            let file_path: String = row.get("file_path");
            if chunks.contains_key(&file_path) {
                continue;
            }
            let line_start: i64 = row.get("line_start");
            let line_end: i64 = row.get("line_end");
            let content: String = row.get("content");
            chunks.insert(
                file_path,
                LeadingChunk {
                    content,
                    line_start: usize::try_from(line_start).unwrap_or(0),
                    line_end: usize::try_from(line_end).unwrap_or(0),
                },
            );
        }
    }
    Ok(chunks)
}
