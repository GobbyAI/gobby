use std::fs;
use std::path::PathBuf;

use crate::WikiError;
use crate::explainer::ExplainerGeneration;

use super::paths::source_page_paths;
use super::render::yaml_scalar;
use super::{
    ArticleKind, PageWriteKind, SynthesisInput, SynthesisSource, SynthesizedPage, WritePolicy,
    slugify_unique, synthesize_article, write_synthesized_page,
};

#[test]
fn existing_page_requires_merge_intent() {
    let temp = tempfile::tempdir().expect("tempdir");
    let page_path = temp.path().join("knowledge/topics/existing.md");
    std::fs::create_dir_all(page_path.parent().expect("page parent")).expect("create parent");
    std::fs::write(&page_path, "human-authored page").expect("existing page written");

    let page = SynthesizedPage {
        path: page_path.clone(),
        title: "Existing".to_string(),
        markdown: "---\ntitle: Existing\n---\n# Existing\nNew synthesis.\n".to_string(),
        explainer: None,
    };

    let error = write_synthesized_page(temp.path(), &page, WritePolicy::RequireMergeIntent)
        .expect_err("existing page requires merge intent");

    assert!(matches!(
        error,
        crate::WikiError::InvalidInput {
            field: "write_intent",
            ..
        }
    ));
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page retained"),
        "human-authored page"
    );
}

#[test]
fn synthesized_page_write_classifies_create_and_overwrite_atomically() {
    let temp = tempfile::tempdir().expect("tempdir");
    let page_path = temp.path().join("knowledge/topics/new.md");
    let page = SynthesizedPage {
        path: page_path.clone(),
        title: "New".to_string(),
        markdown: "# New\n".to_string(),
        explainer: None,
    };

    let created = write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
        .expect("create synthesized page");
    let overwritten =
        write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
            .expect("overwrite synthesized page");

    assert_eq!(created.kind, PageWriteKind::Created);
    assert_eq!(overwritten.kind, PageWriteKind::Overwritten);
    assert_eq!(
        std::fs::read_to_string(&page_path).expect("page written"),
        "# New\n"
    );
}

#[test]
fn slugify_unique_falls_back_after_bounded_suffixes() {
    let slug = slugify_unique("Collision", |_| true);

    assert!(slug.starts_with("collision-"));
    assert!(slug.len() > "collision-".len());
}

#[test]
fn source_page_paths_reserve_article_path() {
    let temp = tempfile::tempdir().expect("tempdir");
    let article_path = temp.path().join("knowledge/sources/collision.md");
    let sources = vec![SynthesisSource {
        title: "Collision".to_string(),
        path: PathBuf::from("raw/collision.md"),
        chunks: Vec::new(),
    }];

    let paths = source_page_paths(temp.path(), &article_path, &sources);

    assert_ne!(paths[0], article_path);
    assert!(paths[0].starts_with(temp.path().join("knowledge/sources")));
}

#[test]
fn synthesized_article_rejects_escaping_target_path() {
    let temp = tempfile::tempdir().expect("tempdir");
    let input = SynthesisInput {
        handoff_id: "handoff-1".to_string(),
        topic: "Escape".to_string(),
        outline: vec![],
        target_kind: ArticleKind::Topic,
        accepted_sources: vec![],
        citations: vec![],
        conflicting_claims: vec![],
        missing_evidence: vec![],
    };
    let outside_name = format!(
        "{}-outside.md",
        temp.path()
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("synthesis")
    );
    let target = temp.path().join("..").join(outside_name);

    let error = synthesize_article(
        temp.path(),
        &input,
        Some(target),
        &ExplainerGeneration::Skipped,
    )
    .expect_err("escaping target must be rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "article_path",
            ..
        }
    ));
}

#[test]
fn synthesized_writer_rejects_escaping_page_path_before_write() {
    let temp = tempfile::tempdir().expect("tempdir");
    let outside_name = format!(
        "{}-outside.md",
        temp.path()
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("synthesis")
    );
    let outside = temp.path().join("..").join(outside_name);
    let page = SynthesizedPage {
        path: outside.clone(),
        title: "Outside".to_string(),
        markdown: "# Outside\n".to_string(),
        explainer: None,
    };

    let error = write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
        .expect_err("escaping page must be rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "synthesized_page",
            ..
        }
    ));
    assert!(!outside.exists());
}

#[test]
#[cfg(unix)]
fn synthesized_writer_rejects_symlinked_parent_before_create_dir_all() {
    use std::os::unix::fs::symlink;

    let temp = tempfile::tempdir().expect("tempdir");
    let outside = tempfile::tempdir().expect("outside tempdir");
    let link = temp.path().join("wiki").join("linked");
    fs::create_dir_all(link.parent().expect("link parent")).expect("link parent");
    symlink(outside.path(), &link).expect("symlink parent");
    let page = SynthesizedPage {
        path: link.join("nested/page.md"),
        title: "Outside".to_string(),
        markdown: "# Outside\n".to_string(),
        explainer: None,
    };

    let error = write_synthesized_page(temp.path(), &page, WritePolicy::AllowOverwriteAfterMerge)
        .expect_err("symlinked parent must be rejected");

    assert!(matches!(
        error,
        WikiError::InvalidInput {
            field: "synthesized_page",
            ..
        }
    ));
    assert!(!outside.path().join("nested/page.md").exists());
}

#[test]
fn yaml_scalar_escapes_quoted_control_characters() {
    assert_eq!(yaml_scalar("Plain Title"), "\"Plain Title\"");
    assert_eq!(
        yaml_scalar("a\\b\"c\nd\re\tf"),
        "\"a\\\\b\\\"c\\nd\\re\\tf\""
    );
    assert_eq!(
        yaml_scalar("nul\0del\u{7f}\u{80}"),
        "\"nul\\u0000del\\u007f\\u0080\""
    );
}
