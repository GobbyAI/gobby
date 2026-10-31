//! Whether a policy grant reaches a path. Globs match one path component at a time.
use super::paths::{resolve_nonstrict, subtree_prefix};
use std::{
    ffi::OsStr,
    path::{Component, Path},
};

const WILDCARDS: [char; 4] = ['*', '?', '[', '{'];

/// Whether the grant `entry` reaches `target`. A plain entry covers everything under it. A glob
/// covers `target` only when its pattern matches `target` or one of its ancestors at the
/// pattern's depth, so `<tmp>/xcrun_db-*` does not reach a sibling such as `<tmp>/gobby`.
pub(super) fn reaches(entry: &str, target: &Path) -> bool {
    let head = subtree_prefix(entry);
    let prefix = resolve_nonstrict(Path::new(&head));
    if !entry.contains(WILDCARDS) {
        return target.starts_with(&prefix);
    }
    let tail = entry[head.trim_end_matches('/').len()..].trim_start_matches('/');
    let parts: Vec<&str> = tail.split('/').filter(|part| !part.is_empty()).collect();
    // `**` spans any number of components; without modeling it, the literal prefix decides.
    if parts.iter().any(|part| part.contains("**")) {
        return target.starts_with(&prefix);
    }
    let literal = normal_names(&prefix);
    let names = normal_names(target);
    names.len() >= literal.len()
        && literal.iter().zip(&names).all(|(want, have)| want == have)
        && parts
            .iter()
            .zip(&names[literal.len()..])
            .all(|(pattern, name)| name_matches(pattern, name))
}

fn normal_names(path: &Path) -> Vec<&OsStr> {
    path.components()
        .filter_map(|component| match component {
            Component::Normal(name) => Some(name),
            _ => None,
        })
        .collect()
}

fn name_matches(pattern: &str, name: &OsStr) -> bool {
    // Bracket and brace classes are not modeled, so such a component may match.
    if pattern.contains(['[', '{']) {
        return true;
    }
    let Some(name) = name.to_str() else {
        return true;
    };
    let pattern: Vec<char> = pattern.chars().collect();
    let name: Vec<char> = name.chars().collect();
    wildcard_match(&pattern, &name)
}

fn wildcard_match(pattern: &[char], name: &[char]) -> bool {
    match pattern.split_first() {
        None => name.is_empty(),
        // `?` stands for any run too: a glob reaches a target by component shape, not exact length.
        Some((&('*' | '?'), rest)) => {
            (0..=name.len()).any(|skip| wildcard_match(rest, &name[skip..]))
        }
        Some((&literal, rest)) => name
            .split_first()
            .is_some_and(|(&first, tail)| first == literal && wildcard_match(rest, tail)),
    }
}

#[cfg(test)]
mod tests {
    use super::reaches;
    use std::path::Path;

    #[test]
    fn plain_entry_reaches_only_its_subtree() {
        assert!(reaches("/srv/a", Path::new("/srv/a/b")));
        assert!(!reaches("/srv/a", Path::new("/srv/ab")));
        assert!(!reaches("/srv/a", Path::new("/srv")));
    }

    #[test]
    fn glob_reaches_when_shared_components_match() {
        assert!(reaches("/srv/gobby*", Path::new("/srv/gobby/g")));
        assert!(reaches("/srv/gob?y", Path::new("/srv/gobby/g")));
        assert!(reaches("/srv/gob?", Path::new("/srv/gobby/g")));
        assert!(reaches("/srv/*/g", Path::new("/srv/gobby/g")));
        assert!(reaches("/srv/*/g", Path::new("/srv")));
        assert!(reaches("/srv/*/x", Path::new("/srv/gobby")));
        assert!(!reaches("/srv/xcrun_db-*", Path::new("/srv/gobby/g")));
    }

    #[test]
    fn unmodeled_syntax_and_globstar_reach_conservatively() {
        assert!(reaches("/srv/{a,b}", Path::new("/srv/c")));
        assert!(reaches("/srv/[ab]", Path::new("/srv/c")));
        assert!(reaches("/srv/**", Path::new("/srv/gobby")));
    }
}
