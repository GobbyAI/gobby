//! `gport start`: protocol step 3, checked against the daemon.
//!
//! Read-only. The daemon is reached with GET requests only, and no file is written.
//! A refusal prints one `REFUSE <id>: <reason>` line per failure and returns `Ok(false)`.
//! A daemon, map or git failure is an `Err`, which `main` maps to exit 2.

use std::collections::HashMap;
use std::collections::hash_map::Entry;
use std::io::Write;
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, bail};
use gobby_core::grant::reject_remote_endpoint;
use gobby_core::local_token::apply_bearer_header;
use serde::Deserialize;
use serde_json::Value;

use crate::map::{Chunk, Map};
use crate::{INTEGRATION_BRANCH, MAP_PATH, PORT_EPIC, git};

/// Children per page. The daemon accepts 1 to 1000.
const CHILD_PAGE_LIMIT: usize = 1000;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

pub fn run(root: &Path, chunk_id: &str, out: &mut dyn Write) -> anyhow::Result<bool> {
    let map = Map::load(root)?;
    let daemon = Daemon::connect()?;
    check(root, &map, chunk_id, &daemon, out)
}

/// A child row of the list route. Rows carry no description, so the full record is fetched
/// by `id` when it is needed.
#[derive(Debug, Clone, Deserialize)]
struct Brief {
    id: String,
    title: String,
}

/// The full record of one task, with the fields the start rule reads.
#[derive(Debug, Clone, Deserialize)]
struct Task {
    id: String,
    #[serde(default, rename = "ref")]
    reference: String,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    closed_reason: Option<String>,
    #[serde(default)]
    closed_commit_sha: Option<String>,
    #[serde(default)]
    state: State,
}

#[derive(Debug, Clone, Default, Deserialize)]
struct State {
    #[serde(default)]
    is_closed: bool,
    #[serde(default)]
    closed_reason: Option<String>,
    #[serde(default)]
    closed_commit_sha: Option<String>,
}

impl Task {
    /// The `#<seq>` reference when the daemon sent one, else the task id.
    fn label(&self) -> &str {
        if self.reference.is_empty() {
            &self.id
        } else {
            &self.reference
        }
    }

    fn is_closed(&self) -> bool {
        self.state.is_closed
    }

    fn closed_reason(&self) -> Option<&str> {
        self.closed_reason
            .as_deref()
            .or(self.state.closed_reason.as_deref())
    }

    fn closed_commit(&self) -> Option<&str> {
        self.closed_commit_sha
            .as_deref()
            .or(self.state.closed_commit_sha.as_deref())
            .map(str::trim)
            .filter(|sha| !sha.is_empty())
    }
}

/// Where the start rule reads tasks. The daemon is the production source; tests supply fixtures.
trait TaskSource {
    /// The full record of the task named `reference` (`#<seq>`), or `None` when there is none.
    fn task_by_ref(&self, reference: &str) -> anyhow::Result<Option<Task>>;
    /// Every direct child of the task with id `parent_id`, across all pages, in daemon order.
    fn children(&self, parent_id: &str) -> anyhow::Result<Vec<Brief>>;
    /// The full record of the task with id `id`.
    fn task(&self, id: &str) -> anyhow::Result<Task>;
}

/// The children of the port epic, and the full records read so far.
struct Tree<'a> {
    source: &'a dyn TaskSource,
    children: Vec<Brief>,
    records: HashMap<String, Task>,
}

impl Tree<'_> {
    /// The full record of task `id`, read once per run.
    fn record(&mut self, id: &str) -> anyhow::Result<&Task> {
        match self.records.entry(id.to_owned()) {
            Entry::Occupied(entry) => Ok(entry.into_mut()),
            Entry::Vacant(entry) => {
                let task = self.source.task(id)?;
                Ok(entry.insert(task))
            }
        }
    }

    /// The tasks among `ids` whose description starts with `heading`.
    fn matching(&mut self, ids: &[String], heading: &str) -> anyhow::Result<Vec<Task>> {
        let mut found = Vec::new();
        for id in ids {
            let task = self.record(id)?;
            if task
                .description
                .as_deref()
                .is_some_and(|description| description.starts_with(heading))
            {
                found.push(task.clone());
            }
        }
        Ok(found)
    }

    /// The children whose description starts with `# <chunk id>:`. Children whose title equals
    /// the chunk's map title are read first, and every child only when none of those matches.
    fn find(&mut self, chunk: &Chunk) -> anyhow::Result<Vec<Task>> {
        let heading = format!("# {}:", chunk.id);
        let titled: Vec<String> = self
            .children
            .iter()
            .filter(|row| row.title == chunk.title)
            .map(|row| row.id.clone())
            .collect();
        let found = self.matching(&titled, &heading)?;
        if !found.is_empty() {
            return Ok(found);
        }
        let all: Vec<String> = self.children.iter().map(|row| row.id.clone()).collect();
        self.matching(&all, &heading)
    }
}

/// The one child task that carries `chunk`'s heading. Zero matches, or several, are refusals.
fn locate(tree: &mut Tree<'_>, chunk: &Chunk) -> anyhow::Result<Result<Task, String>> {
    let mut found = tree.find(chunk)?;
    Ok(match found.len() {
        0 => Err(format!(
            "no child of {PORT_EPIC} has a description starting with `# {}:`",
            chunk.id
        )),
        1 => Ok(found.remove(0)),
        count => {
            let labels: Vec<&str> = found.iter().map(Task::label).collect();
            Err(format!(
                "{count} children of {PORT_EPIC} start with `# {}:` ({}), and one task per chunk is required",
                chunk.id,
                labels.join(", ")
            ))
        }
    })
}

enum Verdict {
    Pass(String),
    Refuse(String),
}

/// Judge one dependency: closed as done, and for a non-checkpoint, a commit on the integration
/// branch.
fn judge(root: &Path, dep: &Chunk, task: &Task) -> anyhow::Result<Verdict> {
    let label = task.label();
    if !task.is_closed() {
        return Ok(Verdict::Refuse(format!("task {label} is not closed")));
    }
    if task.closed_reason() != Some("done") {
        let reason = task.closed_reason().unwrap_or("no reason");
        return Ok(Verdict::Refuse(format!(
            "task {label} is closed as {reason}, not done"
        )));
    }
    if dep.kind == "checkpoint" {
        return Ok(Verdict::Pass(format!(
            "{} done (checkpoint, no commit needed)",
            dep.id
        )));
    }
    let Some(sha) = task.closed_commit() else {
        return Ok(Verdict::Refuse(format!(
            "task {label} is done with no closed_commit_sha"
        )));
    };
    if !is_commit_sha(sha) {
        return Ok(Verdict::Refuse(format!(
            "closed_commit_sha {sha:?} is not a commit hash"
        )));
    }
    if !git::succeeds(
        root,
        &["merge-base", "--is-ancestor", sha, INTEGRATION_BRANCH],
    )? {
        return Ok(Verdict::Refuse(format!(
            "commit {sha} is not an ancestor of {INTEGRATION_BRANCH}"
        )));
    }
    Ok(Verdict::Pass(format!("{} done, landed at {sha}", dep.id)))
}

/// A hex object name. Anything else never reaches git as a revision.
fn is_commit_sha(text: &str) -> bool {
    (4..=64).contains(&text.len()) && text.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn refuse(id: &str, reason: &str) -> String {
    format!("REFUSE {id}: {reason}")
}

fn check(
    root: &Path,
    map: &Map,
    chunk_id: &str,
    source: &dyn TaskSource,
    out: &mut dyn Write,
) -> anyhow::Result<bool> {
    let Some(chunk) = map.chunk(chunk_id) else {
        writeln!(
            out,
            "REFUSE {chunk_id}: no chunk with this id in {MAP_PATH}"
        )?;
        return Ok(false);
    };
    let epic = source
        .task_by_ref(PORT_EPIC)?
        .with_context(|| format!("the port epic {PORT_EPIC} is not a task on the daemon"))?;
    let children = source.children(&epic.id)?;
    let mut tree = Tree {
        source,
        children,
        records: HashMap::new(),
    };

    let mut refusals = Vec::new();
    let mut passes = Vec::new();
    if let Err(reason) = locate(&mut tree, chunk)? {
        refusals.push(refuse(&chunk.id, &reason));
    }
    for dep_id in &chunk.depends_on {
        let dep = map.chunk(dep_id).with_context(|| {
            format!(
                "{} depends on {dep_id}, which is not in {MAP_PATH}",
                chunk.id
            )
        })?;
        match locate(&mut tree, dep)? {
            Err(reason) => refusals.push(refuse(&dep.id, &reason)),
            Ok(task) => match judge(root, dep, &task)? {
                Verdict::Pass(line) => passes.push(line),
                Verdict::Refuse(reason) => refusals.push(refuse(&dep.id, &reason)),
            },
        }
    }

    if !refusals.is_empty() {
        for line in &refusals {
            writeln!(out, "{line}")?;
        }
        return Ok(false);
    }
    for line in &passes {
        writeln!(out, "{line}")?;
    }
    let head = git::text(root, &["rev-parse", INTEGRATION_BRANCH])?;
    writeln!(out, "launch base {head}")?;
    Ok(true)
}

/// The daemon over HTTP. Every request is a GET, with the local bearer when one is readable.
struct Daemon {
    base: String,
}

impl Daemon {
    fn connect() -> anyhow::Result<Self> {
        let base = gobby_core::daemon_url::daemon_url().context("cannot resolve the daemon URL")?;
        // The bearer rides on every request, so a remote endpoint is refused before one is sent.
        reject_remote_endpoint(&base)?;
        Ok(Self {
            base: base.trim_end_matches('/').to_owned(),
        })
    }

    /// GET `path` and parse its JSON body. `Ok(None)` is a 404. Any other failure is an error.
    fn get(&self, path: &str) -> anyhow::Result<Option<Value>> {
        let url = format!("{}{path}", self.base);
        let request = apply_bearer_header(ureq::get(&url).timeout(REQUEST_TIMEOUT));
        let response = match request.call() {
            Ok(response) => response,
            Err(ureq::Error::Status(404, _)) => return Ok(None),
            Err(ureq::Error::Status(status, _)) => {
                bail!("the daemon answered GET {path} with status {status}")
            }
            Err(ureq::Error::Transport(error)) => {
                bail!("cannot reach the daemon at {}: {error}", self.base)
            }
        };
        let body = response
            .into_string()
            .with_context(|| format!("cannot read the body of GET {path}"))?;
        serde_json::from_str(&body)
            .with_context(|| format!("GET {path} did not return JSON"))
            .map(Some)
    }
}

fn parse_task(value: Value) -> anyhow::Result<Task> {
    serde_json::from_value(value).context("a task record from the daemon did not parse")
}

impl TaskSource for Daemon {
    fn task_by_ref(&self, reference: &str) -> anyhow::Result<Option<Task>> {
        // `#` opens a URL fragment, so the reference travels as %23.
        let path = format!("/api/tasks/{}", reference.replace('#', "%23"));
        self.get(&path)?.map(parse_task).transpose()
    }

    fn children(&self, parent_id: &str) -> anyhow::Result<Vec<Brief>> {
        let mut rows: Vec<Brief> = Vec::new();
        loop {
            let path = format!(
                "/api/tasks?parent_task_id={parent_id}&limit={CHILD_PAGE_LIMIT}&offset={}",
                rows.len()
            );
            let page = self
                .get(&path)?
                .with_context(|| format!("the daemon has no route for GET {path}"))?;
            let total = page
                .get("total")
                .and_then(Value::as_u64)
                .context("the child listing has no total")?;
            let tasks = page
                .get("tasks")
                .cloned()
                .context("the child listing has no tasks")?;
            let batch: Vec<Brief> = serde_json::from_value(tasks)
                .context("child rows from the daemon did not parse")?;
            if batch.is_empty() {
                break;
            }
            rows.extend(batch);
            if u64::try_from(rows.len()).unwrap_or(u64::MAX) >= total {
                break;
            }
        }
        Ok(rows)
    }

    fn task(&self, id: &str) -> anyhow::Result<Task> {
        let path = format!("/api/tasks/{id}");
        let value = self
            .get(&path)?
            .with_context(|| format!("task {id} is not on the daemon"))?;
        parse_task(value)
    }
}

#[cfg(test)]
#[path = "start/tests.rs"]
mod tests;
