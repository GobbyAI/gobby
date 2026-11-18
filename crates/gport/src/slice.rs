//! `gport check-slice`: one chunk's commits and paths, and the rate report.
//!
//! `check` reads its authority (the chunk's fields, the frozen list and the owned rows) at the
//! merge base with the integration branch, and the Found Work lines and status file at HEAD. It
//! makes no daemon call. `--report` reads the working tree throughout.

mod status;

use std::fs;
use std::io::{self, Write};
use std::path::Path;

use anyhow::{Context, bail};

use crate::map::Map;
use crate::rows::{self, Selectors};
use crate::{FROZEN_LEDGER_PATH, INTEGRATION_BRANCH, MAP_PATH, RECORD_PATH, git, record};
use status::State;

/// A chunk whose python_edits starts with one of these admits no Python under src/gobby/.
const NO_PYTHON_PREFIXES: [&str; 3] = ["None", "tests/ only", "tests/ and scripts/ only"];
const PYTHON_PREFIX: &str = "src/gobby/";
const VERSION_PINS: &str = "src/gobby/install/version_pins.py";
const ROOT_MANIFEST: &str = "Cargo.toml";
const DAEMON_MANIFEST: &str = "crates/gdaemon/Cargo.toml";
/// Crates whose `[package]` version only a cutover chunk may change.
const VERSIONED_CRATES: [&str; 5] = [
    "crates/gcode",
    "crates/ghook",
    "crates/gmcp",
    "crates/gclient",
    "crates/gterminal",
];
const CUTOVER_KIND: &str = "cutover";
/// The length of an abbreviated SHA: its display length, and the shortest prefix that done-row
/// evidence may name.
const SHA_PREFIX: usize = 7;

/// One commit of `0.9.0..HEAD`.
struct Commit {
    sha: String,
    subject: String,
}

/// Checks one chunk's slice in its worktree. Every refusal is one `REFUSE` line on `out`; the
/// result is `Ok(false)` when there is any.
pub fn check(
    root: &Path,
    chunk_id: &str,
    tasks: &[String],
    out: &mut dyn Write,
) -> anyhow::Result<bool> {
    let tags = task_tags(tasks)?;
    let Some(base) = merge_base(root)? else {
        writeln!(
            out,
            "REFUSE {INTEGRATION_BRANCH} and HEAD do not have exactly one merge base"
        )?;
        return Ok(false);
    };
    let commits = range_commits(root)?;
    let paths = changed_paths(root, &base)?;
    let mut refusals = Vec::new();

    let map_bytes = git::show(root, &base, MAP_PATH)?
        .with_context(|| format!("{MAP_PATH} is missing at {}", short(&base)))?;
    let map = Map::from_bytes(&map_bytes)?;
    let ledger = text_at(root, &base, FROZEN_LEDGER_PATH)?
        .with_context(|| format!("{FROZEN_LEDGER_PATH} is missing at {}", short(&base)))?;
    let frozen = text_at(root, &base, RECORD_PATH)?
        .as_deref()
        .map(record::frozen_files)
        .unwrap_or_default();
    let found = text_at(root, "HEAD", RECORD_PATH)?
        .map(|text| record::found_work_lines(&text, chunk_id))
        .unwrap_or_default();

    let ledger_rows = rows::rows(&ledger);
    let selectors = Selectors::compile(&map.chunks);
    let owned = selectors.try_owned_by(chunk_id, &ledger_rows)?;
    let chunk = map.chunk(chunk_id);
    if chunk.is_none() {
        refusals.push(format!(
            "chunk {chunk_id} is not in {MAP_PATH} at {}",
            short(&base)
        ));
    }
    let python_refused = chunk.is_some_and(|chunk| {
        NO_PYTHON_PREFIXES
            .iter()
            .any(|prefix| chunk.python_edits.starts_with(*prefix))
    });
    let cutover = chunk.is_some_and(|chunk| chunk.kind == CUTOVER_KIND);

    for commit in &commits {
        if !tags
            .iter()
            .any(|tag| commit.subject.starts_with(tag.as_str()))
        {
            refusals.push(format!(
                "commit {} \"{}\" does not start with {} for any --task",
                short(&commit.sha),
                commit.subject,
                tags.join(" or ")
            ));
        }
    }

    for path in paths.iter().map(String::as_str) {
        let quoted = format!("`{path}`");
        let named = found.iter().any(|line| line.contains(quoted.as_str()));
        if python_refused
            && path.starts_with(PYTHON_PREFIX)
            && path.ends_with(".py")
            && path != VERSION_PINS
            && !named
        {
            refusals.push(format!(
                "{path} is Python under {PYTHON_PREFIX}, which the python_edits of {chunk_id} \
                 does not allow; a Found Work line under {chunk_id} must name it"
            ));
        }
        if frozen.iter().any(|entry| entry.as_str() == path) && !named {
            refusals.push(format!(
                "{path} is on the frozen list at {}; a Found Work line under {chunk_id} must \
                 name it",
                short(&base)
            ));
        }
    }

    registration(root, &base, &paths, cutover, &mut refusals)?;

    if let Some(chunk) = chunk {
        for line in Selectors::compile(std::slice::from_ref(chunk)).bad {
            refusals.push(format!(
                "{MAP_PATH} at {}: a selector does not compile: {line}",
                short(&base)
            ));
        }
        check_status(root, chunk_id, &owned, &commits, &mut refusals)?;
    }

    for refusal in &refusals {
        writeln!(out, "REFUSE {refusal}")?;
    }
    if refusals.is_empty() {
        writeln!(
            out,
            "check-slice {chunk_id}: clean; {} commits and {} paths since {}",
            commits.len(),
            paths.len(),
            short(&base)
        )?;
        Ok(true)
    } else {
        writeln!(out, "check-slice {chunk_id}: {} refusals", refusals.len())?;
        Ok(false)
    }
}

/// Prints `(partial+stub)/(done+partial+stub)` over the named chunks' working-tree status files.
/// The result is `Ok(false)` on any refusal, and on a zero denominator.
pub fn report(root: &Path, chunk_ids: &[String], out: &mut dyn Write) -> anyhow::Result<bool> {
    let map = Map::load(root)?;
    let ledger = fs::read_to_string(root.join(FROZEN_LEDGER_PATH))
        .with_context(|| format!("cannot read {FROZEN_LEDGER_PATH}"))?;
    let ledger_rows = rows::rows(&ledger);
    let selectors = Selectors::compile(&map.chunks);
    let mut refusals = Vec::new();
    let (mut numerator, mut denominator) = (0usize, 0usize);

    for chunk_id in chunk_ids {
        let owned = selectors.try_owned_by(chunk_id, &ledger_rows)?;
        if map.chunk(chunk_id).is_none() {
            refusals.push(format!(
                "chunk {chunk_id} is not in {MAP_PATH} in the working tree"
            ));
            continue;
        }
        let path = status::path(chunk_id);
        let bytes = match fs::read(root.join(&path)) {
            Ok(bytes) => bytes,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if !owned.is_empty() {
                    refusals.push(status::refusal(
                        &path,
                        &format!("missing, though {chunk_id} owns {} rows", owned.len()),
                    ));
                }
                continue;
            }
            Err(error) => return Err(error).with_context(|| format!("cannot read {path}")),
        };
        let file = match status::parse(&bytes) {
            Ok(file) => file,
            Err(error) => {
                refusals.push(status::refusal(&path, &format!("does not parse: {error}")));
                continue;
            }
        };
        refusals.extend(status::row_problems(&path, chunk_id, &file, &owned));
        for row in &file.rows {
            match row.state {
                Some(State::Partial | State::Stub) => {
                    numerator += 1;
                    denominator += 1;
                }
                Some(State::Done) => denominator += 1,
                _ => {}
            }
        }
    }

    for refusal in &refusals {
        writeln!(out, "REFUSE {refusal}")?;
    }
    if denominator == 0 {
        writeln!(out, "no attempted items")?;
        return Ok(false);
    }
    writeln!(
        out,
        "(partial+stub)/(done+partial+stub): {numerator}/{denominator}"
    )?;
    Ok(refusals.is_empty())
}

/// The `[gobby-#N]` prefix each `--task` ref admits. `N` and `#N` name the same task.
fn task_tags(tasks: &[String]) -> anyhow::Result<Vec<String>> {
    tasks
        .iter()
        .map(|task| {
            let number = task.strip_prefix('#').unwrap_or(task.as_str());
            if number.is_empty() || !number.bytes().all(|byte| byte.is_ascii_digit()) {
                bail!("--task {task:?} is not a task number; pass N or #N");
            }
            Ok(format!("[gobby-#{number}]"))
        })
        .collect()
}

/// The single merge base of the integration branch and HEAD, or None when there is not exactly
/// one. The checkout, the branch and HEAD are verified first, so a missing one is an input error
/// (Err, exit 2). Only a branch and HEAD that exist with no single merge base is a refusal (None).
fn merge_base(root: &Path) -> anyhow::Result<Option<String>> {
    git::output(root, &["rev-parse", "--git-dir"])
        .with_context(|| format!("{} is not a git repository", root.display()))?;
    let branch = format!("{INTEGRATION_BRANCH}^{{commit}}");
    if git::output_or_none(root, &["rev-parse", "--verify", "--quiet", &branch])?.is_none() {
        bail!(
            "integration branch {INTEGRATION_BRANCH} is missing in {}",
            root.display()
        );
    }
    if git::output_or_none(root, &["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])?.is_none()
    {
        bail!("HEAD has no commit in {}", root.display());
    }
    let args = ["merge-base", "--all", INTEGRATION_BRANCH, "HEAD"];
    let Some(bytes) = git::output_or_none(root, &args)? else {
        return Ok(None);
    };
    let text = String::from_utf8(bytes).context("git merge-base printed text that is not UTF-8")?;
    let mut bases = text.lines();
    match (bases.next(), bases.next()) {
        (Some(base), None) => Ok(Some(base.to_owned())),
        _ => Ok(None),
    }
}

/// The commits of `0.9.0..HEAD`, merges left out.
fn range_commits(root: &Path) -> anyhow::Result<Vec<Commit>> {
    let range = format!("{INTEGRATION_BRANCH}..HEAD");
    let bytes = git::output(
        root,
        &["log", "--no-merges", "--format=%H%x09%s", range.as_str()],
    )?;
    let text = String::from_utf8(bytes).context("git log printed text that is not UTF-8")?;
    Ok(text
        .lines()
        .filter_map(|line| {
            let (sha, subject) = line.split_once('\t')?;
            Some(Commit {
                sha: sha.to_owned(),
                subject: subject.to_owned(),
            })
        })
        .collect())
}

/// Every path that differs between `base` and HEAD. Both names of a rename or copy count.
fn changed_paths(root: &Path, base: &str) -> anyhow::Result<Vec<String>> {
    let bytes = git::output(root, &["diff", "--name-status", "-z", base, "HEAD"])?;
    let text = String::from_utf8(bytes).context("git diff printed paths that are not UTF-8")?;
    let mut fields = text.split('\0').filter(|field| !field.is_empty());
    let mut paths = Vec::new();
    while let Some(status) = fields.next() {
        let count = if status.starts_with(['R', 'C']) { 2 } else { 1 };
        for _ in 0..count {
            if let Some(path) = fields.next() {
                paths.push(path.to_owned());
            }
        }
    }
    Ok(paths)
}

/// The text of `path` at `revision`, or None when that revision has no such path.
fn text_at(root: &Path, revision: &str, path: &str) -> anyhow::Result<Option<String>> {
    match git::show(root, revision, path)? {
        Some(bytes) => {
            Ok(Some(String::from_utf8(bytes).with_context(|| {
                format!("{path} at {revision} is not UTF-8")
            })?))
        }
        None => Ok(None),
    }
}

/// Whether `paths` holds `path` exactly.
fn changed(paths: &[String], path: &str) -> bool {
    paths.iter().any(|changed| changed.as_str() == path)
}

/// The registration protocol. Each manifest that changed is compared at the merge base and HEAD.
fn registration(
    root: &Path,
    base: &str,
    paths: &[String],
    cutover: bool,
    refusals: &mut Vec<String>,
) -> anyhow::Result<()> {
    if changed(paths, ROOT_MANIFEST)
        && let Some((old, new)) = both(root, base, ROOT_MANIFEST, refusals)?
    {
        let (old, new) = (members(&old), members(&new));
        for member in old.iter().filter(|member| !new.contains(member)) {
            refusals.push(format!(
                "{ROOT_MANIFEST}: workspace member {member} was removed"
            ));
        }
    }
    if changed(paths, DAEMON_MANIFEST)
        && let Some((old, new)) = both(root, base, DAEMON_MANIFEST, refusals)?
    {
        let (old, new) = (dependency_keys(&old), dependency_keys(&new));
        for key in old.iter().filter(|key| !new.contains(key)) {
            refusals.push(format!(
                "{DAEMON_MANIFEST}: [dependencies] key {key} was removed"
            ));
        }
    }
    if !cutover {
        for krate in VERSIONED_CRATES {
            let path = format!("{krate}/Cargo.toml");
            if !changed(paths, &path) {
                continue;
            }
            if let Some((old, new)) = both(root, base, &path, refusals)? {
                let (old, new) = (package_version(&old), package_version(&new));
                if old != new {
                    refusals.push(format!(
                        "{path}: [package] version changed from {} to {}; only a cutover chunk \
                         may change it",
                        describe(old.as_ref()),
                        describe(new.as_ref())
                    ));
                }
            }
        }
    }
    Ok(())
}

/// The manifest at the merge base and at HEAD, or None when either side does not parse; the parse
/// error is recorded as a refusal.
fn both(
    root: &Path,
    base: &str,
    path: &str,
    refusals: &mut Vec<String>,
) -> anyhow::Result<Option<(toml::Table, toml::Table)>> {
    let old = manifest(root, base, path, refusals)?;
    let new = manifest(root, "HEAD", path, refusals)?;
    Ok(old.zip(new))
}

/// The manifest at `revision`. An absent file is an empty table.
fn manifest(
    root: &Path,
    revision: &str,
    path: &str,
    refusals: &mut Vec<String>,
) -> anyhow::Result<Option<toml::Table>> {
    let Some(bytes) = git::show(root, revision, path)? else {
        return Ok(Some(toml::Table::new()));
    };
    let parsed = std::str::from_utf8(&bytes)
        .map_err(|error| error.to_string())
        .and_then(|text| toml::from_str::<toml::Table>(text).map_err(|error| error.to_string()));
    match parsed {
        Ok(table) => Ok(Some(table)),
        Err(error) => {
            refusals.push(format!(
                "{path} at {} does not parse: {error}",
                short(revision)
            ));
            Ok(None)
        }
    }
}

/// The workspace members of a root manifest.
fn members(table: &toml::Table) -> Vec<String> {
    table
        .get("workspace")
        .and_then(|workspace| workspace.get("members"))
        .and_then(toml::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|member| member.as_str().map(str::to_owned))
        .collect()
}

/// The keys of a manifest's `[dependencies]` table.
fn dependency_keys(table: &toml::Table) -> Vec<String> {
    table
        .get("dependencies")
        .and_then(toml::Value::as_table)
        .into_iter()
        .flat_map(|dependencies| dependencies.keys().cloned())
        .collect()
}

/// The `[package]` version of a manifest, which may be a table such as `version.workspace = true`.
fn package_version(table: &toml::Table) -> Option<toml::Value> {
    table.get("package")?.get("version").cloned()
}

fn describe(version: Option<&toml::Value>) -> String {
    version.map_or_else(|| "absent".to_owned(), toml::Value::to_string)
}

/// Status file rules for a chunk that the merge base knows: the file is required when it owns rows,
/// and its rows must be exactly the owned rows with each done row's evidence holding.
fn check_status(
    root: &Path,
    chunk_id: &str,
    owned: &[&str],
    commits: &[Commit],
    refusals: &mut Vec<String>,
) -> anyhow::Result<()> {
    let path = status::path(chunk_id);
    let Some(bytes) = git::show(root, "HEAD", &path)? else {
        if !owned.is_empty() {
            refusals.push(status::refusal(
                &path,
                &format!(
                    "missing, though {chunk_id} owns {} rows at the merge base",
                    owned.len()
                ),
            ));
        }
        return Ok(());
    };
    let file = match status::parse(&bytes) {
        Ok(file) => file,
        Err(error) => {
            refusals.push(status::refusal(&path, &format!("does not parse: {error}")));
            return Ok(());
        }
    };
    refusals.extend(status::row_problems(&path, chunk_id, &file, owned));
    for row in file
        .rows
        .iter()
        .filter(|row| row.state == Some(State::Done))
    {
        if let Some(detail) = evidence_problem(root, &row.key, &row.evidence, commits)? {
            refusals.push(status::refusal(&path, &detail));
        }
    }
    Ok(())
}

/// The problem with a done row's evidence, or None when `red <sha> green <sha> test <command>`
/// names two commits of `0.9.0..HEAD` with red an ancestor of green.
fn evidence_problem(
    root: &Path,
    key: &str,
    evidence: &str,
    commits: &[Commit],
) -> anyhow::Result<Option<String>> {
    let Some((red, green)) = status::evidence_shas(evidence) else {
        return Ok(Some(format!(
            "row {key} is done, but its evidence is not `red <sha> green <sha> test <command>`"
        )));
    };
    let red = match resolve(red, commits) {
        Ok(sha) => sha,
        Err(detail) => return Ok(Some(format!("row {key} evidence: red {detail}"))),
    };
    let green = match resolve(green, commits) {
        Ok(sha) => sha,
        Err(detail) => return Ok(Some(format!("row {key} evidence: green {detail}"))),
    };
    if !git::succeeds(root, &["merge-base", "--is-ancestor", red, green])? {
        return Ok(Some(format!(
            "row {key} evidence: red {} is not an ancestor of green {}",
            short(red),
            short(green)
        )));
    }
    Ok(None)
}

/// The full SHA of the one commit of `0.9.0..HEAD` that `token` names as a prefix.
fn resolve<'a>(token: &str, commits: &'a [Commit]) -> Result<&'a str, String> {
    if token.len() < SHA_PREFIX || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("{token:?} is not a commit SHA"));
    }
    let mut hits = commits
        .iter()
        .map(|commit| commit.sha.as_str())
        .filter(|sha| sha.starts_with(token));
    match (hits.next(), hits.next()) {
        (Some(sha), None) => Ok(sha),
        (None, _) => Err(format!("{token} is not in {INTEGRATION_BRANCH}..HEAD")),
        (Some(_), Some(_)) => Err(format!("{token} matches more than one commit")),
    }
}

/// A SHA abbreviated for display.
fn short(sha: &str) -> &str {
    sha.get(..SHA_PREFIX).unwrap_or(sha)
}
