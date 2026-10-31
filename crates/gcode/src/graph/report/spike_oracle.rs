// SPIKE-PARITY scaffolding: chunked HEAD oracle for NS parity. Remove after GO.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::time::{Duration, Instant};

use anyhow::{Context as _, ensure};
use gobby_core::falkor::{GraphClient, Row};

use super::rows::{row_string, row_usize};
use crate::graph::typed_query::string_params;
use crate::visibility::graph_versions::{AdmittedNamespace, with_admitted_versions};

const ENGINE_ROW_CAP: usize = 10_000;
const NULL_PATH_PREDICATE: &str = "(n.file_path IS NULL OR n.file_path = '')";

/// Node id to (degree, incoming, outgoing).
type Degrees = BTreeMap<String, (usize, usize, usize)>;

pub(super) fn run(
    client: &mut GraphClient,
    namespace: &AdmittedNamespace,
    chunk_count: usize,
) -> anyhow::Result<()> {
    let project = namespace.project_id.as_str();
    eprintln!("SPIKE oracle begin ns={project}");
    let seed_query = "MATCH (n:CodeSymbol {project: $project}) \
                      RETURN DISTINCT n.file_path AS file_path ORDER BY file_path";
    let seed_rows = client
        .query(seed_query, Some(string_params(&[("project", project)])))
        .context("oracle seed paths")?;
    let mut paths = Vec::new();
    let mut null_paths = 0usize;
    for row in &seed_rows {
        match row_string(row, &["file_path"]) {
            Some(path) => paths.push(path),
            None => null_paths += 1,
        }
    }
    ensure!(
        paths.iter().all(|path| !path.contains(|c| c == '\'' || c == '\\')),
        "seed path needs Cypher escaping"
    );

    let admitted: BTreeSet<&str> = namespace.versions.paths.iter().map(String::as_str).collect();
    let in_admitted = paths
        .iter()
        .filter(|path| admitted.contains(path.as_str()))
        .count();

    // Partition every distinct seed path into `chunk_count` slices. Seed nodes
    // without a file path form one extra bucket so the partition covers every
    // seed node exactly once.
    let total = paths.len();
    let chunk_count = chunk_count.clamp(1, total.max(1));
    let mut queue: VecDeque<Chunk> = (0..chunk_count)
        .map(|i| {
            let slice = paths[i * total / chunk_count..(i + 1) * total / chunk_count].to_vec();
            Chunk { seeds: slice.len(), paths: slice, null_bucket: false }
        })
        .collect();
    if null_paths > 0 {
        queue.push_back(Chunk { paths: Vec::new(), seeds: null_paths, null_bucket: true });
    }
    let covered: usize = queue.iter().map(|chunk| chunk.seeds).sum();
    ensure!(
        covered == total + null_paths,
        "partition covers {covered} of {} seed nodes",
        total + null_paths
    );

    let head_text = super::head_queries::report_hotspots_query(project, "symbol", None).0;
    let prod_text = super::queries::report_hotspots_query(project, "symbol", None).0;

    let mut head_all = Degrees::new();
    let mut prod_all = Degrees::new();
    let mut head_dupes = 0usize;
    let mut prod_dupes = 0usize;
    let mut head_time = Duration::ZERO;
    let mut prod_time = Duration::ZERO;
    let mut max_rows = 0usize;
    let mut chunk_total = queue.len();
    let mut splits = 0usize;
    let mut index = 0usize;
    while let Some(chunk) = queue.pop_front() {
        index += 1;
        let predicate = chunk.predicate();
        let size = chunk.seeds;
        let (head_rows, head_elapsed) = run_chunk(client, &head_text, &predicate, namespace)
            .with_context(|| format!("HEAD chunk {index}"))?;
        let (prod_rows, prod_elapsed) = run_chunk(client, &prod_text, &predicate, namespace)
            .with_context(|| format!("production chunk {index}"))?;
        if head_rows.len() >= ENGINE_ROW_CAP || prod_rows.len() >= ENGINE_ROW_CAP {
            // The engine caps results silently, so a capped chunk splits in half and reruns.
            ensure!(
                !chunk.null_bucket && chunk.paths.len() > 1,
                "chunk {index} reached the {ENGINE_ROW_CAP}-row engine cap at one seed group"
            );
            eprintln!(
                "SPIKE oracle split chunk {index} seeds={size} head_rows={} prod_rows={}",
                head_rows.len(),
                prod_rows.len(),
            );
            let Chunk { mut paths, .. } = chunk;
            let tail = paths.split_off(paths.len() / 2);
            splits += 1;
            chunk_total += 2;
            queue.push_front(Chunk { seeds: tail.len(), paths: tail, null_bucket: false });
            queue.push_front(Chunk { seeds: paths.len(), paths, null_bucket: false });
            continue;
        }
        max_rows = max_rows.max(head_rows.len()).max(prod_rows.len());
        head_time += head_elapsed;
        prod_time += prod_elapsed;

        let (head_map, head_dropped) = degrees(&head_rows);
        let (prod_map, prod_dropped) = degrees(&prod_rows);
        ensure!(
            head_dropped == 0 && prod_dropped == 0,
            "chunk {index} rows missing fields: head={head_dropped} prod={prod_dropped}"
        );
        eprintln!(
            "SPIKE oracle chunk {index}/{chunk_total} seeds={size} head_rows={} prod_rows={} head_t={head_elapsed:?} prod_t={prod_elapsed:?}",
            head_rows.len(),
            prod_rows.len(),
        );
        for (id, value) in head_map {
            head_dupes += usize::from(head_all.insert(id, value).is_some());
        }
        for (id, value) in prod_map {
            prod_dupes += usize::from(prod_all.insert(id, value).is_some());
        }
    }

    let only_head: Vec<&String> = head_all
        .keys()
        .filter(|id| !prod_all.contains_key(*id))
        .collect();
    let only_prod: Vec<&String> = prod_all
        .keys()
        .filter(|id| !head_all.contains_key(*id))
        .collect();
    let differing: Vec<(&String, (usize, usize, usize), (usize, usize, usize))> = head_all
        .iter()
        .filter_map(|(id, head)| {
            let prod = *prod_all.get(id)?;
            (prod != *head).then_some((id, *head, prod))
        })
        .collect();
    let mismatches = only_head.len() + only_prod.len() + differing.len();

    eprintln!(
        "SPIKE oracle result ns={project} seed_nodes={} seed_paths={total} null_seed_paths={null_paths} admitted_paths={} seed_in_admitted={in_admitted} chunks={index} splits={splits} head_nodes={} prod_nodes={} only_head={} only_prod={} differing={} head_dupes={head_dupes} prod_dupes={prod_dupes} max_chunk_rows={max_rows} head_total={head_time:?} prod_total={prod_time:?} verdict={}",
        total + null_paths,
        admitted.len(),
        head_all.len(),
        prod_all.len(),
        only_head.len(),
        only_prod.len(),
        differing.len(),
        if mismatches == 0 && head_dupes == 0 && prod_dupes == 0 { "EXACT" } else { "MISMATCH" },
    );
    for id in only_head.iter().take(20) {
        eprintln!("SPIKE oracle only_head id={id} head={:?}", head_all[*id]);
    }
    for id in only_prod.iter().take(20) {
        eprintln!("SPIKE oracle only_prod id={id} prod={:?}", prod_all[*id]);
    }
    for (id, head, prod) in differing.iter().take(20) {
        eprintln!("SPIKE oracle differs id={id} head={head:?} prod={prod:?}");
    }
    if std::env::var_os("SPIKE_TOP_CHECK").is_some() { // SPIKE-TIMING
        let mut ranked: Vec<(&String, &(usize, usize, usize))> = prod_all.iter().collect(); // SPIKE-TIMING
        ranked.sort_by(|a, b| b.1.0.cmp(&a.1.0).then(a.0.as_str().cmp(b.0.as_str()))); // SPIKE-TIMING
        let truth: Vec<(String, usize)> = ranked.iter().take(10).map(|(id, v)| ((*id).clone(), v.0)).collect(); // SPIKE-TIMING
        let (limit_text, _) = super::queries::report_hotspots_query(project, "symbol", Some(10)); // SPIKE-TIMING
        let (limit_query, limit_params) = with_admitted_versions((limit_text, string_params(&[("project", project)])), &namespace.versions); // SPIKE-TIMING
        let limit_rows = client.query(&limit_query, Some(limit_params))?; // SPIKE-TIMING
        let got: Vec<(String, usize)> = limit_rows.iter().filter_map(|row| Some((row_string(row, &["id"])?, row_usize(row, &["degree"])?))).collect(); // SPIKE-TIMING
        eprintln!("SPIKE top truth={truth:?}"); // SPIKE-TIMING
        eprintln!("SPIKE top limit10 rows={} got={got:?}", limit_rows.len()); // SPIKE-TIMING
        let degree_match = truth.iter().map(|t| t.1).eq(got.iter().map(|g| g.1)); // SPIKE-TIMING
        let id_match = truth.iter().map(|t| &t.0).eq(got.iter().map(|g| &g.0)); // SPIKE-TIMING
        eprintln!("SPIKE top degree_sequence_match={degree_match} id_sequence_match={id_match}"); // SPIKE-TIMING
    } // SPIKE-TIMING
    if std::env::var_os("SPIKE_TOP_CHECK").is_some() { // SPIKE-TIMING
        let (again_text, _) = super::queries::report_hotspots_query(project, "symbol", Some(10)); // SPIKE-TIMING
        let (again_query, again_params) = with_admitted_versions((again_text, string_params(&[("project", project)])), &namespace.versions); // SPIKE-TIMING
        let again_rows = client.query(&again_query, Some(again_params))?; // SPIKE-TIMING
        let again: Vec<usize> = again_rows.iter().filter_map(|row| row_usize(row, &["degree"])).collect(); // SPIKE-TIMING
        eprintln!("SPIKE top limit10_again degrees={again:?}"); // SPIKE-TIMING
        let (unlimited_text, _) = super::queries::report_hotspots_query(project, "symbol", None); // SPIKE-TIMING
        let (unlimited_query, unlimited_params) = with_admitted_versions((unlimited_text, string_params(&[("project", project)])), &namespace.versions); // SPIKE-TIMING
        let unlimited_rows = client.query(&unlimited_query, Some(unlimited_params))?; // SPIKE-TIMING
        let unlimited_top: Vec<(String, usize)> = unlimited_rows.iter().take(10).filter_map(|row| Some((row_string(row, &["id"])?, row_usize(row, &["degree"])?))).collect(); // SPIKE-TIMING
        eprintln!("SPIKE top unlimited rows={} top10={unlimited_top:?}", unlimited_rows.len()); // SPIKE-TIMING
    } // SPIKE-TIMING
    Ok(())
}

/// One oracle chunk: a slice of seed file paths, or the null-path bucket.
struct Chunk {
    paths: Vec<String>,
    seeds: usize,
    null_bucket: bool,
}

impl Chunk {
    fn predicate(&self) -> String {
        if self.null_bucket {
            NULL_PATH_PREDICATE.to_string()
        } else {
            in_list(&self.paths)
        }
    }
}

fn in_list(paths: &[String]) -> String {
    let items = paths
        .iter()
        .map(|path| format!("'{path}'"))
        .collect::<Vec<_>>()
        .join(", ");
    format!("n.file_path IN [{items}]")
}

fn run_chunk(
    client: &mut GraphClient,
    text: &str,
    predicate: &str,
    namespace: &AdmittedNamespace,
) -> anyhow::Result<(Vec<Row>, Duration)> {
    let query = seed_chunk(text, predicate)?;
    let params = string_params(&[("project", namespace.project_id.as_str())]);
    let (query, params) = with_admitted_versions((query, params), &namespace.versions);
    let started = Instant::now();
    let rows = client.query(&query, Some(params))?;
    Ok((rows, started.elapsed()))
}

/// Restricts a hotspot builder's seed nodes to `predicate`. The seed variable `n`
/// is the only node the filter touches; the rest of the query text is unchanged.
fn seed_chunk(text: &str, predicate: &str) -> anyhow::Result<String> {
    let mut query = text.to_string();
    for anchor in ["WHERE (out_target:", "WHERE (in_source:"] {
        let found = query.matches(anchor).count();
        ensure!(found == 1, "anchor {anchor} found {found} times");
        let rest = &anchor["WHERE (".len()..];
        query = query.replace(anchor, &format!("WHERE {predicate} AND ({rest}"));
    }
    Ok(query)
}

fn degrees(rows: &[Row]) -> (Degrees, usize) {
    let mut out = Degrees::new();
    let mut dropped = 0usize;
    for row in rows {
        let parsed = (|| {
            Some((
                row_string(row, &["id"])?,
                (
                    row_usize(row, &["degree"])?,
                    row_usize(row, &["incoming"])?,
                    row_usize(row, &["outgoing"])?,
                ),
            ))
        })();
        match parsed {
            Some((id, value)) => {
                out.insert(id, value);
            }
            None => dropped += 1,
        }
    }
    (out, dropped)
}
