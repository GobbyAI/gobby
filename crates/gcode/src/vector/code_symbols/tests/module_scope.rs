#[test]
fn lifecycle_http_scoped_to_module() {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let src_dir = manifest_dir.join("src");
    let mut offenders = Vec::new();

    fn visit(
        path: &std::path::Path,
        src_dir: &std::path::Path,
        offenders: &mut Vec<std::path::PathBuf>,
    ) {
        for entry in std::fs::read_dir(path).expect("read source directory") {
            let entry = entry.expect("source entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path, src_dir, offenders);
                continue;
            }
            if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                continue;
            }
            let source = std::fs::read_to_string(&path).expect("read source file");
            let searchable_source = rust_code_without_comments_and_literals(&source);
            let lifecycle_rest = [
                "/points/delete",
                "points/delete",
                "collections/{collection}",
                "/collections/{collection}",
            ];
            let rel_path = path.strip_prefix(src_dir).unwrap_or(&path);
            let allowed_module_dir = std::path::Path::new("vector").join("code_symbols");
            let allowed_facade = std::path::Path::new("vector").join("code_symbols.rs");
            if lifecycle_rest
                .iter()
                .any(|needle| searchable_source.contains(needle))
                && rel_path != allowed_facade
                && !rel_path.starts_with(&allowed_module_dir)
            {
                offenders.push(path);
            }
        }
    }

    visit(&src_dir, &src_dir, &mut offenders);
    assert!(
        offenders.is_empty(),
        "Qdrant lifecycle REST must stay scoped to vector/code_symbols module: {offenders:?}"
    );
}

fn rust_code_without_comments_and_literals(source: &str) -> String {
    let bytes = source.as_bytes();
    let mut output = String::with_capacity(source.len());
    let mut index = 0;

    while index < bytes.len() {
        if bytes.get(index..index + 2) == Some(b"//") {
            let start = index;
            index += 2;
            while index < bytes.len() && bytes[index] != b'\n' {
                index += 1;
            }
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes.get(index..index + 2) == Some(b"/*") {
            let start = index;
            index = skip_block_comment(bytes, index + 2);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if let Some((prefix_len, hashes)) = raw_string_prefix(bytes, index) {
            let start = index;
            index += prefix_len;
            while index < bytes.len() {
                if bytes[index] == b'"' && raw_hashes_match(bytes, index + 1, hashes) {
                    index += 1 + hashes;
                    break;
                }
                index += 1;
            }
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes.get(index..index + 2) == Some(b"b\"") {
            let start = index;
            index = skip_quoted_string(bytes, index + 2);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes[index] == b'"' {
            let start = index;
            index = skip_quoted_string(bytes, index + 1);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if let Some(end) = char_literal_end(bytes, index) {
            push_masked(&mut output, &bytes[index..end]);
            index = end;
            continue;
        }

        output.push(bytes[index] as char);
        index += 1;
    }

    output
}

fn skip_block_comment(bytes: &[u8], mut index: usize) -> usize {
    let mut depth = 1usize;
    while index + 1 < bytes.len() {
        match &bytes[index..index + 2] {
            b"/*" => {
                depth += 1;
                index += 2;
            }
            b"*/" => {
                depth -= 1;
                index += 2;
                if depth == 0 {
                    return index;
                }
            }
            _ => index += 1,
        }
    }
    bytes.len()
}

fn raw_string_prefix(bytes: &[u8], index: usize) -> Option<(usize, usize)> {
    let raw_start = match bytes.get(index) {
        Some(b'r') => index,
        Some(b'b') if bytes.get(index + 1) == Some(&b'r') => index + 1,
        _ => return None,
    };
    let mut cursor = raw_start + 1;
    while bytes.get(cursor) == Some(&b'#') {
        cursor += 1;
    }
    (bytes.get(cursor) == Some(&b'"')).then_some((cursor - index + 1, cursor - raw_start - 1))
}

fn raw_hashes_match(bytes: &[u8], start: usize, hashes: usize) -> bool {
    start + hashes <= bytes.len()
        && bytes[start..start + hashes]
            .iter()
            .all(|byte| *byte == b'#')
}

fn skip_quoted_string(bytes: &[u8], mut index: usize) -> usize {
    let mut escaped = false;
    while index < bytes.len() {
        let byte = bytes[index];
        index += 1;
        if escaped {
            escaped = false;
        } else if byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            break;
        }
    }
    index
}

fn char_literal_end(bytes: &[u8], index: usize) -> Option<usize> {
    let start = if bytes.get(index) == Some(&b'\'') {
        index
    } else if bytes.get(index..index + 2) == Some(b"b'") {
        index + 1
    } else {
        return None;
    };
    let cursor = match bytes.get(start + 1) {
        Some(b'\\') => escaped_char_literal_payload_end(bytes, start + 2)?,
        Some(_) => {
            let rest = std::str::from_utf8(&bytes[start + 1..]).ok()?;
            start + 1 + rest.chars().next()?.len_utf8()
        }
        None => return None,
    };
    (bytes.get(cursor) == Some(&b'\'')).then_some(cursor + 1)
}

fn escaped_char_literal_payload_end(bytes: &[u8], index: usize) -> Option<usize> {
    match bytes.get(index)? {
        b'x' => (index + 3 <= bytes.len()).then_some(index + 3),
        b'u' if bytes.get(index + 1) == Some(&b'{') => {
            let close = bytes[index + 2..].iter().position(|byte| *byte == b'}')?;
            Some(index + 2 + close + 1)
        }
        _ => Some(index + 1),
    }
}

fn push_masked(output: &mut String, bytes: &[u8]) {
    for byte in bytes {
        if *byte == b'\n' {
            output.push('\n');
        } else {
            output.push(' ');
        }
    }
}

#[test]
fn rust_literal_mask_handles_long_char_escapes() {
    let source = r#"let emoji = '\u{1F600}';
let byte = b'\x7f';
call_real_code();
"#;

    let masked = rust_code_without_comments_and_literals(source);

    assert!(!masked.contains(r"\u{1F600}"));
    assert!(!masked.contains(r"\x7f"));
    assert!(masked.contains("call_real_code();"));
}

#[test]
fn routes_through_gobby_core_qdrant() {
    let source = [
        include_str!("../qdrant.rs"),
        include_str!("../lifecycle.rs"),
    ]
    .join("\n");
    assert!(source.contains("gobby_core::config::resolve_qdrant_config"));
    assert!(source.contains("gobby_core::qdrant::with_qdrant"));
    assert!(source.contains("gobby_core::qdrant::collection_name"));
    assert!(source.contains("CollectionScope::Custom"));
    assert!(source.contains("gobby_core::qdrant::search"));
    assert!(source.contains("gobby_core::qdrant::upsert"));
}
