//! Bounded file-page worker pool (#17532): the pooled path must emit a doc
//! set byte-identical to the serial path, bound its in-flight generation
//! calls to the pool width, and preserve zero-call reuse.

use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicUsize, Ordering};

use super::support::*;
use super::*;
use crate::commands::code::runtime::hasher;

/// Multi-file input large enough that a pool genuinely interleaves work.
fn multi_file_input(file_count: usize) -> CodewikiInput {
    let mut files = Vec::new();
    let mut symbols = Vec::new();
    for index in 0..file_count {
        let file = format!("src/f{index}.rs");
        symbols.push(test_symbol(
            &file,
            &format!("Type{index}"),
            "class",
            1,
            &format!("pub struct Type{index};"),
        ));
        symbols.push(test_symbol(
            &file,
            &format!("run{index}"),
            "function",
            4,
            &format!("pub fn run{index}()"),
        ));
        files.push(file);
    }
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files,
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols,
    }
}

/// Pure function of its inputs, so serial and pooled runs asking for the same
/// page must produce the same bytes. The curated/handbook system prompts get
/// their parseable fixtures; everything else gets prompt-keyed prose.
fn deterministic_generation(prompt: &str, system: &str, tier: PromptTier) -> Option<String> {
    if system == prompts::CURATED_NAVIGATION_SYSTEM {
        return Some(test_curated_navigation_json());
    }
    if system == prompts::CONCEPT_PAGE_SYSTEM {
        return Some(test_concept_handbook_body());
    }
    if system == prompts::NARRATIVE_PAGE_SYSTEM {
        return Some(test_narrative_handbook_body());
    }
    Some(format!(
        "Deterministic prose for {tier:?} prompt {}.",
        hasher::content_hash(prompt.as_bytes())
    ))
}

fn collect_serial(input: &CodewikiInput) -> Vec<(String, String)> {
    let mut serial_generate = deterministic_generation;
    collect_doc_pairs(
        input,
        GenerateDocsOptions {
            generate: Some(&mut serial_generate),
            ..Default::default()
        },
    )
}

fn collect_pooled(input: &CodewikiInput, workers: usize) -> Vec<(String, String)> {
    let pooled_generate = |prompt: &str, system: &str, tier: PromptTier| {
        deterministic_generation(prompt, system, tier)
    };
    let mut serial_generate = deterministic_generation;
    collect_doc_pairs(
        input,
        GenerateDocsOptions {
            generate: Some(&mut serial_generate),
            file_workers: Some(FileGenerationWorkers {
                workers: NonZeroUsize::new(workers).expect("worker count"),
                generate: &pooled_generate,
                verify: None,
            }),
            ..Default::default()
        },
    )
}

#[test]
fn pooled_output_is_byte_identical_to_serial() {
    let input = multi_file_input(8);
    let serial = collect_serial(&input);
    let pooled = collect_pooled(&input, 4);
    // Full equality covers page bytes AND emit order: the pool must buffer
    // out-of-order completions and write strictly in file order.
    assert_eq!(serial, pooled);
    assert!(
        serial
            .iter()
            .filter(|(path, _)| path.starts_with("code/files/"))
            .count()
            >= 8,
        "every input file must emit a file page"
    );
}

#[test]
fn single_worker_pool_matches_serial() {
    // `--max-workers 1` maps to `file_workers: None` in run.rs; a pool of one
    // must still be equivalent for callers that construct it directly.
    let input = multi_file_input(4);
    assert_eq!(collect_serial(&input), collect_pooled(&input, 1));
}

#[test]
fn pool_bounds_concurrent_generation_calls() {
    let input = multi_file_input(8);
    let in_flight = AtomicUsize::new(0);
    let max_in_flight = AtomicUsize::new(0);
    let arrivals = AtomicUsize::new(0);
    let rendezvous = std::sync::Barrier::new(3);
    let pooled_generate = |prompt: &str, system: &str, tier: PromptTier| {
        let now = in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        max_in_flight.fetch_max(now, Ordering::SeqCst);
        if arrivals.fetch_add(1, Ordering::SeqCst) < 3 {
            rendezvous.wait();
        }
        in_flight.fetch_sub(1, Ordering::SeqCst);
        deterministic_generation(prompt, system, tier)
    };
    let mut serial_generate = deterministic_generation;
    let docs = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut serial_generate),
            file_workers: Some(FileGenerationWorkers {
                workers: NonZeroUsize::new(3).expect("worker count"),
                generate: &pooled_generate,
                verify: None,
            }),
            ..Default::default()
        },
    );
    assert!(!docs.is_empty());
    let peak = max_in_flight.load(Ordering::SeqCst);
    assert_eq!(peak, 3, "three workers must rendezvous concurrently");
}

#[test]
fn pooled_reuse_makes_zero_generation_calls() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("source dir");
    let input = multi_file_input(4);
    for (index, file) in input.files.iter().enumerate() {
        std::fs::write(
            project.path().join(file),
            format!("pub struct Type{index};\n\npub fn run{index}() {{}}\n"),
        )
        .expect("write source");
    }
    let out_dir = project.path().join("codewiki");

    let mut first_generator = deterministic_generation;
    let first = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut first_generator),
            ..Default::default()
        },
    );
    write_incremental_doc_set_with_snapshot(
        project.path(),
        &out_dir,
        &first,
        None,
        "symbols",
        DocPruneScope::unscoped(),
    )
    .expect("first write");

    let calls = AtomicUsize::new(0);
    let counting_sync = |_prompt: &str, _system: &str, _tier: PromptTier| {
        calls.fetch_add(1, Ordering::SeqCst);
        Some("Second-run prose.".to_string())
    };
    let mut counting_serial =
        |prompt: &str, system: &str, tier: PromptTier| counting_sync(prompt, system, tier);
    let mut plan = ReusePlan::load(project.path(), &out_dir, "symbols").expect("reuse plan loads");
    let second = collect_docs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut counting_serial),
            reuse: Some(&mut plan),
            file_workers: Some(FileGenerationWorkers {
                workers: NonZeroUsize::new(4).expect("worker count"),
                generate: &counting_sync,
                verify: None,
            }),
            ..Default::default()
        },
    );
    assert_eq!(
        calls.load(Ordering::SeqCst),
        0,
        "unchanged sources must make zero LLM calls through the pool"
    );
    // Reused docs carry the on-disk pages verbatim even through the pool.
    let file_page = second
        .iter()
        .find(|doc| doc.path.starts_with("code/files/"))
        .expect("file page is emitted");
    let on_disk =
        std::fs::read_to_string(out_dir.join(&file_page.path)).expect("file page on disk");
    assert_eq!(file_page.content, on_disk);
}
