use super::detect_language_from_content_with_paths;

#[test]
fn captured_objc_header_uses_logical_sibling_membership() {
    let logical_paths = ["Sources/Widget.h", "Sources/Widget.m"];

    assert_eq!(
        detect_language_from_content_with_paths(
            "Sources/Widget.h",
            b"void render(void);",
            |path| logical_paths.contains(&path),
        ),
        Some("objc")
    );
    assert_eq!(
        detect_language_from_content_with_paths("Sources/Other.h", b"void render(void);", |path| {
            logical_paths.contains(&path)
        },),
        Some("c")
    );
}
