use std::any::Any;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};

use crate::commands::code::Symbol;

use super::FileGenerationWorkers;
use crate::commands::code::{
    AiDepth, AuditContext, BuiltDoc, CodewikiInput, CodewikiProgress, DocPruneScope, FileDoc,
    FileDocPosition, PromptTier, RelationshipFacts, ReusePlan, TextGenerator, TextVerifier,
    build_file_doc, cluster_file_modules, file_doc_path, file_module_link_key, is_core_file,
    module_for_file, relationship_facts_for_file, render_file_doc, resolve_file_reuse,
};

pub(super) struct FileGenerationOutput {
    pub files: Vec<String>,
    pub file_modules: HashMap<String, String>,
    pub file_docs: Vec<FileDoc>,
}

struct FileJob {
    index: usize,
    file: String,
    module: String,
    symbols: Vec<Symbol>,
    relationships: RelationshipFacts,
    neighbors: BTreeSet<String>,
    reused: Option<(String, String)>,
}

enum WorkerEvent {
    Progress(String),
    Done(usize, Box<FileDoc>, BTreeSet<String>),
    Failed(String),
}

#[expect(
    clippy::too_many_arguments,
    reason = "generation inputs are explicit pipeline state"
)]
pub(super) fn generate_file_docs(
    input: &CodewikiInput,
    doc_scope: &DocPruneScope,
    file_workers: Option<FileGenerationWorkers<'_>>,
    audit: Option<&AuditContext>,
    reuse: &mut Option<&mut ReusePlan>,
    verify_leaves: bool,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<FileGenerationOutput> {
    let mut files = input
        .files
        .iter()
        .filter(|file| is_core_file(file) && doc_scope.includes_file(file))
        .cloned()
        .collect::<BTreeSet<_>>();
    for symbol in &input.symbols {
        if is_core_file(&symbol.file_path) && doc_scope.includes_file(&symbol.file_path) {
            files.insert(symbol.file_path.clone());
        }
    }
    let files = files.into_iter().collect::<Vec<_>>();

    let mut symbols_by_file: BTreeMap<String, Vec<Symbol>> = BTreeMap::new();
    for symbol in &input.symbols {
        if !is_core_file(&symbol.file_path) || !doc_scope.includes_file(&symbol.file_path) {
            continue;
        }
        symbols_by_file
            .entry(symbol.file_path.clone())
            .or_default()
            .push(symbol.clone());
    }
    for symbols in symbols_by_file.values_mut() {
        symbols.sort_by_key(|symbol| (symbol.line_start, symbol.byte_start, symbol.name.clone()));
    }

    let file_modules = cluster_file_modules(&files, &symbols_by_file, &input.graph_edges);
    let symbols_by_id = input
        .symbols
        .iter()
        .map(|symbol| (symbol.id.as_str(), symbol))
        .collect::<HashMap<&str, &Symbol>>();
    let file_verb = if ai_depth.includes_files() {
        "generating"
    } else {
        "building"
    };
    progress.emit(format!("{file_verb} file docs for {} files", files.len()));
    let mut file_docs = Vec::with_capacity(files.len());
    match file_workers {
        None => generate_file_docs_serial(
            &files,
            &mut symbols_by_file,
            &file_modules,
            &symbols_by_id,
            input,
            audit,
            reuse,
            verify_leaves,
            ai_depth,
            progress,
            generate,
            verify,
            emit,
            &mut file_docs,
        )?,
        Some(pool) => generate_file_docs_pooled(
            pool,
            &files,
            &mut symbols_by_file,
            &file_modules,
            &symbols_by_id,
            input,
            audit,
            reuse,
            verify_leaves,
            ai_depth,
            progress,
            emit,
            &mut file_docs,
        )?,
    }
    Ok(FileGenerationOutput {
        files,
        file_modules,
        file_docs,
    })
}

#[expect(
    clippy::too_many_arguments,
    reason = "serial and pooled paths share explicit state"
)]
fn generate_file_docs_serial(
    files: &[String],
    symbols_by_file: &mut BTreeMap<String, Vec<Symbol>>,
    file_modules: &HashMap<String, String>,
    symbols_by_id: &HashMap<&str, &Symbol>,
    input: &CodewikiInput,
    audit: Option<&AuditContext>,
    reuse: &mut Option<&mut ReusePlan>,
    verify_leaves: bool,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    generate: &mut Option<&mut TextGenerator<'_>>,
    verify: &mut Option<&mut TextVerifier<'_>>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
    file_docs: &mut Vec<FileDoc>,
) -> anyhow::Result<()> {
    for (index, file) in files.iter().enumerate() {
        let job = prepare_file_job(
            index,
            file,
            symbols_by_file,
            file_modules,
            symbols_by_id,
            &input.graph_edges,
            reuse,
        );
        let FileJob {
            index,
            file,
            module,
            symbols,
            relationships,
            neighbors,
            reused,
        } = job;
        let mut no_verify: Option<&mut TextVerifier<'_>> = None;
        let leaf_verify = if verify_leaves {
            &mut *verify
        } else {
            &mut no_verify
        };
        let file_doc = build_file_doc(
            &file,
            module,
            symbols,
            input.leading_chunks.get(&file),
            &relationships,
            audit.map(|audit| &audit.deprecations),
            audit.map(|audit| &audit.tests),
            reused,
            generate,
            leaf_verify,
            ai_depth,
            &mut |message| progress.emit(message),
            FileDocPosition {
                index: index + 1,
                total: files.len(),
            },
        );
        emit_file_doc(&file_doc, neighbors, emit)?;
        file_docs.push(file_doc);
    }
    Ok(())
}

fn prepare_file_job(
    index: usize,
    file: &str,
    symbols_by_file: &mut BTreeMap<String, Vec<Symbol>>,
    file_modules: &HashMap<String, String>,
    symbols_by_id: &HashMap<&str, &Symbol>,
    graph_edges: &[crate::commands::code::CodewikiGraphEdge],
    reuse: &mut Option<&mut ReusePlan>,
) -> FileJob {
    let symbols = symbols_by_file.remove(file).unwrap_or_default();
    let relationships = {
        let file_symbol_ids = symbols
            .iter()
            .map(|symbol| symbol.id.as_str())
            .collect::<HashSet<&str>>();
        relationship_facts_for_file(file, &file_symbol_ids, symbols_by_id, graph_edges)
    };
    let module = file_modules
        .get(file)
        .cloned()
        .unwrap_or_else(|| module_for_file(file));
    let neighbors = relationships.neighbor_files(file);
    let reused = resolve_file_reuse(reuse, file, &module, &neighbors);
    FileJob {
        index,
        file: file.to_string(),
        module,
        symbols,
        relationships,
        neighbors,
        reused,
    }
}

fn emit_file_doc(
    file_doc: &FileDoc,
    neighbors: BTreeSet<String>,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
) -> anyhow::Result<()> {
    emit(
        BuiltDoc {
            path: file_doc_path(&file_doc.path),
            content: file_doc
                .reused_page
                .clone()
                .unwrap_or_else(|| render_file_doc(file_doc)),
            degraded: file_doc.degraded,
            summary: Some(file_doc.summary.clone()),
            neighbors: BTreeSet::new(),
            invalidation_key: Some(file_module_link_key(&file_doc.module)),
            invalidation_key_requires_sources: true,
        }
        .with_neighbors(neighbors),
    )
}

#[expect(
    clippy::too_many_arguments,
    reason = "pooled generation mirrors the serial pipeline"
)]
fn generate_file_docs_pooled(
    pool: FileGenerationWorkers<'_>,
    files: &[String],
    symbols_by_file: &mut BTreeMap<String, Vec<Symbol>>,
    file_modules: &HashMap<String, String>,
    symbols_by_id: &HashMap<&str, &Symbol>,
    input: &CodewikiInput,
    audit: Option<&AuditContext>,
    reuse: &mut Option<&mut ReusePlan>,
    verify_leaves: bool,
    ai_depth: AiDepth,
    progress: &mut CodewikiProgress,
    emit: &mut dyn FnMut(BuiltDoc) -> anyhow::Result<()>,
    file_docs: &mut Vec<FileDoc>,
) -> anyhow::Result<()> {
    let file_total = files.len();
    if file_total == 0 {
        return Ok(());
    }
    let worker_count = pool.workers.get().min(file_total);
    let dispatch_limit = (worker_count + 2).min(file_total);
    let (job_tx, job_rx) = mpsc::channel::<FileJob>();
    let job_rx = Mutex::new(job_rx);
    let (event_tx, event_rx) = mpsc::channel();
    let live_workers = Arc::new(AtomicUsize::new(worker_count));
    let mut next_dispatch = 0_usize;
    for file in files.iter().take(dispatch_limit) {
        job_tx.send(prepare_file_job(
            next_dispatch,
            file,
            symbols_by_file,
            file_modules,
            symbols_by_id,
            &input.graph_edges,
            reuse,
        ))?;
        next_dispatch += 1;
    }
    let mut job_tx = Some(job_tx);
    if next_dispatch == file_total {
        job_tx.take();
    }
    let deprecations = audit.map(|audit| &audit.deprecations);
    let tests = audit.map(|audit| &audit.tests);
    std::thread::scope(|scope| -> anyhow::Result<()> {
        for _ in 0..worker_count {
            let event_tx = event_tx.clone();
            let job_rx = &job_rx;
            let live_workers = Arc::clone(&live_workers);
            scope.spawn(move || {
                let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    loop {
                        let job = job_rx
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner())
                            .recv();
                        let Ok(FileJob {
                            index,
                            file,
                            module,
                            symbols,
                            relationships,
                            neighbors,
                            reused,
                        }) = job
                        else {
                            break;
                        };
                        let mut worker_generate = |prompt: &str, system: &str, tier: PromptTier| {
                            (pool.generate)(prompt, system, tier)
                        };
                        let mut generate: Option<&mut TextGenerator<'_>> =
                            Some(&mut worker_generate);
                        let mut worker_verify = pool
                            .verify
                            .filter(|_| verify_leaves)
                            .map(|verify| move |prompt: &str, system: &str| verify(prompt, system));
                        let mut leaf_verify: Option<&mut TextVerifier<'_>> = worker_verify
                            .as_mut()
                            .map(|verify| verify as &mut TextVerifier<'_>);
                        let mut progress_sink = |message: String| {
                            let _ = event_tx.send(WorkerEvent::Progress(message));
                        };
                        let file_doc = build_file_doc(
                            &file,
                            module,
                            symbols,
                            input.leading_chunks.get(&file),
                            &relationships,
                            deprecations,
                            tests,
                            reused,
                            &mut generate,
                            &mut leaf_verify,
                            ai_depth,
                            &mut progress_sink,
                            FileDocPosition {
                                index: index + 1,
                                total: file_total,
                            },
                        );
                        if event_tx
                            .send(WorkerEvent::Done(index, Box::new(file_doc), neighbors))
                            .is_err()
                        {
                            break;
                        }
                    }
                }));
                live_workers.fetch_sub(1, Ordering::AcqRel);
                if let Err(payload) = outcome {
                    let _ = event_tx.send(WorkerEvent::Failed(panic_message(payload.as_ref())));
                }
            });
        }
        drop(event_tx);
        let mut completed: BTreeMap<usize, (FileDoc, BTreeSet<String>)> = BTreeMap::new();
        let mut next_emit = 0_usize;
        let mut emit_error = None;
        let mut worker_error = None;
        while let Ok(event) = event_rx.recv() {
            match event {
                WorkerEvent::Progress(message) => progress.emit(message),
                WorkerEvent::Failed(message) => {
                    worker_error.get_or_insert_with(|| {
                        anyhow::anyhow!("file generation worker failed: {message}")
                    });
                    job_tx.take();
                }
                WorkerEvent::Done(index, file_doc, neighbors) => {
                    completed.insert(index, (*file_doc, neighbors));
                    if emit_error.is_some() || worker_error.is_some() {
                        continue;
                    }
                    while let Some((file_doc, neighbors)) = completed.remove(&next_emit) {
                        if let Err(error) = emit_file_doc(&file_doc, neighbors, emit) {
                            emit_error = Some(error);
                            job_tx.take();
                            break;
                        }
                        file_docs.push(file_doc);
                        next_emit += 1;
                        if next_dispatch < file_total {
                            let file = &files[next_dispatch];
                            let job = prepare_file_job(
                                next_dispatch,
                                file,
                                symbols_by_file,
                                file_modules,
                                symbols_by_id,
                                &input.graph_edges,
                                reuse,
                            );
                            let sender = job_tx.take().ok_or_else(|| {
                                anyhow::anyhow!("file generation queue closed while jobs remain")
                            })?;
                            sender.send(job).map_err(|_| {
                                anyhow::anyhow!("all file generation workers exited")
                            })?;
                            next_dispatch += 1;
                            if next_dispatch < file_total {
                                job_tx = Some(sender);
                            }
                        }
                    }
                }
            }
        }
        match emit_error {
            Some(error) => Err(error),
            None => match worker_error {
                Some(error) => Err(error),
                None if next_emit == file_total => Ok(()),
                None => anyhow::bail!(
                    "file generation stopped after {next_emit} of {file_total} files; {} workers remain",
                    live_workers.load(Ordering::Acquire)
                ),
            },
        }
    })
}

fn panic_message(payload: &(dyn Any + Send)) -> String {
    payload
        .downcast_ref::<&str>()
        .map(|message| (*message).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "unknown panic payload".to_string())
}
