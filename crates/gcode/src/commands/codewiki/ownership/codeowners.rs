use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug)]
pub(super) struct Codeowners {
    entries: Vec<CodeownersEntry>,
}

#[derive(Debug)]
struct CodeownersEntry {
    pattern: String,
    owners: Vec<String>,
}

pub(super) fn read_codeowners(project_root: &Path) -> anyhow::Result<Option<Codeowners>> {
    for relative in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"] {
        let path = project_root.join(relative);
        match std::fs::read_to_string(&path) {
            Ok(raw) => return Ok(Some(parse_codeowners(&raw))),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
    }
    Ok(None)
}

fn parse_codeowners(raw: &str) -> Codeowners {
    let entries = raw
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let mut parts = line.split_whitespace();
            let pattern = parts.next()?.to_string();
            let owners = parts
                .take_while(|part| !part.starts_with('#'))
                .map(str::to_string)
                .collect::<Vec<_>>();
            (!owners.is_empty()).then_some(CodeownersEntry { pattern, owners })
        })
        .collect();
    Codeowners { entries }
}

pub(super) fn declared_owners_for_files(
    codeowners: Option<&Codeowners>,
    files: &[String],
) -> BTreeMap<String, Vec<String>> {
    let mut out = BTreeMap::new();
    let Some(codeowners) = codeowners else {
        return out;
    };
    for file in files {
        if let Some(entry) = codeowners
            .entries
            .iter()
            .rev()
            .find(|entry| codeowners_pattern_matches(&entry.pattern, file))
        {
            out.insert(file.clone(), entry.owners.clone());
        }
    }
    out
}

fn codeowners_pattern_matches(pattern: &str, file: &str) -> bool {
    let normalized = pattern.trim_start_matches('/');
    if normalized.ends_with('/') {
        return file.starts_with(normalized);
    }
    if normalized.contains('*') || normalized.contains('?') || normalized.contains('[') {
        if pattern.starts_with('/') || normalized.contains('/') {
            return match glob::Pattern::new(normalized) {
                Ok(glob) => glob.matches(file),
                Err(error) => {
                    log::warn!(
                        "failed to parse CODEOWNERS pattern {pattern:?} as glob {normalized:?} for file {file:?}: {error}"
                    );
                    false
                }
            };
        }
        return Path::new(file)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| match glob::Pattern::new(normalized) {
                Ok(glob) => glob.matches(name),
                Err(error) => {
                    log::warn!(
                        "failed to parse CODEOWNERS pattern {pattern:?} as basename glob {normalized:?} for file {file:?} name {name:?}: {error}"
                    );
                    false
                }
            });
    }
    if normalized.contains('/') {
        file == normalized || file.starts_with(&format!("{normalized}/"))
    } else {
        Path::new(file).file_name().and_then(|name| name.to_str()) == Some(normalized)
    }
}
