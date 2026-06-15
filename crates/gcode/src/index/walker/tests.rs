use std::path::{Path, PathBuf};

use super::generated::MINIFIED_JS_MIN_BYTES;
use super::*;

mod classification;
mod discovery;
mod generated;
mod hidden;

fn write_file(root: &Path, rel: &str, contents: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(path, contents).expect("write file");
}

fn rels(root: &Path, paths: Vec<PathBuf>) -> Vec<String> {
    let mut rels: Vec<String> = paths
        .into_iter()
        .map(|path| {
            path.strip_prefix(root)
                .expect("path under root")
                .to_string_lossy()
                .to_string()
        })
        .collect();
    rels.sort();
    rels
}
