use std::collections::BTreeMap;

use chrono::{DateTime, Utc};

use super::super::LabelSource;
use super::super::labels::{derive_label, label_candidates};
use super::super::partition::{PartitionCommunity, ProjectPartition};
use super::{AssignedCommunity, PriorCommunity, assign_ids};

fn community(members: &[&str], signature: &str) -> PartitionCommunity {
    let members: Vec<String> = members.iter().map(|member| (*member).to_owned()).collect();
    let in_degree = members.iter().cloned().map(|member| (member, 0)).collect();
    PartitionCommunity {
        internal_edges: members.len().saturating_sub(1),
        cohesion: 1.0,
        members,
        in_degree,
        member_signature: signature.to_owned(),
    }
}

fn project(communities: Vec<PartitionCommunity>) -> ProjectPartition {
    let file_count = communities
        .iter()
        .map(|community| community.members.len())
        .sum();
    ProjectPartition {
        communities,
        directed: Vec::new(),
        file_count,
        partition_signature: "partition-signature".to_owned(),
    }
}

fn timestamp() -> DateTime<Utc> {
    "2026-09-20T12:00:00Z"
        .parse()
        .expect("fixed test timestamp is valid")
}

fn prior(
    community_id: i32,
    members: &[&str],
    member_signature: &str,
    label_source: LabelSource,
) -> PriorCommunity {
    PriorCommunity {
        community_id,
        members: members.iter().map(|member| (*member).to_owned()).collect(),
        member_signature: member_signature.to_owned(),
        label: "stored-label".to_owned(),
        label_deterministic: "stored-deterministic".to_owned(),
        label_source,
        label_confidence: Some(0.85),
        label_model: Some("model-v1".to_owned()),
        labeled_signature: Some(member_signature.to_owned()),
        labeled_at: Some(timestamp()),
        label_attempted_at: Some(timestamp()),
    }
}

fn ids(assigned: &[AssignedCommunity]) -> Vec<i32> {
    assigned
        .iter()
        .map(|community| community.community_id)
        .collect()
}

#[test]
fn unchanged_partition_keeps_every_id() {
    let partition = project(vec![
        community(&["src/a.rs", "src/b.rs"], "sig-ab"),
        community(&["tests/a.rs"], "sig-test"),
    ]);
    let prior = vec![
        prior(7, &["src/a.rs", "src/b.rs"], "sig-ab", LabelSource::Model),
        prior(2, &["tests/a.rs"], "sig-test", LabelSource::Deterministic),
    ];

    let (assigned, watermark) = assign_ids(&partition, &prior, 7);

    assert_eq!(ids(&assigned), vec![7, 2]);
    assert_eq!(assigned[0].matched_prior, Some(7));
    assert_eq!(assigned[0].partition_index, 0);
    assert_eq!(assigned[0].label.label, "stored-label");
    assert_eq!(
        assigned[0].label.label_deterministic,
        derive_label(
            &partition.communities[0].members,
            &partition.communities[0].in_degree
        )
    );
    assert_eq!(assigned[0].label.label_source, LabelSource::Model);
    assert_eq!(assigned[0].label.label_confidence, Some(0.85));
    assert_eq!(assigned[0].label.label_model.as_deref(), Some("model-v1"));
    assert_eq!(
        assigned[0].label.labeled_signature.as_deref(),
        Some("sig-ab")
    );
    assert_eq!(assigned[0].label.labeled_at, Some(timestamp()));
    assert_eq!(assigned[0].label.label_attempted_at, Some(timestamp()));
    assert_eq!(
        assigned[0].label.label_candidates,
        label_candidates(&partition.communities[0])
    );
    assert_eq!(watermark, 7);
}

#[test]
fn split_keeps_id_on_larger_child_and_issues_fresh_id() {
    let partition = project(vec![
        community(&["a.rs", "b.rs"], "sig-ab"),
        community(&["c.rs"], "sig-c"),
    ]);
    let prior = vec![prior(
        10,
        &["a.rs", "b.rs", "c.rs"],
        "sig-abc",
        LabelSource::Deterministic,
    )];

    let (assigned, watermark) = assign_ids(&partition, &prior, 10);

    assert_eq!(ids(&assigned), vec![10, 11]);
    assert_eq!(assigned[0].matched_prior, Some(10));
    assert_eq!(assigned[1].matched_prior, None);
    assert_eq!(watermark, 11);
}

#[test]
fn merge_keeps_id_of_larger_parent() {
    let partition = project(vec![community(
        &["a.rs", "b.rs", "c.rs", "d.rs"],
        "sig-abcd",
    )]);
    let prior = vec![
        prior(
            4,
            &["a.rs", "b.rs", "c.rs"],
            "sig-abc",
            LabelSource::Deterministic,
        ),
        prior(7, &["d.rs"], "sig-d", LabelSource::Deterministic),
    ];

    let (assigned, watermark) = assign_ids(&partition, &prior, 7);

    assert_eq!(ids(&assigned), vec![4]);
    assert_eq!(assigned[0].matched_prior, Some(4));
    assert_eq!(watermark, 7);
}

#[test]
fn jaccard_beats_raw_overlap_on_swallow_case() {
    let partition = project(vec![
        community(
            &[
                "a.rs", "b.rs", "c.rs", "x.rs", "y.rs", "z.rs", "q.rs", "r.rs", "s.rs", "t.rs",
            ],
            "sig-swallow",
        ),
        community(&["d.rs", "e.rs"], "sig-de"),
    ]);
    let prior = vec![prior(
        3,
        &["a.rs", "b.rs", "c.rs", "d.rs", "e.rs"],
        "sig-abcde",
        LabelSource::Deterministic,
    )];

    let (assigned, _) = assign_ids(&partition, &prior, 3);

    assert_eq!(ids(&assigned), vec![4, 3]);
    assert_eq!(assigned[1].matched_prior, Some(3));
}

#[test]
fn equal_candidates_prefer_old_id_then_new_index() {
    let partition = project(vec![
        community(&["a.rs", "b.rs"], "sig-ab"),
        community(&["c.rs", "d.rs"], "sig-cd"),
    ]);
    let prior = vec![
        prior(8, &["b.rs", "d.rs"], "sig-bd", LabelSource::Deterministic),
        prior(2, &["a.rs", "c.rs"], "sig-ac", LabelSource::Deterministic),
    ];

    let (assigned, _) = assign_ids(&partition, &prior, 8);

    assert_eq!(ids(&assigned), vec![2, 8]);
}

#[test]
fn retired_ids_are_never_reissued() {
    let partition = project(vec![
        community(&["kept.rs"], "sig-kept"),
        community(&["unrelated.rs"], "sig-new"),
    ]);
    let prior = vec![
        prior(
            3,
            &["retired.rs"],
            "sig-retired",
            LabelSource::Deterministic,
        ),
        prior(9, &["kept.rs"], "sig-kept", LabelSource::Deterministic),
    ];

    let (assigned, watermark) = assign_ids(&partition, &prior, 9);

    assert_eq!(ids(&assigned), vec![9, 10]);
    assert_eq!(watermark, 10);
}

#[test]
fn renamed_files_keep_id_and_flip_signature() {
    let partition = project(vec![community(
        &["src/kept.rs", "src/renamed.rs"],
        "sig-new",
    )]);
    let prior = vec![prior(
        5,
        &["src/kept.rs", "src/old.rs"],
        "sig-old",
        LabelSource::Deterministic,
    )];

    let (assigned, _) = assign_ids(&partition, &prior, 5);

    assert_eq!(ids(&assigned), vec![5]);
    assert_eq!(assigned[0].matched_prior, Some(5));
    assert_eq!(assigned[0].label.labeled_signature, None);
}

#[test]
fn model_label_carries_with_stale_signature() {
    let partition = project(vec![community(&["src/a.rs", "src/c.rs"], "sig-new")]);
    let prior = vec![prior(
        12,
        &["src/a.rs", "src/b.rs"],
        "sig-old",
        LabelSource::Model,
    )];

    let (assigned, _) = assign_ids(&partition, &prior, 12);
    let label = &assigned[0].label;

    assert_eq!(label.label, "stored-label");
    assert_eq!(
        label.label_deterministic,
        derive_label(
            &partition.communities[0].members,
            &partition.communities[0].in_degree,
        )
    );
    assert_eq!(label.label_source, LabelSource::Model);
    assert_eq!(label.label_confidence, Some(0.85));
    assert_eq!(label.label_model.as_deref(), Some("model-v1"));
    assert_eq!(label.labeled_signature.as_deref(), Some("sig-old"));
    assert_eq!(label.labeled_at, Some(timestamp()));
    assert_eq!(label.label_attempted_at, Some(timestamp()));
    assert_eq!(
        label.label_candidates,
        label_candidates(&partition.communities[0])
    );
}

#[test]
fn deterministic_label_recomputes_on_change() {
    let partition = project(vec![community(&["new/a.rs", "new/b.rs"], "sig-new")]);
    let prior = vec![prior(
        6,
        &["new/a.rs", "old/b.rs"],
        "sig-old",
        LabelSource::Deterministic,
    )];

    let (assigned, _) = assign_ids(&partition, &prior, 6);
    let label = &assigned[0].label;
    let deterministic = derive_label(
        &partition.communities[0].members,
        &partition.communities[0].in_degree,
    );

    assert_eq!(label.label, deterministic);
    assert_eq!(label.label_deterministic, deterministic);
    assert_eq!(label.label_source, LabelSource::Deterministic);
    assert_eq!(
        label.label_candidates,
        label_candidates(&partition.communities[0])
    );
}

#[test]
fn gate_rejected_label_reopens_on_membership_change() {
    let partition = project(vec![community(&["src/a.rs", "src/c.rs"], "sig-new")]);
    let prior = vec![prior(
        18,
        &["src/a.rs", "src/b.rs"],
        "sig-old",
        LabelSource::Deterministic,
    )];

    let (assigned, _) = assign_ids(&partition, &prior, 18);
    let label = &assigned[0].label;

    assert_eq!(label.labeled_signature, None);
    assert_eq!(label.label_confidence, None);
    assert_eq!(label.label_model, None);
    assert_eq!(label.labeled_at, None);
    assert_eq!(label.label_attempted_at, Some(timestamp()));
}

#[test]
fn watermark_is_monotone_across_runs() {
    let first = project(vec![community(&["a.rs"], "sig-a")]);
    let (first_assigned, first_watermark) = assign_ids(&first, &[], 5);
    let first_prior = vec![prior(
        first_assigned[0].community_id,
        &["a.rs"],
        "sig-a",
        LabelSource::Deterministic,
    )];
    let second = project(vec![
        community(&["a.rs"], "sig-a"),
        community(&["b.rs"], "sig-b"),
    ]);

    let (_, second_watermark) = assign_ids(&second, &first_prior, first_watermark);

    assert_eq!(first_watermark, 6);
    assert_eq!(second_watermark, 7);
}

#[test]
fn watermark_stays_at_input_when_no_id_is_issued() {
    let partition = project(vec![community(&["a.rs"], "sig-a")]);
    let prior = vec![prior(9, &["a.rs"], "sig-a", LabelSource::Deterministic)];

    let (assigned, watermark) = assign_ids(&partition, &prior, 5);

    assert_eq!(ids(&assigned), vec![9]);
    assert_eq!(watermark, 5);
}

#[test]
fn label_source_round_trips_exact_values() {
    for source in [LabelSource::Deterministic, LabelSource::Model] {
        assert_eq!(LabelSource::parse(source.as_str()), Some(source));
    }
    assert_eq!(
        LabelSource::parse("deterministic"),
        Some(LabelSource::Deterministic)
    );
    assert_eq!(LabelSource::parse("model"), Some(LabelSource::Model));
    assert_eq!(LabelSource::parse("MODEL"), None);
    assert_eq!(LabelSource::parse("manual"), None);
}

#[test]
fn unmatched_community_uses_deterministic_label_and_candidates() {
    let partition = project(vec![community(&["src/a.rs", "src/b.rs"], "sig-new")]);

    let (assigned, _) = assign_ids(&partition, &[], 0);
    let label = &assigned[0].label;
    let expected = derive_label(
        &partition.communities[0].members,
        &partition.communities[0].in_degree,
    );

    assert_eq!(label.label, expected);
    assert_eq!(label.label_deterministic, expected);
    assert_eq!(label.label_source, LabelSource::Deterministic);
    assert_eq!(label.label_confidence, None);
    assert_eq!(label.label_model, None);
    assert_eq!(label.labeled_signature, None);
    assert_eq!(label.labeled_at, None);
    assert_eq!(label.label_attempted_at, None);
    assert_eq!(
        label.label_candidates,
        label_candidates(&partition.communities[0])
    );
}

#[test]
fn test_helpers_keep_empty_indegree_explicit() {
    let community = community(&["src/a.rs"], "sig-a");
    assert_eq!(
        community.in_degree,
        BTreeMap::from([("src/a.rs".to_owned(), 0)])
    );
}

#[test]
fn deterministic_labels_dedupe_in_current_partition_order() {
    let partition = project(vec![
        community(
            &["src/gobby/c1.py", "src/gobby/c2.py", "src/gobby/c3.py"],
            "sig-c",
        ),
        community(&["src/gobby/a1.py", "src/gobby/a2.py"], "sig-a"),
        community(&["src/gobby/b1.py", "src/gobby/b2.py"], "sig-b"),
        community(&["src/gobby/d1.py", "src/gobby/d2.py"], "sig-d"),
        community(&["tests/e1.py", "tests/e2.py"], "sig-e"),
    ]);
    let a = &["src/gobby/a1.py", "src/gobby/a2.py"];
    let b = &["src/gobby/b1.py", "src/gobby/b2.py"];
    let e = &["tests/e1.py", "tests/e2.py"];
    let prior = vec![
        PriorCommunity {
            label: "src/gobby".to_owned(),
            label_deterministic: "src/gobby".to_owned(),
            ..prior(1, a, "sig-a", LabelSource::Deterministic)
        },
        PriorCommunity {
            label: "src/gobby #2".to_owned(),
            label_deterministic: "src/gobby #2".to_owned(),
            ..prior(2, b, "sig-b", LabelSource::Deterministic)
        },
        prior(
            3,
            &["src/gobby/d1.py", "src/gobby/d2.py"],
            "sig-d",
            LabelSource::Model,
        ),
        // A gate-skipped model pick (6.3) keeps its chosen candidate.
        PriorCommunity {
            label: "tests/e1.py".to_owned(),
            label_deterministic: "tests".to_owned(),
            ..prior(4, e, "sig-e", LabelSource::Deterministic)
        },
    ];

    let (assigned, _) = assign_ids(&partition, &prior, 4);

    let labels = assigned
        .iter()
        .map(|community| {
            (
                community.label.label.as_str(),
                community.label.label_deterministic.as_str(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        labels,
        [
            ("src/gobby", "src/gobby"),
            ("src/gobby #2", "src/gobby #2"),
            ("src/gobby #3", "src/gobby #3"),
            ("stored-label", "src/gobby #4"),
            ("tests/e1.py", "tests"),
        ]
    );
    for community in &assigned {
        assert_eq!(
            community.label.label_candidates[0],
            community.label.label_deterministic
        );
    }
}
