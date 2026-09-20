use std::collections::{BTreeMap, HashSet};
use std::env;

use crate::communities::identity::identity_tests::identity_from;
use crate::communities::identity::load_project_imports;
use crate::communities::labels::{dedupe_labels, derive_label, label_candidates};
use crate::config::Context;

use super::{
    DirectedImport, PartitionCommunity, PartitionError, ProjectPartition, build_partition,
    cohesion, directed_imports, fold_undirected, in_degrees, internal_edges, is_low_cohesion,
    leiden_groups, member_signature, sorted_files, split_low_cohesion, split_oversized,
};

type Undirected = BTreeMap<(String, String), usize>;

fn paths(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_string()).collect()
}

fn owned(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs
        .iter()
        .map(|(source, module)| ((*source).to_string(), (*module).to_string()))
        .collect()
}

fn partition_of(visible: &[&str], imports: &[(String, String)]) -> ProjectPartition {
    let borrowed = imports
        .iter()
        .map(|(source, module)| (source.as_str(), module.as_str()))
        .collect::<Vec<_>>();
    let identity = identity_from(visible, &borrowed);
    build_partition(&identity, imports).expect("a valid partition")
}

/// The Python module name that resolves back to `path`.
fn module_for(path: &str) -> String {
    path.trim_end_matches(".py").replace('/', ".")
}

fn numbered(prefix: &str, count: usize) -> Vec<String> {
    (0..count)
        .map(|index| format!("{prefix}/f{index:02}.py"))
        .collect()
}

/// Every unordered pair inside `members`, which must already be sorted.
fn clique(edges: &mut Undirected, members: &[String], weight: usize) {
    for (index, left) in members.iter().enumerate() {
        for right in &members[index + 1..] {
            edges.insert((left.clone(), right.clone()), weight);
        }
    }
}

fn link(edges: &mut Undirected, left: &str, right: &str, weight: usize) {
    let key = if left <= right {
        (left.to_string(), right.to_string())
    } else {
        (right.to_string(), left.to_string())
    };
    edges.insert(key, weight);
}

fn member_signatures(partition: &ProjectPartition) -> Vec<String> {
    partition
        .communities
        .iter()
        .map(|community| community.member_signature.clone())
        .collect()
}

#[test]
fn alias_collapse_folds_into_one_edge() {
    let partition = partition_of(
        &["a.py", "pkg/__init__.py"],
        &owned(&[("a.py", "pkg"), ("a.py", "pkg.__init__")]),
    );

    assert_eq!(
        partition.directed,
        vec![DirectedImport {
            importer: "a.py".to_string(),
            provider: "pkg/__init__.py".to_string(),
            count: 2,
        }]
    );
}

#[test]
fn external_module_adds_no_edge() {
    let partition = partition_of(&["a.py"], &owned(&[("a.py", "numpy")]));

    assert!(partition.directed.is_empty());
    assert_eq!(partition.file_count, 1);
}

#[test]
fn ambiguous_provider_adds_no_edge() {
    let partition = partition_of(&["a.py", "b.py", "src/b.py"], &owned(&[("a.py", "b")]));

    assert!(partition.directed.is_empty(), "{:?}", partition.directed);
}

#[test]
fn self_import_is_dropped() {
    let partition = partition_of(&["pkg/__init__.py"], &owned(&[("pkg/__init__.py", "pkg")]));

    assert!(partition.directed.is_empty());
}

#[test]
fn non_visible_source_row_is_skipped() {
    let partition = partition_of(
        &["a.py", "b.py"],
        &owned(&[("ghost.py", "b"), ("a.py", "b")]),
    );

    assert_eq!(
        partition.directed,
        vec![DirectedImport {
            importer: "a.py".to_string(),
            provider: "b.py".to_string(),
            count: 1,
        }]
    );
}

#[test]
fn provider_outside_visible_set_is_typed_error() {
    // `ImportIdentity::providers_for` filters by the identity's own visible set,
    // so the guard can only fire when the partition's node set is narrower than
    // the identity's. Calling the row walk with that narrower set is the only
    // way to reach the invariant violation it exists to report.
    let identity = identity_from(&["a.py", "b.py"], &[("a.py", "b")]);
    let files = HashSet::from(["a.py"]);

    let error = directed_imports(&identity, &files, &owned(&[("a.py", "b")]))
        .expect_err("a provider outside the node set is an invariant violation");

    assert_eq!(
        error,
        PartitionError::ProviderNotVisible {
            module: "b".to_string(),
            file: "b.py".to_string(),
        }
    );
}

#[test]
fn two_cliques_joined_by_bridge_split_by_directory() {
    let alpha = paths(&["alpha/a1.py", "alpha/a2.py", "alpha/a3.py", "alpha/a4.py"]);
    let beta = paths(&["beta/b1.py", "beta/b2.py", "beta/b3.py", "beta/b4.py"]);
    let mut imports = Vec::new();
    for group in [&alpha, &beta] {
        for source in group {
            for target in group {
                if source != target {
                    imports.push((source.clone(), module_for(target)));
                }
            }
        }
    }
    imports.push((alpha[0].clone(), module_for(&beta[0])));
    let visible = alpha
        .iter()
        .chain(beta.iter())
        .map(String::as_str)
        .collect::<Vec<_>>();

    let partition = partition_of(&visible, &imports);

    let members = partition
        .communities
        .iter()
        .map(|community| community.members.clone())
        .collect::<Vec<_>>();
    assert_eq!(members, vec![alpha, beta]);
}

#[test]
fn oversized_community_is_split_once() {
    // 100 files, so `max_size` is 25. The blob is a 40-clique and a 30-clique
    // joined by one import: Leiden separates them in a single pass, and both
    // children are still over the ceiling. A recursive pass would keep going,
    // so leaving them at 40 and 30 is exactly what proves the pass runs once.
    let left = numbered("left", 40);
    let right = numbered("right", 30);
    let mut edges = Undirected::new();
    clique(&mut edges, &left, 1);
    clique(&mut edges, &right, 1);
    link(&mut edges, &left[0], &right[0], 1);
    let mut blob = left.clone();
    blob.extend(right.iter().cloned());
    blob.sort();

    let split = split_oversized(vec![blob], 100, &edges).expect("a valid re-partition");

    let mut sizes = split.iter().map(Vec::len).collect::<Vec<_>>();
    sizes.sort_unstable();
    assert_eq!(sizes, vec![30, 40], "children of one pass are not re-split");
}

#[test]
fn oversized_pass_leaves_communities_within_the_ceiling_alone() {
    let members = numbered("src", 20);
    let mut edges = Undirected::new();
    clique(&mut edges, &members, 1);

    let split = split_oversized(vec![members.clone()], 100, &edges).expect("a valid pass");

    assert_eq!(split, vec![members]);
}

#[test]
fn low_cohesion_community_is_split_once() {
    // Two stars joined by one import: 50 members, 49 edges, so
    // 40 * 49 = 1960 < 50 * 49 = 2450 and the community is below the threshold.
    let left = numbered("left", 25);
    let right = numbered("right", 25);
    let mut edges = Undirected::new();
    for group in [&left, &right] {
        for leaf in &group[1..] {
            link(&mut edges, &group[0], leaf, 1);
        }
    }
    link(&mut edges, &left[0], &right[0], 1);
    let mut members = left.clone();
    members.extend(right.iter().cloned());
    members.sort();

    let split = split_low_cohesion(vec![members], &edges).expect("a valid re-partition");

    let mut sizes = split.iter().map(Vec::len).collect::<Vec<_>>();
    sizes.sort_unstable();
    assert_eq!(sizes, vec![25, 25]);
}

#[test]
fn cohesion_threshold_uses_integer_math() {
    // 40 * 77 == 56 * 55 exactly: the community sits on the threshold, and
    // `cohesion < 0.05` is false there.
    assert!(!is_low_cohesion(56, 77));
    assert!(is_low_cohesion(56, 76));
    assert!(
        !is_low_cohesion(49, 0),
        "the member floor is checked before the ratio"
    );
    // `n * (n - 1)` is 2.5e19 here, past `u64::MAX`; the comparison runs in u128.
    assert!(is_low_cohesion(5_000_000_000, 1));
}

#[test]
fn singleton_cohesion_is_one() {
    let partition = partition_of(&["a.py"], &[]);

    assert_eq!(partition.communities.len(), 1);
    assert_eq!(partition.communities[0].cohesion, 1.0);
    assert_eq!(cohesion(1, 0), 1.0);
    assert_eq!(cohesion(2, 1), 1.0);
    assert_eq!(cohesion(4, 3), 0.5);
}

#[test]
fn member_signature_matches_graphify_shape() {
    // sha256 of "a.py\0b.py\0", truncated to 16 hex digits, exactly as
    // Graphify's `_member_signature` computes it.
    assert_eq!(
        member_signature(&paths(&["a.py", "b.py"])),
        "3a901b2057022886"
    );
    assert_eq!(
        member_signature(&paths(&["src/gobby/memory/store.py"])),
        "3951addc645f3379"
    );
}

#[test]
fn label_prefers_deepest_majority_prefix() {
    let members = paths(&[
        "src/gobby/memory/a.py",
        "src/gobby/memory/b.py",
        "src/gobby/memory/c.py",
        "src/other.py",
    ]);

    assert_eq!(derive_label(&members, &BTreeMap::new()), "src/gobby/memory");
}

#[test]
fn label_falls_back_to_top_level_plurality() {
    let members = paths(&["a/1.py", "a/2.py", "b/1.py", "c/1.py", "d/1.py"]);

    assert_eq!(derive_label(&members, &BTreeMap::new()), "a");
}

#[test]
fn label_falls_back_to_in_degree_for_root_files() {
    let members = paths(&["conf.py", "main.py", "setup.py"]);
    let in_degree = BTreeMap::from([
        ("conf.py".to_string(), 3),
        ("main.py".to_string(), 1),
        ("setup.py".to_string(), 7),
    ]);

    assert_eq!(derive_label(&members, &in_degree), "setup.py");
}

#[test]
fn label_collision_gets_ordinal_suffix() {
    let labels = dedupe_labels(paths(&["src/gobby", "src/gobby", "web", "src/gobby"]));

    assert_eq!(
        labels,
        paths(&["src/gobby", "src/gobby #2", "web", "src/gobby #3"])
    );
}

#[test]
fn candidates_are_distinct_and_lead_with_deterministic() {
    let community = PartitionCommunity {
        members: paths(&[
            "src/gobby/memory/search.py",
            "src/gobby/memory/store.py",
            "src/gobby/memory/store_index.py",
        ]),
        internal_edges: 3,
        cohesion: 1.0,
        in_degree: BTreeMap::from([
            ("src/gobby/memory/search.py".to_string(), 2),
            ("src/gobby/memory/store.py".to_string(), 9),
            ("src/gobby/memory/store_index.py".to_string(), 1),
        ]),
        member_signature: "0123456789abcdef".to_string(),
    };

    let candidates = label_candidates(&community);

    assert_eq!(candidates[0], "src/gobby/memory");
    assert!(candidates.len() <= 4, "{candidates:?}");
    assert_eq!(
        candidates.iter().collect::<HashSet<_>>().len(),
        candidates.len(),
        "{candidates:?}"
    );
    assert!(candidates.contains(&"store".to_string()), "{candidates:?}");
    assert!(candidates.contains(&"memory".to_string()), "{candidates:?}");
}

#[test]
fn partition_is_invariant_to_row_order() {
    let alpha = paths(&["alpha/a1.py", "alpha/a2.py", "alpha/a3.py"]);
    let beta = paths(&["beta/b1.py", "beta/b2.py", "beta/b3.py"]);
    let mut imports = Vec::new();
    for group in [&alpha, &beta] {
        for source in group {
            for target in group {
                if source != target {
                    imports.push((source.clone(), module_for(target)));
                }
            }
        }
    }
    imports.push((alpha[0].clone(), module_for(&beta[0])));
    let visible = alpha
        .iter()
        .chain(beta.iter())
        .map(String::as_str)
        .collect::<Vec<_>>();
    let mut reversed = imports.clone();
    reversed.reverse();

    assert_eq!(
        partition_of(&visible, &imports),
        partition_of(&visible, &reversed)
    );
}

#[test]
fn partition_signature_covers_edges() {
    let visible = ["a.py", "b.py", "pkg/__init__.py"];
    let base = partition_of(&visible, &owned(&[("a.py", "b"), ("a.py", "pkg")]));
    let heavier = partition_of(
        &visible,
        &owned(&[("a.py", "b"), ("a.py", "pkg"), ("a.py", "pkg.__init__")]),
    );

    assert_eq!(
        member_signatures(&base),
        member_signatures(&heavier),
        "membership is unchanged"
    );
    assert_ne!(base.partition_signature, heavier.partition_signature);
}

#[test]
fn build_handles_twenty_thousand_rows() {
    const FILES: usize = 2_000;
    let files = (0..FILES)
        .map(|index| format!("src/m{index}.py"))
        .collect::<Vec<_>>();
    let mut imports = Vec::with_capacity(FILES * 10);
    for (index, file) in files.iter().enumerate() {
        for offset in 1..=10 {
            imports.push((file.clone(), format!("m{}", (index + offset) % FILES)));
        }
    }
    let visible = files.iter().map(String::as_str).collect::<Vec<_>>();

    let partition = partition_of(&visible, &imports);

    assert_eq!(partition.file_count, FILES);
    assert_eq!(
        partition
            .communities
            .iter()
            .map(|community| community.members.len())
            .sum::<usize>(),
        FILES
    );
}

/// One line per stage of the threshold spike.
fn report(stage: &str, groups: &[Vec<String>], file_count: usize, undirected: &Undirected) {
    let mut sizes = groups.iter().map(Vec::len).collect::<Vec<_>>();
    sizes.sort_unstable();
    let singletons = sizes.iter().filter(|size| **size == 1).count();
    let largest = sizes.last().copied().unwrap_or(0);
    let median = sizes.get(sizes.len() / 2).copied().unwrap_or(0);
    let mut cohesions = groups
        .iter()
        .filter(|group| group.len() >= 3)
        .map(|group| cohesion(group.len(), internal_edges(group, undirected)))
        .collect::<Vec<_>>();
    cohesions.sort_by(|left, right| left.partial_cmp(right).expect("finite cohesion"));
    let median_cohesion = cohesions.get(cohesions.len() / 2).copied().unwrap_or(0.0);
    let share = if file_count == 0 {
        0.0
    } else {
        largest as f64 / file_count as f64
    };
    println!(
        "{stage}: communities={} files={file_count} largest={largest} largest_share={share:.4} \
median={median} singletons={singletons} median_cohesion_ge3={median_cohesion:.4}",
        groups.len()
    );
    println!("{stage}: sizes={sizes:?}");
}

/// The threshold spike for plan acceptance 2.2.7, 2.2.11 and 2.2.12.
///
/// ```bash
/// GCODE_EXPERIMENT_DSN=<hub dsn> GCODE_EXPERIMENT_ROOT=<project root> \
///   cargo nextest run -p gobby-code --no-capture \
///   -E 'test(partition_experiment_reports_distribution)'
/// ```
///
/// The spike measures a live code index, so it is gated on its two environment
/// variables rather than `#[ignore]`d: an ordinary run has no index to read and
/// returns, and the spike run does the work.
#[test]
fn partition_experiment_reports_distribution() {
    let (Ok(dsn), Ok(root)) = (
        env::var("GCODE_EXPERIMENT_DSN"),
        env::var("GCODE_EXPERIMENT_ROOT"),
    ) else {
        return;
    };
    let mut ctx = Context::resolve(Some(&root), true).expect("a resolvable project");
    ctx.database_url = dsn;
    let mut conn = crate::db::connect_readonly(&ctx.database_url).expect("a read-only connection");
    let imports = load_project_imports(&mut conn, &ctx).expect("the project's import rows");

    let files = sorted_files(&imports.identity);
    let file_set = files.iter().map(String::as_str).collect::<HashSet<_>>();
    let directed = directed_imports(&imports.identity, &file_set, &imports.rows)
        .expect("every provider is visible");
    let undirected = fold_undirected(&directed);
    let in_degree = in_degrees(&directed);

    let base = leiden_groups(&files, &undirected).expect("a valid import graph");
    report("leiden-only", &base, files.len(), &undirected);
    let oversized =
        split_oversized(base.clone(), files.len(), &undirected).expect("oversized pass");
    report("plus-oversized", &oversized, files.len(), &undirected);
    let mut split = split_low_cohesion(oversized, &undirected).expect("low-cohesion pass");
    report("plus-low-cohesion", &split, files.len(), &undirected);

    split.sort_by(|left, right| {
        right
            .len()
            .cmp(&left.len())
            .then_with(|| left[0].cmp(&right[0]))
    });
    let labels = dedupe_labels(
        split
            .iter()
            .map(|group| {
                let scoped = group
                    .iter()
                    .map(|member| (member.clone(), in_degree.get(member).copied().unwrap_or(0)))
                    .collect::<BTreeMap<_, _>>();
                derive_label(group, &scoped)
            })
            .collect(),
    );
    for (group, label) in split.iter().zip(&labels) {
        if group.len() >= 5 {
            println!("label: size={} {label}", group.len());
        }
    }
    assert!(
        !labels.iter().any(|label| label.starts_with("community-")),
        "no community-N placeholder is ever produced"
    );
}
