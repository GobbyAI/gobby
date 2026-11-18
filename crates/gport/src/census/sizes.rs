//! Section 1: lines and files per top-level package under `src/gobby`, and three totals.

use super::scan::{GOBBY_PREFIX, TextFile};
use super::table;

const HEADING: &str = "## 1. Package sizes";
const SHARED: &str = "src/gobby/install/shared/";

pub fn section(src: &[TextFile], tests: &[TextFile], packages: &[String]) -> String {
    let mut rows = Vec::new();
    for package in packages {
        let totals = totals(
            src.iter()
                .filter(|file| file.package() == Some(package.as_str())),
        );
        rows.push(row(package, totals));
    }
    let loose = totals(src.iter().filter(|file| {
        file.rel
            .strip_prefix(GOBBY_PREFIX)
            .is_some_and(|inner| !inner.contains('/'))
    }));
    rows.push(row("(files directly under src/gobby)", loose));
    rows.push(row("src (all files)", totals(src.iter())));
    rows.push(row("tests", totals(tests.iter())));
    rows.push(row(
        SHARED.trim_end_matches('/'),
        totals(src.iter().filter(|file| file.rel.starts_with(SHARED))),
    ));
    format!(
        "{HEADING}\n\nText files by top-level package. Lines count the lines of each text file. \
         `__pycache__` is skipped.\n\n{}",
        table(&["Package", "Files", "Lines"], &rows)
    )
}

fn totals<'a>(files: impl Iterator<Item = &'a TextFile>) -> (usize, usize) {
    files.fold((0, 0), |(count, lines), file| {
        (count + 1, lines + file.text.lines().count())
    })
}

fn row(label: &str, (files, lines): (usize, usize)) -> Vec<String> {
    vec![label.to_owned(), files.to_string(), lines.to_string()]
}
