use std::fs;
use std::path::PathBuf;

use crate::WikiError;
use crate::explainer::ExplainerGeneration;

use super::paths::source_page_paths;
use super::render::yaml_scalar;
use super::{
    ArticleKind, PageWriteKind, SynthesisInput, SynthesisSource, SynthesizedPage, WritePolicy,
    resolve_article_path, slugify_unique, synthesize_article, synthesize_source_pages,
    write_synthesized_page,
};

fn empty_input(topic: &str, target_kind: ArticleKind) -> SynthesisInput {
    SynthesisInput {
        handoff_id: "handoff-1".to_string(),
        topic: topic.to_string(),
        outline: vec![],
        target_kind,
        accepted_sources: vec![],
        citations: vec![],
        conflicting_claims: vec![],
        missing_evidence: vec![],
        existing_page_body: None,
        aliases: vec![],
        extra_tags: vec![],
    }
}

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
    let slug = slugify_unique("Collision", "concept", |_| true);

    assert!(slug.starts_with("collision-"));
    assert!(slug.len() > "collision-".len());
}

#[test]
fn slugify_unique_deconflicts_agent_instruction_filenames() {
    assert_eq!(
        slugify_unique("Claude", "concept", |_| false),
        "claude-concept"
    );
    assert_eq!(slugify_unique("AGENTS", "topic", |_| false), "agents-topic");
    assert_eq!(slugify_unique("Gcode", "concept", |_| false), "gcode");
}

#[test]
fn reserved_instruction_title_never_becomes_instruction_filename() {
    let temp = tempfile::tempdir().expect("tempdir");
    let input = empty_input("Claude", ArticleKind::Concept);

    let article_path = resolve_article_path(temp.path(), "Claude", ArticleKind::Concept);
    let article = synthesize_article(
        temp.path(),
        &input,
        article_path,
        &ExplainerGeneration::Skipped,
    )
    .expect("article synthesized");

    assert_eq!(
        article.path,
        temp.path().join("knowledge/concepts/claude-concept.md")
    );
    // Title-addressed resolution still works: the page keeps title `Claude`,
    // and lint lookup keys derived from that title cover `[[Claude]]`.
    let parsed = crate::frontmatter::parse_frontmatter(&article.markdown).expect("frontmatter");
    assert_eq!(parsed.metadata.title.as_deref(), Some("Claude"));
    let keys = gobby_core::vault::lint::page_targets(
        std::path::Path::new("knowledge/concepts/claude-concept.md"),
        parsed.metadata.title.as_deref(),
        &parsed.metadata.aliases,
    );
    assert!(keys.contains(&crate::links::canonical_target_key("Claude")));
}

#[test]
fn resolve_article_path_reuses_existing_page_with_matching_title() {
    let temp = tempfile::tempdir().expect("tempdir");
    let directory = temp.path().join("knowledge/topics");
    fs::create_dir_all(&directory).expect("topics dir");
    fs::write(
        directory.join("durable-compile.md"),
        "---\ntitle: Durable Compile\n---\n# Durable Compile\n",
    )
    .expect("existing article written");

    // A recompile of the same topic resolves back to the existing article
    // instead of minting a -2 sibling (#17635).
    let path = resolve_article_path(temp.path(), "Durable Compile", ArticleKind::Topic);

    assert_eq!(path, directory.join("durable-compile.md"));
}

#[test]
fn resolve_article_path_suffixes_different_topic_with_same_slug() {
    let temp = tempfile::tempdir().expect("tempdir");
    let directory = temp.path().join("knowledge/topics");
    fs::create_dir_all(&directory).expect("topics dir");
    fs::write(
        directory.join("durable-compile.md"),
        "---\ntitle: Durable Compile\n---\n# Durable Compile\n",
    )
    .expect("existing article written");

    // Identity is the exact frontmatter title: a different topic that
    // slugifies to the same base is a genuine collision and keeps suffixing.
    let path = resolve_article_path(temp.path(), "Durable compile", ArticleKind::Topic);

    assert_eq!(path, directory.join("durable-compile-2.md"));
}

#[test]
fn resolve_article_path_treats_page_without_frontmatter_as_collision() {
    let temp = tempfile::tempdir().expect("tempdir");
    let directory = temp.path().join("knowledge/topics");
    fs::create_dir_all(&directory).expect("topics dir");
    fs::write(
        directory.join("durable-compile.md"),
        "# Durable Compile\n\nHand-authored page without frontmatter.\n",
    )
    .expect("existing page written");

    // A page whose identity cannot be read is never claimed for overwrite.
    let path = resolve_article_path(temp.path(), "Durable Compile", ArticleKind::Topic);

    assert_eq!(path, directory.join("durable-compile-2.md"));
}

#[test]
fn explicit_reserved_target_page_is_refused() {
    let temp = tempfile::tempdir().expect("tempdir");
    fs::create_dir_all(temp.path().join("knowledge/concepts")).expect("create concepts dir");
    let input = empty_input("Claude", ArticleKind::Concept);

    let error = synthesize_article(
        temp.path(),
        &input,
        temp.path().join("knowledge/concepts/claude.md"),
        &ExplainerGeneration::Skipped,
    )
    .expect_err("reserved target page refused");

    assert_eq!(error.code(), "invalid_input");
}

#[test]
fn source_page_paths_reserve_article_path() {
    let temp = tempfile::tempdir().expect("tempdir");
    let article_path = temp.path().join("knowledge/sources/collision.md");
    let sources = vec![SynthesisSource {
        title: "Collision".to_string(),
        path: PathBuf::from("raw/collision.md"),
        chunks: Vec::new(),
        existing_page: None,
    }];

    let paths = source_page_paths(temp.path(), &article_path, &sources);

    assert_ne!(paths[0], article_path);
    assert!(paths[0].starts_with(temp.path().join("knowledge/sources")));
}

#[test]
fn source_page_paths_reuse_existing_digest_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let digest = temp.path().join("knowledge/sources/gwiki-source-1.md");
    let article_path = temp.path().join("knowledge/topics/reuse.md");
    let sources = vec![
        SynthesisSource {
            title: "Session digest".to_string(),
            path: PathBuf::from("raw/sessions/digest.md"),
            chunks: Vec::new(),
            existing_page: Some(digest.clone()),
        },
        SynthesisSource {
            title: "Fresh note".to_string(),
            path: PathBuf::from("raw/research/fresh.md"),
            chunks: Vec::new(),
            existing_page: None,
        },
    ];

    let paths = source_page_paths(temp.path(), &article_path, &sources);

    assert_eq!(paths[0], digest);
    assert_eq!(
        paths[1],
        temp.path().join("knowledge/sources/fresh-note.md")
    );
}

#[test]
fn recompile_resolves_stub_for_same_source_identity_in_place() {
    let temp = tempfile::tempdir().expect("tempdir");
    let article_path = temp.path().join("knowledge/topics/hn-roundup.md");
    let mut input = empty_input("HN Roundup", ArticleKind::Topic);
    input.accepted_sources = vec![SynthesisSource {
        title: "48631169".to_string(),
        path: PathBuf::from("raw/src-hn-48631169.md"),
        chunks: vec!["First extract.".to_string()],
        existing_page: None,
    }];

    let first = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("first source pages synthesized");
    assert_eq!(first.len(), 1);
    assert_eq!(
        first[0].path,
        temp.path().join("knowledge/sources/48631169.md")
    );
    write_synthesized_page(
        temp.path(),
        &first[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("first stub written");

    input.handoff_id = "handoff-2".to_string();
    input.accepted_sources[0].chunks = vec!["Updated extract.".to_string()];
    let second = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("recompiled source pages synthesized");

    assert_eq!(second.len(), 1);
    assert_eq!(second[0].path, first[0].path);
    write_synthesized_page(
        temp.path(),
        &second[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("stub updated in place");

    let entries: Vec<String> = fs::read_dir(temp.path().join("knowledge/sources"))
        .expect("sources dir listed")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(entries, vec!["48631169.md".to_string()]);
    let markdown =
        fs::read_to_string(temp.path().join("knowledge/sources/48631169.md")).expect("stub read");
    assert!(markdown.contains("Updated extract."), "{markdown}");
    assert!(
        markdown.contains("source_path: \"raw/src-hn-48631169.md\""),
        "{markdown}"
    );
}

#[test]
fn recompile_resolves_stub_after_source_content_hash_rotates() {
    // #17705: a re-fetched source gets a new `src-<hash>-<location>` id (the
    // 16-hex content hash rotates) for the SAME canonical location. A recompile
    // must land back on the existing stub by its stable location slug instead of
    // minting a slug-suffixed `-2` sibling and leaking the source as uncompiled.
    let temp = tempfile::tempdir().expect("tempdir");
    let article_path = temp.path().join("knowledge/topics/gist-roundup.md");
    let mut input = empty_input("Gist Roundup", ArticleKind::Topic);
    input.accepted_sources = vec![SynthesisSource {
        title: "mgsloan pre-commit gist".to_string(),
        path: PathBuf::from("raw/src-b75e5eacb3d718ef-https-gist-github-com-mgsloan-pre-commit.md"),
        chunks: vec!["First extract.".to_string()],
        existing_page: None,
    }];

    let first = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("first source pages synthesized");
    assert_eq!(first.len(), 1);
    let stub_path = first[0].path.clone();
    write_synthesized_page(
        temp.path(),
        &first[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("first stub written");

    // Content re-fetched: hash prefix rotates, same location slug.
    input.handoff_id = "handoff-2".to_string();
    input.accepted_sources[0].path =
        PathBuf::from("raw/src-1ca0bb24edbe42ac-https-gist-github-com-mgsloan-pre-commit.md");
    input.accepted_sources[0].chunks = vec!["Updated extract.".to_string()];

    let second = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("recompiled source pages synthesized");
    assert_eq!(second.len(), 1);
    assert_eq!(
        second[0].path, stub_path,
        "recompile after hash rotation must reuse the existing stub"
    );
    write_synthesized_page(
        temp.path(),
        &second[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("stub updated in place");

    let entries: Vec<String> = fs::read_dir(temp.path().join("knowledge/sources"))
        .expect("sources dir listed")
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(entries.len(), 1, "no -2 sibling minted: {entries:?}");
    let markdown = fs::read_to_string(&stub_path).expect("stub read");
    assert!(markdown.contains("Updated extract."), "{markdown}");
}

#[test]
fn recompile_reuses_legacy_stub_without_source_path_frontmatter() {
    // The real #17705 vault case: a stub written before the `source_path`
    // frontmatter key (#17596) records the raw path only in its `Source path:`
    // body line, AND the source content hash later rotated. A recompile must
    // resolve back to that legacy stub by its stable location slug.
    let temp = tempfile::tempdir().expect("tempdir");
    let sources_dir = temp.path().join("knowledge/sources");
    fs::create_dir_all(&sources_dir).expect("sources dir");
    let legacy = concat!(
        "---\n",
        "title: \"Gist\"\n",
        "source_kind: \"source_note\"\n",
        "synthesis_mode: \"source\"\n",
        "---\n\n",
        "# Gist\n\n",
        "Source path: `raw/src-b75e5eacb3d718ef-https-gist-example-com-a.md`\n\n",
        "## Extracts\n\n- old\n",
    );
    fs::write(sources_dir.join("gist.md"), legacy).expect("legacy stub written");

    let article_path = temp.path().join("knowledge/topics/roundup.md");
    let mut input = empty_input("Roundup", ArticleKind::Topic);
    input.accepted_sources = vec![SynthesisSource {
        title: "Gist".to_string(),
        path: PathBuf::from("raw/src-1ca0bb24edbe42ac-https-gist-example-com-a.md"),
        chunks: vec!["new".to_string()],
        existing_page: None,
    }];

    let pages = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("source pages synthesized");
    assert_eq!(pages.len(), 1);
    assert_eq!(
        pages[0].path,
        sources_dir.join("gist.md"),
        "recompile must reuse the legacy stub, not mint a -2 sibling"
    );
}

#[test]
fn source_page_paths_suffix_only_for_different_source_identity() {
    let temp = tempfile::tempdir().expect("tempdir");
    let article_path = temp.path().join("knowledge/topics/hn-roundup.md");
    let mut input = empty_input("HN Roundup", ArticleKind::Topic);
    input.accepted_sources = vec![SynthesisSource {
        title: "Release notes".to_string(),
        path: PathBuf::from("raw/src-first.md"),
        chunks: vec!["First extract.".to_string()],
        existing_page: None,
    }];
    let first = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("first source pages synthesized");
    write_synthesized_page(
        temp.path(),
        &first[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("first stub written");

    // Same title, different raw source: a genuine collision keeps suffixing.
    let sources = vec![SynthesisSource {
        title: "Release notes".to_string(),
        path: PathBuf::from("raw/src-second.md"),
        chunks: Vec::new(),
        existing_page: None,
    }];

    let paths = source_page_paths(temp.path(), &article_path, &sources);

    assert_eq!(
        paths[0],
        temp.path().join("knowledge/sources/release-notes-2.md")
    );
}

#[test]
fn recompile_stub_merges_used_by_links_across_articles() {
    let temp = tempfile::tempdir().expect("tempdir");
    let source = SynthesisSource {
        title: "Shared note".to_string(),
        path: PathBuf::from("raw/src-shared.md"),
        chunks: vec!["Shared extract.".to_string()],
        existing_page: None,
    };
    let mut first_input = empty_input("First Topic", ArticleKind::Topic);
    first_input.accepted_sources = vec![source.clone()];
    let first_article = temp.path().join("knowledge/topics/first-topic.md");
    let first = synthesize_source_pages(temp.path(), &first_input, &first_article)
        .expect("first source pages synthesized");
    write_synthesized_page(
        temp.path(),
        &first[0],
        WritePolicy::AllowOverwriteAfterMerge,
    )
    .expect("first stub written");

    let mut second_input = empty_input("Second Topic", ArticleKind::Topic);
    second_input.accepted_sources = vec![source];
    let second_article = temp.path().join("knowledge/topics/second-topic.md");
    let second = synthesize_source_pages(temp.path(), &second_input, &second_article)
        .expect("second source pages synthesized");

    assert_eq!(second[0].path, first[0].path);
    assert!(
        second[0]
            .markdown
            .contains("- [[knowledge/topics/first-topic|First Topic]]"),
        "{}",
        second[0].markdown
    );
    assert!(
        second[0]
            .markdown
            .contains("- [[knowledge/topics/second-topic|Second Topic]]"),
        "{}",
        second[0].markdown
    );
}

#[test]
fn synthesize_source_pages_skips_sources_with_existing_digest() {
    let temp = tempfile::tempdir().expect("tempdir");
    let digest = temp.path().join("knowledge/sources/gwiki-source-1.md");
    let mut input = empty_input("Reuse", ArticleKind::Concept);
    input.accepted_sources = vec![
        SynthesisSource {
            title: "Session digest".to_string(),
            path: PathBuf::from("raw/sessions/digest.md"),
            chunks: vec!["Digest extract.".to_string()],
            existing_page: Some(digest),
        },
        SynthesisSource {
            title: "Fresh note".to_string(),
            path: PathBuf::from("raw/research/fresh.md"),
            chunks: vec!["Fresh extract.".to_string()],
            existing_page: None,
        },
    ];
    let article_path = temp.path().join("knowledge/concepts/reuse.md");

    let pages = synthesize_source_pages(temp.path(), &input, &article_path)
        .expect("source pages synthesized");

    assert_eq!(pages.len(), 1);
    assert_eq!(pages[0].title, "Fresh note");
    assert_eq!(
        pages[0].path,
        temp.path().join("knowledge/sources/fresh-note.md")
    );
}

#[test]
fn frontmatter_renders_aliases_and_extra_tags() {
    let temp = tempfile::tempdir().expect("tempdir");
    let mut input = empty_input("Gcode", ArticleKind::Concept);
    input.aliases = vec!["gcode".to_string(), "GCode".to_string()];
    input.extra_tags = vec!["entity".to_string()];

    let article_path = resolve_article_path(temp.path(), "Gcode", ArticleKind::Concept);
    let article = synthesize_article(
        temp.path(),
        &input,
        article_path,
        &ExplainerGeneration::Skipped,
    )
    .expect("article synthesized");

    assert!(
        article
            .markdown
            .contains("aliases:\n  - \"gcode\"\n  - \"GCode\"\n"),
        "{}",
        article.markdown
    );
    assert!(
        article
            .markdown
            .contains("tags:\n  - gwiki\n  - compiled\n  - \"entity\"\n"),
        "{}",
        article.markdown
    );
    let parsed =
        crate::frontmatter::parse_frontmatter(&article.markdown).expect("frontmatter parses");
    assert_eq!(parsed.metadata.aliases, vec!["gcode", "GCode"]);
    assert!(parsed.metadata.tags.iter().any(|tag| tag == "entity"));
}

#[test]
fn synthesized_article_rejects_escaping_target_path() {
    let temp = tempfile::tempdir().expect("tempdir");
    let input = empty_input("Escape", ArticleKind::Topic);
    let outside_name = format!(
        "{}-outside.md",
        temp.path()
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("synthesis")
    );
    let target = temp.path().join("..").join(outside_name);

    let error = synthesize_article(temp.path(), &input, target, &ExplainerGeneration::Skipped)
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
