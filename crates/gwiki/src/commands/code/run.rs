use std::collections::BTreeMap;
use std::num::NonZeroUsize;
use std::path::Path;

use gobby_code::codewiki_facts::{FileId, ScopeSelector};
use gobby_core::config::AiRouting;

use crate::commands::code::Symbol;

use super::runtime::{self as output, CodeEngineRuntime};
use super::types::ai_outcome_for_doc;
use super::{
    AiGenerationSettings, BuiltDoc, CodewikiAiOptions, CodewikiProgress, CodewikiPublication,
    CodewikiRunSummary, CommitStamp, DocPruneScope, DocSink, LeadingChunk, MAX_EDGE_LIMIT,
    PromptTier, PublicationFingerprint, ReusePlan, TextGenerator, TextVerifier,
    build_codewiki_changes_doc, build_codewiki_index_snapshot, generation, io, is_core_file,
    read_ownership_meta, resolve_tool_loop_dump_dir, write_ownership_meta,
};

mod ai;
mod finalization;
mod preparation;

use ai::ResolvedAiRun;
use finalization::{FinalizeRun, RunCounts, finalize_run};
use preparation::prepare_run;

// Each parameter maps one-to-one to a Code command flag.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_summary(
    ctx: &CodeEngineRuntime,
    out: Option<String>,
    scope_args: Vec<String>,
    complete_scope: bool,
    ai: CodewikiAiOptions,
    edge_limit: usize,
    include_docs: bool,
    since: Option<String>,
    max_workers: usize,
    verbose: bool,
) -> anyhow::Result<CodewikiRunSummary> {
    validate_edge_limit(edge_limit)?;
    let ai_depth = ai.depth;
    let verify_scope = ai.verify_scope;
    let mut progress = CodewikiProgress::stderr((verbose || ctx.verbose) && !ctx.quiet);
    let prepared = prepare_run(
        ctx,
        &scope_args,
        complete_scope,
        include_docs,
        edge_limit,
        &mut progress,
    )?;
    let ResolvedAiRun {
        generator: shared_generator,
        verifier: shared_verifier,
        mut tool_loop_generator,
        ai_outcome,
        aggregate_ai_outcome,
        ai_enabled,
        ai_mode,
        mut notices,
    } = ResolvedAiRun::resolve(ctx, &ai, prepared.input.graph_availability)?;
    let file_workers = NonZeroUsize::new(max_workers)
        .filter(|workers| workers.get() > 1)
        .and_then(|workers| {
            shared_generator
                .as_deref()
                .map(|generate| generation::FileGenerationWorkers {
                    workers,
                    generate,
                    verify: shared_verifier.as_deref(),
                })
        });
    let out_path = output::resolve_output_path(&ctx.project_root, out.as_deref());
    let out_dir = out_path.display().to_string();
    // Destructive-downgrade guard (#17776): `--ai auto` that resolves NO
    // generator on either lane must not rewrite a previously AI-generated
    // vault as structural docs — the #17530 settings invalidation would
    // regenerate every page and clobber the AI prose (a transient daemon
    // outage once erased a full vault this way). Explicit `--ai off`
    // (route Off without the auto fallback) keeps the intentional
    // structural-rewrite path.
    if shared_generator.is_none()
        && tool_loop_generator.is_none()
        && ai_outcome.route == AiRouting::Off
        && ai_outcome.fallback
    {
        let previous = super::io::read_codewiki_meta(&out_path)?;
        let ai_pages = ai_generated_page_count(&previous);
        if ai_pages > 0 {
            anyhow::bail!(
                "--ai auto found no usable AI route, but {ai_pages} existing pages in {} \
                 were AI-generated; refusing to rewrite them as structural docs. Fix the \
                 AI route (is the daemon running?) or pass --ai off to downgrade \
                 intentionally.",
                out_path.display(),
            );
        }
    }
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
    if prepared.doc_scope.is_unscoped() {
        progress.emit("reading metadata and hashing snapshot");
    } else {
        progress.emit("reading metadata for scoped write");
    }
    let staging_snapshot = build_codewiki_index_snapshot(&ctx.project_root, &prepared.input)?;
    let index_snapshot = prepared
        .doc_scope
        .is_unscoped()
        .then(|| staging_snapshot.clone());
    // The requested generation settings are part of the reuse comparison
    // (#17530): flags like `--ai-aggregate-candidate` change what a page would
    // say without changing any source hash. With AI off they shape nothing, so
    // they are not recorded and cannot spuriously invalidate structural docs.
    let ai_settings = if ai_mode == "off" {
        AiGenerationSettings::default()
    } else {
        AiGenerationSettings::from_options(&ai)
    };
    let fingerprint = PublicationFingerprint::from_run(
        &ctx.project_root,
        &prepared.input.files,
        ai_mode,
        &ai_settings,
        ai_outcome,
        aggregate_ai_outcome,
        &prepared.scopes,
        since_changed.as_ref(),
        &staging_snapshot,
    )?;
    let publication = CodewikiPublication::prepare(&out_path, &fingerprint)?;
    let stage_path = publication.stage_out().to_path_buf();
    let effective_since_changed = if publication.requires_full_hash_scan() {
        None
    } else {
        since_changed
    };
    let previous_meta = if prepared.doc_scope.is_unscoped() {
        Some(io::read_codewiki_meta(&stage_path)?)
    } else {
        None
    };
    let mut ownership_meta = if prepared.doc_scope.is_unscoped() {
        Some(read_ownership_meta(&stage_path)?)
    } else {
        None
    };
    let mut reuse_plan = ReusePlan::load_with_since_and_ai_outcome(
        &ctx.project_root,
        &stage_path,
        ai_mode,
        effective_since_changed.clone(),
        ai_outcome,
    )?
    .with_ai_settings(ai_settings.clone());
    let mut sink = DocSink::open_with_prune_scope(
        &ctx.project_root,
        &stage_path,
        ai_mode,
        prepared.doc_scope.clone(),
    )?
    .with_ai_outcome(ai_outcome)
    .with_commit_stamp(prepared.commit_stamp.clone())
    .with_ai_settings(ai_settings)
    .with_since(effective_since_changed);
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
        let write_outcome = ai_outcome_for_doc(&doc.path, ai_outcome, aggregate_ai_outcome);
        sink.persist_with_ai_outcome(&doc, write_outcome)?;
        Ok(())
    };
    // Tool-loop / nav-plan failure dumps land under the live output's `_meta/`
    // — which the doc walkers never visit — instead of among the generated
    // pages; `GOBBY_CODEWIKI_TOOL_LOOP_DUMP_DIR` redirects them to a scratch
    // directory (#17533). Resolved here once so library code never reads the
    // environment.
    let tool_loop_dump_dir = resolve_tool_loop_dump_dir(
        std::env::var("GOBBY_CODEWIKI_TOOL_LOOP_DUMP_DIR")
            .ok()
            .as_deref(),
        &out_path,
    );
    // Serial `FnMut` adapters over the shared thread-safe callables; the file
    // worker pool (`file_workers` above) shares the originals across threads
    // (#17532).
    let mut sequential_generator = shared_generator.as_deref().map(|generator| {
        move |prompt: &str, system: &str, tier: PromptTier| generator(prompt, system, tier)
    });
    let mut sequential_verifier = shared_verifier
        .as_deref()
        .map(|verifier| move |prompt: &str, system: &str| verifier(prompt, system));
    let diagram_stats = generation::generate_hierarchical_docs(
        &prepared.input,
        generation::GenerateDocsOptions {
            ownership: ownership_meta
                .as_mut()
                .map(|meta| (ctx.project_root.as_path(), ctx.project_id.as_str(), meta)),
            system_model: Some(&prepared.system_model),
            feature_catalog: prepared.feature_catalog.as_ref(),
            audit: Some(&prepared.audit_context),
            generate: sequential_generator
                .as_mut()
                .map(|generator| generator as &mut TextGenerator<'_>),
            tool_loop: tool_loop_generator.as_deref_mut(),
            verify: sequential_verifier
                .as_mut()
                .map(|verifier| verifier as &mut TextVerifier<'_>),
            ai_depth,
            verify_scope,
            aggregate_ai_outcome,
            reuse: Some(&mut reuse_plan),
            progress: Some(&mut progress),
            doc_scope: Some(&prepared.doc_scope),
            tool_loop_dump_dir: Some(&tool_loop_dump_dir),
            file_workers,
        },
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
        write_ownership_meta(&stage_path, ownership_meta)?;
    }
    let symbol_count = prepared
        .input
        .symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .count();
    progress.emit("publishing completed codewiki stage");
    finalize_run(FinalizeRun {
        ctx,
        out_dir,
        publication,
        stage_path: &stage_path,
        sink,
        index_snapshot,
        doc_scope: prepared.doc_scope,
        system_model: &prepared.system_model,
        commit_stamp: prepared.commit_stamp.as_ref(),
        diagram_stats,
        counts: RunCounts {
            generated_pages,
            files: file_count,
            modules: module_count,
            symbols: symbol_count,
        },
        ai_enabled,
        notices: &mut notices,
    })
}

pub(crate) fn run_summary_text(summary: &CodewikiRunSummary, unscoped: bool) -> String {
    if unscoped {
        format!(
            "wrote {} file docs, {} module docs, and repo.md to {}",
            summary.files, summary.modules, summary.out_dir
        )
    } else {
        format!(
            "wrote {} scoped file docs and {} scoped module docs to {}",
            summary.files, summary.modules, summary.out_dir
        )
    }
}

fn codewiki_doc_scope(scopes: &[String], complete_scope: bool) -> DocPruneScope {
    if complete_scope {
        DocPruneScope::unscoped()
    } else {
        DocPruneScope::from_scopes(scopes)
    }
}

/// Repair-only entry for `gwiki code --repair-citations`: re-anchors every
/// generated page's `[file:line]` citations against the current index and
/// rewrites only the pages whose citations changed. No generation, no AI/LLM
/// calls. Loads the full visible symbol set used by generation so a citation to
/// any indexed file can resolve.
pub(crate) fn repair_summary(
    ctx: &CodeEngineRuntime,
    out: Option<String>,
) -> anyhow::Result<super::CitationRepairSummary> {
    let files = ctx
        .facts
        .scoped_files(&ScopeSelector::all())?
        .into_iter()
        .map(|file| file.path)
        .collect::<Vec<_>>();
    let file_ids = files.iter().cloned().map(FileId::new).collect::<Vec<_>>();
    let symbols = ctx
        .facts
        .symbols_in(&file_ids)?
        .into_iter()
        .map(|symbol| Symbol::from_fact(symbol, &ctx.project_id))
        .collect::<Vec<_>>();
    let out_path = output::resolve_output_path(&ctx.project_root, out.as_deref());
    super::repair_citations(&out_path, &symbols)
}

pub(crate) fn repair_summary_text(summary: &super::CitationRepairSummary) -> String {
    format!(
        "scanned {} pages; repaired {} pages, {} citations; {} unresolved",
        summary.pages_scanned,
        summary.pages_repaired,
        summary.citations_repaired,
        summary.citations_unresolved,
    )
}

pub(crate) fn validate_edge_limit(edge_limit: usize) -> anyhow::Result<()> {
    if (1..=MAX_EDGE_LIMIT).contains(&edge_limit) {
        return Ok(());
    }
    anyhow::bail!("codewiki --edge-limit must be between 1 and {MAX_EDGE_LIMIT}, got {edge_limit}")
}

/// Pages in the previous run's meta that were written by an AI route
/// (#17776). A nonzero count blocks a no-generator `--ai auto` run from
/// rewriting the vault structurally; pages already structural (`off`) or
/// pre-AI entries never block.
pub(crate) fn ai_generated_page_count(meta: &super::types::CodewikiMeta) -> usize {
    meta.docs
        .values()
        .filter(|doc| matches!(doc.ai_route.as_str(), "daemon" | "direct"))
        .count()
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
        .args([
            "diff",
            "--name-only",
            "--relative",
            "--end-of-options",
            since_ref,
        ])
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

pub(crate) fn capture_commit_stamp(project_root: &Path) -> Option<CommitStamp> {
    let revision = std::process::Command::new("git")
        .arg("-C")
        .arg(project_root)
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()?;
    if !revision.status.success() {
        return None;
    }
    let sha = String::from_utf8(revision.stdout).ok()?.trim().to_string();
    if sha.is_empty() {
        return None;
    }

    let status = std::process::Command::new("git")
        .arg("-C")
        .arg(project_root)
        .args(["status", "--porcelain", "--untracked-files=no"])
        .output()
        .ok()?;
    if !status.status.success() {
        return None;
    }
    Some(CommitStamp {
        sha,
        dirty: !status.stdout.is_empty(),
    })
}

/// codewiki documents code and structured config — any file the indexer
/// recognizes as an AST or json/yaml language. Content-only files (markdown,
/// plain text, license/lock files) are gwiki's domain, so codewiki skips them.
fn documents_file(file_path: &str) -> bool {
    output::is_indexed_language(file_path)
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
    ctx: &CodeEngineRuntime,
    files: &[String],
) -> anyhow::Result<BTreeMap<String, LeadingChunk>> {
    let file_ids = files.iter().cloned().map(FileId::new).collect::<Vec<_>>();
    Ok(ctx
        .facts
        .leading_chunks(&file_ids)?
        .into_iter()
        .map(|chunk| {
            (
                chunk.file.as_str().to_owned(),
                LeadingChunk {
                    content: chunk.content,
                    line_start: chunk.line_start,
                    line_end: chunk.line_end,
                },
            )
        })
        .collect())
}

#[cfg(test)]
mod scope_tests {
    use super::*;

    #[test]
    fn complete_scope_switches_to_global_generation_and_pruning() {
        let scopes = vec!["src".to_string()];

        let partial = codewiki_doc_scope(&scopes, false);
        assert!(!partial.is_unscoped());
        assert!(partial.includes_file("src/lib.rs"));
        assert!(!partial.includes_file("tools/helper.rs"));

        let complete = codewiki_doc_scope(&scopes, true);
        assert!(complete.is_unscoped());
        assert!(complete.includes_file("tools/helper.rs"));
    }

    #[test]
    fn capture_commit_stamp_detects_dirty_worktrees_and_non_git_roots() {
        let project = tempfile::tempdir().expect("project tempdir");
        let root = project.path();
        assert_eq!(capture_commit_stamp(root), None);

        let git = |args: &[&str]| {
            let status = isolated_test_git(root)
                .args(args)
                .status()
                .expect("run git");
            assert!(status.success(), "git {args:?} failed");
        };
        git(&["init", "-q", "--object-format=sha1", "--template="]);
        git(&["config", "user.email", "test@example.com"]);
        git(&["config", "user.name", "Test"]);
        git(&["config", "commit.gpgSign", "false"]);
        std::fs::write(root.join("tracked.rs"), "fn tracked() {}\n").expect("write source");
        git(&["add", "tracked.rs"]);
        git(&["commit", "-q", "-m", "base"]);

        let clean = capture_commit_stamp(root).expect("clean commit stamp");
        assert_eq!(clean.sha.len(), 40);
        assert!(clean.sha.bytes().all(|byte| byte.is_ascii_hexdigit()));
        assert!(!clean.dirty);

        std::fs::write(root.join("tracked.rs"), "fn tracked() { let _ = 1; }\n")
            .expect("modify source");
        let dirty = capture_commit_stamp(root).expect("dirty commit stamp");
        assert_eq!(dirty.sha, clean.sha);
        assert!(dirty.dirty);
    }

    fn isolated_test_git(root: &std::path::Path) -> std::process::Command {
        let mut command = std::process::Command::new("git");
        command
            .arg("-C")
            .arg(root)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", root.join("global.gitconfig"))
            .env("GIT_CONFIG_COUNT", "0")
            .env("HOME", root);
        for variable in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_TEMPLATE_DIR",
        ] {
            command.env_remove(variable);
        }
        command
    }
}
