use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

pub(in crate::index::import_resolution) fn load_js_external_packages(
    root_path: &Path,
) -> HashSet<String> {
    let package_json = root_path.join("package.json");
    let Ok(contents) = std::fs::read_to_string(package_json) else {
        return HashSet::new();
    };
    let Ok(json) = serde_json::from_str::<serde_json::Value>(&contents) else {
        return HashSet::new();
    };

    let mut packages = HashSet::new();
    for field in [
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
        "bundledDependencies",
        "bundleDependencies",
    ] {
        let Some(value) = json.get(field) else {
            continue;
        };
        if let Some(map) = value.as_object() {
            packages.extend(map.keys().cloned());
        } else if let Some(array) = value.as_array() {
            packages.extend(
                array
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_owned)),
            );
        }
    }
    packages
}

pub(in crate::index::import_resolution) fn load_js_self_package_name(
    root_path: &Path,
) -> Option<String> {
    let package_json = root_path.join("package.json");
    let contents = std::fs::read_to_string(package_json).ok()?;
    let json = serde_json::from_str::<serde_json::Value>(&contents).ok()?;
    json.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

pub(in crate::index::import_resolution) fn load_go_module_path(root_path: &Path) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("go.mod")).ok()?;
    contents.lines().find_map(|line| {
        let line = line.trim();
        line.strip_prefix("module ")
            .map(str::trim)
            .filter(|module| !module.is_empty())
            .map(ToOwned::to_owned)
    })
}

/// Index every discovered Go source file by its project-relative package
/// directory. `go_candidate_files` consults this so a local selector call
/// `pkg.Fn()` can resolve against any file in the imported package directory,
/// matching Go's directory-granular package model.
pub(in crate::index::import_resolution) fn build_go_package_files(
    root_path: &Path,
    candidate_files: &[PathBuf],
) -> HashMap<String, Vec<String>> {
    let mut packages: HashMap<String, Vec<String>> = HashMap::new();
    let Ok(root_abs) = root_path.canonicalize() else {
        return packages;
    };
    for path in candidate_files {
        let ext = path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default();
        if ext != "go" {
            continue;
        }
        let Some(rel) = canonical_relative_path(path, &root_abs) else {
            continue;
        };
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        let dir = rel
            .parent()
            .map(|parent| parent.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        packages.entry(dir).or_default().push(rel_str);
    }
    for files in packages.values_mut() {
        files.sort();
        files.dedup();
    }
    packages
}

fn canonical_relative_path(path: &Path, root_abs: &Path) -> Option<PathBuf> {
    let abs = path.canonicalize().ok()?;
    abs.strip_prefix(root_abs).ok().map(Path::to_path_buf)
}

pub(in crate::index::import_resolution) fn load_rust_external_crates(
    root_path: &Path,
) -> HashSet<String> {
    let mut crates = HashSet::new();
    for manifest in rust_manifest_paths(root_path) {
        let Ok(contents) = std::fs::read_to_string(manifest) else {
            continue;
        };
        let Ok(cargo_toml) = toml::from_str::<toml::Table>(&contents) else {
            continue;
        };

        for section in ["dependencies", "dev-dependencies", "build-dependencies"] {
            collect_rust_dependency_keys(cargo_toml.get(section), &mut crates);
        }

        if let Some(targets) = cargo_toml.get("target").and_then(toml::Value::as_table) {
            for target in targets.values() {
                for section in ["dependencies", "dev-dependencies", "build-dependencies"] {
                    collect_rust_dependency_keys(target.get(section), &mut crates);
                }
            }
        }
    }

    crates
}

fn rust_manifest_paths(root_path: &Path) -> Vec<PathBuf> {
    let root_manifest = root_path.join("Cargo.toml");
    let mut manifests = vec![root_manifest.clone()];
    let Ok(contents) = std::fs::read_to_string(&root_manifest) else {
        return manifests;
    };
    let Ok(cargo_toml) = toml::from_str::<toml::Table>(&contents) else {
        return manifests;
    };
    let Some(members) = cargo_toml
        .get("workspace")
        .and_then(|workspace| workspace.get("members"))
        .and_then(toml::Value::as_array)
    else {
        return manifests;
    };
    for member in members.iter().filter_map(toml::Value::as_str) {
        if member.contains('*') {
            let pattern = root_path.join(member).join("Cargo.toml");
            let Some(pattern) = pattern.to_str() else {
                continue;
            };
            let Ok(entries) = glob::glob(pattern) else {
                log::debug!(
                    "invalid Cargo workspace glob member `{member}` under {}",
                    root_path.display()
                );
                continue;
            };
            manifests.extend(entries.flatten().filter(|path| path.is_file()));
            continue;
        }
        let manifest = root_path.join(member).join("Cargo.toml");
        if manifest.is_file() {
            manifests.push(manifest);
        }
    }
    manifests.sort();
    manifests.dedup();
    manifests
}

pub(in crate::index::import_resolution) fn load_rust_self_crate_name(
    root_path: &Path,
) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("Cargo.toml")).ok()?;
    let cargo_toml = toml::from_str::<toml::Table>(&contents).ok()?;
    cargo_toml
        .get("package")
        .and_then(|package| package.get("name"))
        .and_then(toml::Value::as_str)
        .map(normalize_rust_crate_name)
        .filter(|name| !name.is_empty())
}

fn collect_rust_dependency_keys(value: Option<&toml::Value>, crates: &mut HashSet<String>) {
    let Some(table) = value.and_then(toml::Value::as_table) else {
        return;
    };
    for name in table.keys() {
        let name = normalize_rust_crate_name(name);
        if !name.is_empty() {
            crates.insert(name);
        }
    }
}

fn normalize_rust_crate_name(name: &str) -> String {
    name.trim().replace('-', "_")
}

pub(in crate::index::import_resolution) fn load_dart_external_packages(
    root_path: &Path,
) -> HashSet<String> {
    let Ok(contents) = std::fs::read_to_string(root_path.join("pubspec.yaml")) else {
        return HashSet::new();
    };
    let Ok(yaml) = serde_yaml::from_str::<serde_yaml::Value>(&contents) else {
        return HashSet::new();
    };

    let mut packages = HashSet::new();
    for field in ["dependencies", "dev_dependencies", "dependency_overrides"] {
        if let Some(map) = yaml.get(field).and_then(|value| value.as_mapping()) {
            for key in map.keys().filter_map(|key| key.as_str()) {
                if !key.is_empty() && key != "sdk" {
                    packages.insert(key.to_string());
                }
            }
        }
    }
    packages
}

pub(in crate::index::import_resolution) fn load_dart_self_package_name(
    root_path: &Path,
) -> Option<String> {
    let contents = std::fs::read_to_string(root_path.join("pubspec.yaml")).ok()?;
    let yaml = serde_yaml::from_str::<serde_yaml::Value>(&contents).ok()?;
    yaml.get("name")
        .and_then(|value| value.as_str())
        .map(ToOwned::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[cfg(unix)]
    fn symlink_dir(target: &Path, link: &Path) -> std::io::Result<()> {
        std::os::unix::fs::symlink(target, link)
    }

    #[cfg(windows)]
    fn symlink_dir(target: &Path, link: &Path) -> std::io::Result<()> {
        std::os::windows::fs::symlink_dir(target, link)
    }

    #[test]
    fn go_package_files_canonicalize_symlinked_candidates() {
        let project = tempfile::tempdir().expect("temp project");
        let real_dir = project.path().join("real/store");
        fs::create_dir_all(&real_dir).expect("create real Go package");
        fs::write(real_dir.join("query.go"), "package store\n").expect("write Go file");
        let link = project.path().join("linked");
        if let Err(err) = symlink_dir(&project.path().join("real"), &link) {
            eprintln!("skipping symlink canonicalization test: {err}");
            return;
        }

        let packages = build_go_package_files(project.path(), &[link.join("store/query.go")]);

        assert_eq!(
            packages.get("real/store"),
            Some(&vec!["real/store/query.go".to_string()])
        );
        assert!(!packages.contains_key("linked/store"));
    }
}
