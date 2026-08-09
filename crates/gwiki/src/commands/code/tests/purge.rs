use super::*;

#[test]
fn purge_removes_generated_docs_and_metadata_only() -> anyhow::Result<()> {
    let temp = tempfile::tempdir()?;
    let out_dir = temp.path();
    write_doc(out_dir, "code/files/src/lib.rs.md", "# Lib\n")?;
    write_doc(out_dir, "code/modules/src.md", "# Src\n")?;
    write_doc(out_dir, "notes/manual.md", "# Manual\n")?;
    write_doc(
        out_dir,
        CODEWIKI_META_PATH,
        r#"{
            "docs": {
                "code/files/src/lib.rs.md": { "source_hashes": {} }
            },
            "generated_docs": ["code/modules/src.md"],
            "ai_mode": "off"
        }"#,
    )?;
    write_doc(out_dir, OWNERSHIP_META_PATH, "{}")?;
    write_doc(out_dir, TRUTH_DIGEST_META_PATH, "{}")?;

    let counts = purge_generated_output(out_dir)?;

    assert_eq!(counts.markdown_removed, 2);
    assert_eq!(counts.metadata_removed, 3);
    assert!(!out_dir.join("code/files/src/lib.rs.md").exists());
    assert!(!out_dir.join("code/modules/src.md").exists());
    assert!(!out_dir.join(CODEWIKI_META_PATH).exists());
    assert!(!out_dir.join(OWNERSHIP_META_PATH).exists());
    assert!(!out_dir.join(TRUTH_DIGEST_META_PATH).exists());
    assert!(out_dir.join("notes/manual.md").exists());
    Ok(())
}

#[test]
fn purge_refuses_unsafe_generated_path_from_metadata() -> anyhow::Result<()> {
    let temp = tempfile::tempdir()?;
    let out_dir = temp.path();
    write_doc(
        out_dir,
        CODEWIKI_META_PATH,
        r#"{
            "docs": {
                "../outside.md": { "source_hashes": {} },
                "code/files/src/lib.rs.md": { "source_hashes": {} }
            },
            "generated_docs": [],
            "ai_mode": "off"
        }"#,
    )?;
    write_doc(out_dir, "code/files/src/lib.rs.md", "# Lib\n")?;

    let error = purge_generated_output(out_dir).expect_err("unsafe path must be rejected");

    assert!(error.to_string().contains("unsafe codewiki path"));
    assert!(out_dir.join("code/files/src/lib.rs.md").exists());
    Ok(())
}
