use std::collections::BTreeSet;
use std::io::Write;

use postgres::{Client, GenericClient};
use serde::Serialize;

use crate::commands::purge::{BackendConfigs, optional_backend_configs, purge_scope_state};
use crate::project_lock::{PruneProjectLock, try_acquire_prune_lock};
use crate::schema::GWIKI_POSTGRES_TABLES;
use crate::search::SearchScope;
use crate::support::config::qdrant_config_has_url;
use crate::support::postgres::require_postgres_index_readwrite;
use crate::{CommandOutcome, CommandResult, ScopeIdentity, WikiError};

const COMMAND: &str = "gwiki prune";
const AFFECTED_ID_LIMIT: usize = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SweepOutcome {
    Active,
    Deleted,
    AlreadyMissing,
    Busy,
}

#[derive(Debug, Default, Serialize, PartialEq, Eq)]
struct ReconcileTotals {
    scanned: usize,
    active: usize,
    orphaned: usize,
    deleted: usize,
    already_missing: usize,
    busy: usize,
    invalid: usize,
    failed: usize,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct BackendPhase {
    status: &'static str,
    scanned: usize,
}

#[derive(Debug, Serialize)]
struct PruneSummary {
    command: &'static str,
    status: &'static str,
    #[serde(flatten)]
    totals: ReconcileTotals,
    qdrant: BackendPhase,
    falkor: BackendPhase,
    topic_collections: ReconcileTotals,
    affected_scope_ids: Vec<String>,
    invalid_identifiers: Vec<String>,
}

struct PruneDiscovery {
    totals: ReconcileTotals,
    qdrant: BackendPhase,
    falkor: BackendPhase,
    topic_collections: ReconcileTotals,
    orphan_project_ids: Vec<String>,
    orphan_topics: Vec<String>,
    invalid_identifiers: Vec<String>,
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
struct SqlScopes {
    projects: Vec<String>,
    topics: BTreeSet<String>,
}

#[derive(Debug, Default)]
struct SweepTotals {
    active: usize,
    deleted: usize,
    already_missing: usize,
    busy: usize,
    failed: usize,
    affected_scope_ids: Vec<String>,
}

trait PruneBackend {
    fn qdrant_collections(&mut self) -> Result<Option<Vec<String>>, WikiError>;
    fn falkor_scopes(&mut self) -> Result<Option<crate::falkor_graph::FalkorScopes>, WikiError>;
    fn sql_scopes(&mut self) -> Result<SqlScopes, WikiError>;
    fn authority_project_ids(&mut self) -> Result<BTreeSet<String>, WikiError>;
    fn reconcile_project(&mut self, project_id: &str) -> Result<SweepOutcome, WikiError>;
    fn reconcile_topic(&mut self, topic_name: &str) -> Result<SweepOutcome, WikiError>;
}

struct SystemPruneBackend {
    conn: Client,
    configs: BackendConfigs,
    qdrant_project_scopes: BTreeSet<String>,
    qdrant_topic_scopes: BTreeSet<String>,
    falkor_project_scopes: BTreeSet<String>,
    falkor_topic_scopes: BTreeSet<String>,
}

impl SystemPruneBackend {
    fn connect() -> Result<Self, WikiError> {
        let mut conn = require_postgres_index_readwrite(COMMAND)?;
        let configs = optional_backend_configs(&mut conn, COMMAND)?;
        Ok(Self {
            conn,
            configs,
            qdrant_project_scopes: BTreeSet::new(),
            qdrant_topic_scopes: BTreeSet::new(),
            falkor_project_scopes: BTreeSet::new(),
            falkor_topic_scopes: BTreeSet::new(),
        })
    }
}

impl PruneBackend for SystemPruneBackend {
    fn qdrant_collections(&mut self) -> Result<Option<Vec<String>>, WikiError> {
        let Some(config) = self
            .configs
            .qdrant
            .as_ref()
            .filter(|config| qdrant_config_has_url(config))
        else {
            return Ok(None);
        };
        let collections =
            gobby_core::qdrant::list_collections(config).map_err(|error| WikiError::Config {
                detail: format!("failed to enumerate Qdrant collections for gwiki prune: {error}"),
            })?;
        self.qdrant_project_scopes = collections
            .iter()
            .filter_map(|collection| collection.strip_prefix("gwiki_project_"))
            .map(str::to_string)
            .collect();
        self.qdrant_topic_scopes = collections
            .iter()
            .filter_map(|collection| topic_name_from_collection(collection))
            .map(str::to_string)
            .collect();
        Ok(Some(collections))
    }

    fn falkor_scopes(&mut self) -> Result<Option<crate::falkor_graph::FalkorScopes>, WikiError> {
        let scopes = self
            .configs
            .falkor
            .as_ref()
            .map(crate::falkor_graph::list_scopes)
            .transpose()?;
        if let Some(scopes) = &scopes {
            self.falkor_project_scopes = scopes.projects.iter().cloned().collect();
            self.falkor_topic_scopes = scopes.topics.iter().cloned().collect();
        }
        Ok(scopes)
    }

    fn sql_scopes(&mut self) -> Result<SqlScopes, WikiError> {
        collect_sql_scopes(&mut self.conn)
    }

    fn authority_project_ids(&mut self) -> Result<BTreeSet<String>, WikiError> {
        collect_authority_project_ids(&mut self.conn)
    }

    fn reconcile_project(&mut self, project_id: &str) -> Result<SweepOutcome, WikiError> {
        match try_acquire_prune_lock(project_id)? {
            PruneProjectLock::Busy => Ok(SweepOutcome::Busy),
            PruneProjectLock::ProjectExists => Ok(SweepOutcome::Active),
            PruneProjectLock::Acquired(_guard) => {
                let projection_existed = self.qdrant_project_scopes.contains(project_id)
                    || self.falkor_project_scopes.contains(project_id);
                let scope = ScopeIdentity::project(project_id);
                let search_scope = SearchScope::project(project_id);
                let state =
                    purge_scope_state(&mut self.conn, &self.configs, &scope, &search_scope)?;
                if state.postgres.total() == 0 && !projection_existed {
                    Ok(SweepOutcome::AlreadyMissing)
                } else {
                    Ok(SweepOutcome::Deleted)
                }
            }
        }
    }

    fn reconcile_topic(&mut self, topic_name: &str) -> Result<SweepOutcome, WikiError> {
        let scope = ScopeIdentity::topic(topic_name);
        match try_acquire_prune_lock(&scope.to_string())? {
            PruneProjectLock::Busy => Ok(SweepOutcome::Busy),
            PruneProjectLock::ProjectExists => Ok(SweepOutcome::Active),
            PruneProjectLock::Acquired(_guard) => {
                if sql_scope_exists(&mut self.conn, "topic", topic_name)? {
                    return Ok(SweepOutcome::Active);
                }
                let projection_existed = self.qdrant_topic_scopes.contains(topic_name)
                    || self.falkor_topic_scopes.contains(topic_name);
                let search_scope = SearchScope::topic(topic_name);
                let state =
                    purge_scope_state(&mut self.conn, &self.configs, &scope, &search_scope)?;
                if state.postgres.total() == 0 && !projection_existed {
                    Ok(SweepOutcome::AlreadyMissing)
                } else {
                    Ok(SweepOutcome::Deleted)
                }
            }
        }
    }
}

pub(crate) fn execute(force: bool) -> Result<CommandOutcome, WikiError> {
    let mut backend = SystemPruneBackend::connect()?;
    let summary = run_prune(&mut backend, force, confirm_prune)?;
    let exit_code = u8::from(summary.totals.failed > 0);
    let text = render_text(&summary);
    let payload = serde_json::to_value(&summary).map_err(|error| WikiError::Json {
        action: "serialize gwiki prune summary",
        path: None,
        source: error,
    })?;
    Ok(CommandOutcome {
        status_messages: Vec::new(),
        result: CommandResult { payload, text },
        exit_code,
    })
}

fn run_prune<B, C>(backend: &mut B, force: bool, confirm: C) -> Result<PruneSummary, WikiError>
where
    B: PruneBackend,
    C: FnOnce(&[String]) -> Result<bool, WikiError>,
{
    // Configured projection enumeration is a preflight gate. Both complete
    // before any scope can be mutated.
    let qdrant_collections = backend.qdrant_collections()?;
    let falkor_scopes = backend.falkor_scopes()?;
    let sql_scopes = backend.sql_scopes()?;
    let authority = backend.authority_project_ids()?;
    let discovery = classify_discovery(
        &authority,
        &sql_scopes,
        qdrant_collections.as_deref(),
        falkor_scopes.as_ref(),
    );

    let mut orphan_scope_ids = discovery.orphan_project_ids.clone();
    orphan_scope_ids.extend(
        discovery
            .orphan_topics
            .iter()
            .map(|topic_name| format!("topic:{topic_name}")),
    );
    if !orphan_scope_ids.is_empty() && !force && !confirm(&orphan_scope_ids)? {
        return Ok(PruneSummary {
            command: COMMAND,
            status: "aborted",
            totals: discovery.totals,
            qdrant: discovery.qdrant,
            falkor: discovery.falkor,
            topic_collections: discovery.topic_collections,
            affected_scope_ids: Vec::new(),
            invalid_identifiers: discovery.invalid_identifiers,
        });
    }

    let project_sweep = sweep_scopes("project", &discovery.orphan_project_ids, |project_id| {
        backend.reconcile_project(project_id)
    });
    let topic_sweep = sweep_scopes("topic", &discovery.orphan_topics, |topic_name| {
        backend.reconcile_topic(topic_name)
    });
    let mut totals = discovery.totals;
    apply_sweep(&mut totals, &project_sweep);
    apply_sweep(&mut totals, &topic_sweep);
    let mut topic_collections = discovery.topic_collections;
    apply_sweep(&mut topic_collections, &topic_sweep);
    let mut affected_scope_ids = project_sweep.affected_scope_ids;
    for scope_id in topic_sweep.affected_scope_ids {
        record_affected(&mut affected_scope_ids, &scope_id);
    }
    Ok(PruneSummary {
        command: COMMAND,
        status: "completed",
        totals,
        qdrant: discovery.qdrant,
        falkor: discovery.falkor,
        topic_collections,
        affected_scope_ids,
        invalid_identifiers: discovery.invalid_identifiers,
    })
}

fn classify_discovery(
    authority: &BTreeSet<String>,
    sql_scopes: &SqlScopes,
    qdrant_collections: Option<&[String]>,
    falkor_scopes: Option<&crate::falkor_graph::FalkorScopes>,
) -> PruneDiscovery {
    let mut project_ids = BTreeSet::new();
    let mut topic_names = BTreeSet::new();
    let mut invalid = BTreeSet::new();
    let mut invalid_topics = BTreeSet::new();
    let mut scanned = 0;
    let mut topic_scanned = 0;

    for project_id in &sql_scopes.projects {
        scanned += 1;
        record_project_id(project_id, &mut project_ids, &mut invalid);
    }
    for topic_name in &sql_scopes.topics {
        scanned += 1;
        topic_scanned += 1;
        record_topic_name(
            topic_name,
            &format!("topic:{topic_name}"),
            &mut topic_names,
            &mut invalid,
            &mut invalid_topics,
        );
    }

    let qdrant_scanned = qdrant_collections.map_or(0, |collections| {
        let mut phase_scanned = 0;
        for collection in collections {
            if let Some(project_id) = collection.strip_prefix("gwiki_project_") {
                phase_scanned += 1;
                record_project_id(project_id, &mut project_ids, &mut invalid);
            } else if let Some(topic_name) = collection.strip_prefix("gwiki_topic_") {
                phase_scanned += 1;
                topic_scanned += 1;
                if topic_name_from_collection(collection).is_some() {
                    record_topic_name(
                        topic_name,
                        collection,
                        &mut topic_names,
                        &mut invalid,
                        &mut invalid_topics,
                    );
                } else {
                    invalid.insert(collection.clone());
                    invalid_topics.insert(collection.clone());
                }
            } else if collection.starts_with("gwiki_") {
                phase_scanned += 1;
                invalid.insert(collection.clone());
            }
        }
        scanned += phase_scanned;
        phase_scanned
    });

    let falkor_scanned = falkor_scopes.map_or(0, |scopes| {
        for project_id in &scopes.projects {
            record_project_id(project_id, &mut project_ids, &mut invalid);
        }
        for topic_name in &scopes.topics {
            topic_scanned += 1;
            record_topic_name(
                topic_name,
                &format!("topic:{topic_name}"),
                &mut topic_names,
                &mut invalid,
                &mut invalid_topics,
            );
        }
        let phase_scanned = scopes.projects.len() + scopes.topics.len();
        scanned += phase_scanned;
        phase_scanned
    });

    let active_projects = project_ids
        .iter()
        .filter(|project_id| authority.contains(*project_id))
        .count();
    let orphan_project_ids = project_ids
        .into_iter()
        .filter(|project_id| !authority.contains(project_id))
        .collect::<Vec<_>>();
    let active_topics = topic_names
        .iter()
        .filter(|topic_name| sql_scopes.topics.contains(*topic_name))
        .count();
    let orphan_topics = topic_names
        .into_iter()
        .filter(|topic_name| !sql_scopes.topics.contains(topic_name))
        .collect::<Vec<_>>();
    let invalid_count = invalid.len();
    PruneDiscovery {
        totals: ReconcileTotals {
            scanned,
            active: active_projects + active_topics,
            orphaned: orphan_project_ids.len() + orphan_topics.len(),
            invalid: invalid_count,
            ..ReconcileTotals::default()
        },
        qdrant: BackendPhase {
            status: if qdrant_collections.is_some() {
                "ready"
            } else {
                "skipped"
            },
            scanned: qdrant_scanned,
        },
        falkor: BackendPhase {
            status: if falkor_scopes.is_some() {
                "ready"
            } else {
                "skipped"
            },
            scanned: falkor_scanned,
        },
        topic_collections: ReconcileTotals {
            scanned: topic_scanned,
            active: active_topics,
            orphaned: orphan_topics.len(),
            invalid: invalid_topics.len(),
            ..ReconcileTotals::default()
        },
        orphan_project_ids,
        orphan_topics,
        invalid_identifiers: invalid.into_iter().take(AFFECTED_ID_LIMIT).collect(),
    }
}

fn record_project_id(
    project_id: &str,
    project_ids: &mut BTreeSet<String>,
    invalid: &mut BTreeSet<String>,
) {
    match uuid::Uuid::parse_str(project_id) {
        Ok(parsed) if parsed.to_string() == project_id => {
            project_ids.insert(project_id.to_string());
        }
        _ => {
            invalid.insert(project_id.to_string());
        }
    }
}

fn record_topic_name(
    topic_name: &str,
    identifier: &str,
    topic_names: &mut BTreeSet<String>,
    invalid: &mut BTreeSet<String>,
    invalid_topics: &mut BTreeSet<String>,
) {
    match crate::models::validate_topic_name(topic_name) {
        Ok(validated) if validated == topic_name => {
            topic_names.insert(topic_name.to_string());
        }
        _ => {
            invalid.insert(identifier.to_string());
            invalid_topics.insert(identifier.to_string());
        }
    }
}

fn topic_name_from_collection(collection: &str) -> Option<&str> {
    let topic_name = collection.strip_prefix("gwiki_topic_")?;
    (crate::models::topic_collection_name(topic_name)
        .ok()
        .as_deref()
        == Some(collection))
    .then_some(topic_name)
}

fn sweep_scopes(
    scope_kind: &str,
    scope_ids: &[String],
    mut reconcile: impl FnMut(&str) -> Result<SweepOutcome, WikiError>,
) -> SweepTotals {
    let mut totals = SweepTotals::default();
    for scope_id in scope_ids {
        let affected_id = match scope_kind {
            "topic" => format!("topic:{scope_id}"),
            _ => scope_id.clone(),
        };
        match reconcile(scope_id) {
            Ok(SweepOutcome::Active) => totals.active += 1,
            Ok(SweepOutcome::Deleted) => {
                totals.deleted += 1;
                record_affected(&mut totals.affected_scope_ids, &affected_id);
            }
            Ok(SweepOutcome::AlreadyMissing) => {
                totals.already_missing += 1;
                record_affected(&mut totals.affected_scope_ids, &affected_id);
            }
            Ok(SweepOutcome::Busy) => {
                totals.busy += 1;
                record_affected(&mut totals.affected_scope_ids, &affected_id);
            }
            Err(error) => {
                totals.failed += 1;
                record_affected(&mut totals.affected_scope_ids, &affected_id);
                eprintln!("Warning: gwiki prune failed for {scope_kind} {scope_id}: {error}");
            }
        }
    }
    totals
}

fn apply_sweep(totals: &mut ReconcileTotals, sweep: &SweepTotals) {
    totals.active += sweep.active;
    totals.deleted += sweep.deleted;
    totals.already_missing += sweep.already_missing;
    totals.busy += sweep.busy;
    totals.failed += sweep.failed;
}

fn record_affected(affected: &mut Vec<String>, scope_id: &str) {
    if affected.len() < AFFECTED_ID_LIMIT {
        affected.push(scope_id.to_string());
    }
}

fn collect_authority_project_ids(
    conn: &mut impl GenericClient,
) -> Result<BTreeSet<String>, WikiError> {
    conn.query(
        "SELECT id::text AS project_id FROM projects ORDER BY id",
        &[],
    )
    .map_err(|error| postgres_error("load authoritative projects", error))?
    .into_iter()
    .map(|row| Ok(row.get::<_, String>("project_id")))
    .collect()
}

fn collect_sql_scopes(conn: &mut impl GenericClient) -> Result<SqlScopes, WikiError> {
    let rows = conn
        .query(&scope_discovery_sql(), &[])
        .map_err(|error| postgres_error("discover scopes across gwiki tables", error))?;
    let mut scopes = SqlScopes::default();
    for row in rows {
        let scope_kind = row.get::<_, String>("scope_kind");
        let scope_id = row.get::<_, String>("scope_id");
        match scope_kind.as_str() {
            "project" => scopes.projects.push(scope_id),
            "topic" => {
                scopes.topics.insert(scope_id);
            }
            _ => {}
        }
    }
    Ok(scopes)
}

fn scope_discovery_sql() -> String {
    let unions = GWIKI_POSTGRES_TABLES
        .iter()
        .map(|table| {
            format!(
                "SELECT scope_kind, scope_id FROM {} WHERE scope_kind IN ('project', 'topic')",
                table.name()
            )
        })
        .collect::<Vec<_>>()
        .join(" UNION ");
    format!("SELECT scope_kind, scope_id FROM ({unions}) scopes ORDER BY scope_kind, scope_id")
}

fn sql_scope_exists(
    conn: &mut impl GenericClient,
    scope_kind: &str,
    scope_id: &str,
) -> Result<bool, WikiError> {
    conn.query_one(&scope_exists_sql(), &[&scope_kind, &scope_id])
        .map(|row| row.get::<_, bool>("scope_exists"))
        .map_err(|error| postgres_error("recheck SQL scope authority", error))
}

fn scope_exists_sql() -> String {
    let unions = GWIKI_POSTGRES_TABLES
        .iter()
        .map(|table| {
            format!(
                "SELECT 1 FROM {} WHERE scope_kind = $1 AND scope_id = $2",
                table.name()
            )
        })
        .collect::<Vec<_>>()
        .join(" UNION ALL ");
    format!("SELECT EXISTS ({unions}) AS scope_exists")
}

fn postgres_error(action: &str, error: postgres::Error) -> WikiError {
    WikiError::Config {
        detail: format!("failed to {action} for gwiki prune: {error}"),
    }
}

fn confirm_prune(scope_ids: &[String]) -> Result<bool, WikiError> {
    eprintln!("Pending gwiki prune: {} orphan scope(s).", scope_ids.len());
    for scope_id in scope_ids.iter().take(AFFECTED_ID_LIMIT) {
        eprintln!("  scope: {scope_id}");
    }
    eprint!("\nPurge all listed generated wiki state? [y/N] ");
    std::io::stderr().flush().map_err(|source| WikiError::Io {
        action: "flush gwiki prune confirmation",
        path: None,
        source,
    })?;
    let mut input = String::new();
    std::io::stdin()
        .read_line(&mut input)
        .map_err(|source| WikiError::Io {
            action: "read gwiki prune confirmation",
            path: None,
            source,
        })?;
    Ok(input.trim().eq_ignore_ascii_case("y"))
}

fn render_text(summary: &PruneSummary) -> String {
    let totals = &summary.totals;
    let mut text = format!(
        "gwiki prune: status={} scanned={} active={} orphaned={} deleted={} already_missing={} busy={} invalid={} failed={} qdrant={} falkor={}",
        summary.status,
        totals.scanned,
        totals.active,
        totals.orphaned,
        totals.deleted,
        totals.already_missing,
        totals.busy,
        totals.invalid,
        totals.failed,
        summary.qdrant.status,
        summary.falkor.status,
    );
    let topics = &summary.topic_collections;
    text.push_str(&format!(
        "\ntopic_collections: scanned={} active={} orphaned={} deleted={} already_missing={} busy={} invalid={} failed={}",
        topics.scanned,
        topics.active,
        topics.orphaned,
        topics.deleted,
        topics.already_missing,
        topics.busy,
        topics.invalid,
        topics.failed,
    ));
    if !summary.affected_scope_ids.is_empty() {
        text.push_str("\naffected scope IDs: ");
        text.push_str(&summary.affected_scope_ids.join(", "));
    }
    if !summary.invalid_identifiers.is_empty() {
        text.push_str("\nreported without deletion: ");
        text.push_str(&summary.invalid_identifiers.join(", "));
    }
    text
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;

    const LIVE: &str = "3f81ed3f-3855-4fb5-a4ad-c06ff4be1b3d";
    const SQL_ORPHAN: &str = "437e0f48-37d0-499f-a971-f3253b9bbd0c";
    const QDRANT_ORPHAN: &str = "4f5ce9c1-11f1-4a73-865d-e5b611b9ac88";
    const FALKOR_ORPHAN: &str = "75747eb8-c6e2-4636-aa05-7c0376528e53";

    #[test]
    fn discovery_preserves_sql_topics_and_classifies_orphan_topic_projections() {
        let authority = BTreeSet::from([LIVE.to_string()]);
        let sql = SqlScopes {
            projects: vec![SQL_ORPHAN.to_string()],
            topics: BTreeSet::from(["rust".to_string()]),
        };
        let falkor = crate::falkor_graph::FalkorScopes {
            projects: vec![FALKOR_ORPHAN.to_string()],
            topics: vec!["graph-only".to_string()],
        };
        let discovery = classify_discovery(
            &authority,
            &sql,
            Some(&[
                format!("gwiki_project_{LIVE}"),
                format!("gwiki_project_{QDRANT_ORPHAN}"),
                "gwiki_topic_rust".to_string(),
                "gwiki_topic_abandoned".to_string(),
                "gwiki_topic_".to_string(),
                "gwiki_broken".to_string(),
                "unrelated".to_string(),
            ]),
            Some(&falkor),
        );

        assert_eq!(discovery.totals.scanned, 10);
        assert_eq!(discovery.totals.active, 2);
        assert_eq!(discovery.totals.orphaned, 5);
        assert_eq!(discovery.totals.invalid, 2);
        assert_eq!(
            discovery.orphan_project_ids,
            vec![
                SQL_ORPHAN.to_string(),
                QDRANT_ORPHAN.to_string(),
                FALKOR_ORPHAN.to_string(),
            ]
        );
        assert_eq!(discovery.orphan_topics, vec!["abandoned", "graph-only"]);
        assert_eq!(discovery.topic_collections.scanned, 5);
        assert_eq!(discovery.topic_collections.active, 1);
        assert_eq!(discovery.topic_collections.orphaned, 2);
        assert_eq!(discovery.topic_collections.invalid, 1);
        assert_eq!(
            discovery.invalid_identifiers,
            vec!["gwiki_broken".to_string(), "gwiki_topic_".to_string()]
        );
    }

    #[test]
    fn sql_discovery_query_provably_unions_every_gwiki_table() {
        let query = scope_discovery_sql();
        for table in GWIKI_POSTGRES_TABLES {
            assert!(query.contains(table.name()), "missing {}", table.name());
        }
        assert_eq!(
            query.matches("scope_kind IN ('project', 'topic')").count(),
            5
        );
        assert_eq!(query.matches(" UNION ").count(), 4);

        let recheck = scope_exists_sql();
        for table in GWIKI_POSTGRES_TABLES {
            assert!(recheck.contains(table.name()), "missing {}", table.name());
        }
        assert_eq!(recheck.matches("scope_kind = $1").count(), 5);
        assert_eq!(recheck.matches(" UNION ALL ").count(), 4);
    }

    #[test]
    fn sweep_continues_after_failure_and_caps_affected_ids() {
        let ids = (0..14)
            .map(|index| format!("project-{index}"))
            .collect::<Vec<_>>();
        let totals = sweep_scopes("topic", &ids, |project_id| match project_id {
            "project-0" => Ok(SweepOutcome::Active),
            "project-1" => Ok(SweepOutcome::Busy),
            "project-2" => Ok(SweepOutcome::AlreadyMissing),
            "project-3" => Err(WikiError::Config {
                detail: "fixture failure".to_string(),
            }),
            _ => Ok(SweepOutcome::Deleted),
        });

        assert_eq!(totals.active, 1);
        assert_eq!(totals.busy, 1);
        assert_eq!(totals.already_missing, 1);
        assert_eq!(totals.failed, 1);
        assert_eq!(totals.deleted, 10);
        assert_eq!(totals.affected_scope_ids.len(), AFFECTED_ID_LIMIT);
        assert!(
            totals
                .affected_scope_ids
                .iter()
                .all(|scope_id| scope_id.starts_with("topic:"))
        );
    }

    #[test]
    fn missing_qdrant_skips_only_qdrant_work() {
        let mut backend = FakeBackend {
            sql: SqlScopes {
                projects: vec![SQL_ORPHAN.to_string()],
                ..SqlScopes::default()
            },
            falkor: Some(crate::falkor_graph::FalkorScopes {
                projects: vec![FALKOR_ORPHAN.to_string()],
                ..crate::falkor_graph::FalkorScopes::default()
            }),
            ..FakeBackend::default()
        };

        let summary = run_prune(&mut backend, true, |_| Ok(true)).expect("prune succeeds");

        assert_eq!(summary.qdrant.status, "skipped");
        assert_eq!(summary.falkor.status, "ready");
        assert_eq!(summary.totals.deleted, 2);
        assert_eq!(backend.reconciled_projects.len(), 2);
    }

    #[test]
    fn topic_that_appears_during_final_recheck_is_preserved() {
        let mut backend = FakeBackend {
            qdrant: Some(vec!["gwiki_topic_recovered".to_string()]),
            outcomes: BTreeMap::from([("topic:recovered".to_string(), SweepOutcome::Active)]),
            ..FakeBackend::default()
        };

        let summary = run_prune(&mut backend, true, |_| Ok(true)).expect("prune succeeds");

        assert_eq!(summary.topic_collections.orphaned, 1);
        assert_eq!(summary.topic_collections.active, 1);
        assert_eq!(summary.topic_collections.deleted, 0);
        assert_eq!(backend.reconciled_topics, vec!["recovered"]);
        assert!(render_text(&summary).contains("topic_collections: scanned=1 active=1"));
    }

    #[test]
    fn unavailable_projection_backends_preserve_sql_backed_topics() {
        let mut backend = FakeBackend {
            sql: SqlScopes {
                topics: BTreeSet::from(["rust".to_string()]),
                ..SqlScopes::default()
            },
            ..FakeBackend::default()
        };

        let summary = run_prune(&mut backend, true, |_| Ok(true)).expect("prune succeeds");

        assert_eq!(summary.qdrant.status, "skipped");
        assert_eq!(summary.falkor.status, "skipped");
        assert_eq!(summary.topic_collections.active, 1);
        assert!(backend.reconciled_topics.is_empty());
    }

    #[test]
    fn configured_projection_discovery_failure_aborts_before_mutation() {
        let mut backend = FakeBackend {
            qdrant_error: true,
            sql: SqlScopes {
                projects: vec![SQL_ORPHAN.to_string()],
                ..SqlScopes::default()
            },
            ..FakeBackend::default()
        };

        let error = run_prune(&mut backend, true, |_| Ok(true)).expect_err("preflight fails");

        assert!(error.to_string().contains("fixture Qdrant failure"));
        assert!(backend.reconciled_projects.is_empty());
        assert!(backend.reconciled_topics.is_empty());
    }

    #[test]
    fn ingestion_only_scope_is_discovered_from_sql_union() -> anyhow::Result<()> {
        let Some(database_url) = std::env::var("GOBBY_TEST_DATABASE_URL").ok() else {
            eprintln!("skipping gwiki prune SQL test; GOBBY_TEST_DATABASE_URL is not set");
            return Ok(());
        };
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
        for table in GWIKI_POSTGRES_TABLES {
            conn.batch_execute(&format!(
                "CREATE TEMP TABLE {} (scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL)",
                table.name()
            ))?;
        }
        conn.execute(
            "INSERT INTO gwiki_ingestions (scope_kind, scope_id) VALUES ('project', $1)",
            &[&SQL_ORPHAN],
        )?;
        for (table, topic_name) in
            GWIKI_POSTGRES_TABLES
                .iter()
                .zip(["rust", "python", "retention", "operations"])
        {
            conn.execute(
                &format!(
                    "INSERT INTO {} (scope_kind, scope_id) VALUES ('topic', $1)",
                    table.name()
                ),
                &[&topic_name],
            )?;
        }

        let scopes = collect_sql_scopes(&mut conn)?;
        assert_eq!(scopes.projects, vec![SQL_ORPHAN]);
        assert_eq!(
            scopes.topics,
            BTreeSet::from([
                "operations".to_string(),
                "python".to_string(),
                "retention".to_string(),
                "rust".to_string(),
            ])
        );
        assert!(sql_scope_exists(&mut conn, "topic", "rust")?);
        assert!(!sql_scope_exists(&mut conn, "topic", "orphan")?);
        Ok(())
    }

    #[derive(Default)]
    struct FakeBackend {
        authority: BTreeSet<String>,
        sql: SqlScopes,
        qdrant: Option<Vec<String>>,
        falkor: Option<crate::falkor_graph::FalkorScopes>,
        qdrant_error: bool,
        reconciled_projects: Vec<String>,
        reconciled_topics: Vec<String>,
        outcomes: BTreeMap<String, SweepOutcome>,
    }

    impl PruneBackend for FakeBackend {
        fn qdrant_collections(&mut self) -> Result<Option<Vec<String>>, WikiError> {
            if self.qdrant_error {
                return Err(WikiError::Config {
                    detail: "fixture Qdrant failure".to_string(),
                });
            }
            Ok(self.qdrant.clone())
        }

        fn falkor_scopes(
            &mut self,
        ) -> Result<Option<crate::falkor_graph::FalkorScopes>, WikiError> {
            Ok(self.falkor.take())
        }

        fn sql_scopes(&mut self) -> Result<SqlScopes, WikiError> {
            Ok(self.sql.clone())
        }

        fn authority_project_ids(&mut self) -> Result<BTreeSet<String>, WikiError> {
            Ok(self.authority.clone())
        }

        fn reconcile_project(&mut self, project_id: &str) -> Result<SweepOutcome, WikiError> {
            self.reconciled_projects.push(project_id.to_string());
            Ok(self
                .outcomes
                .get(project_id)
                .copied()
                .unwrap_or(SweepOutcome::Deleted))
        }

        fn reconcile_topic(&mut self, topic_name: &str) -> Result<SweepOutcome, WikiError> {
            self.reconciled_topics.push(topic_name.to_string());
            Ok(self
                .outcomes
                .get(&format!("topic:{topic_name}"))
                .copied()
                .unwrap_or(SweepOutcome::Deleted))
        }
    }
}
