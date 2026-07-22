use std::collections::BTreeSet;
use std::io::Write;

use postgres::{Client, GenericClient};
use serde::Serialize;

use crate::commands::purge::{BackendConfigs, optional_backend_configs, purge_scope_state};
use crate::project_lock::{PruneProjectLock, try_acquire_prune_lock};
use crate::search::SearchScope;
use crate::setup::GWIKI_POSTGRES_TABLES;
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
    affected_scope_ids: Vec<String>,
    invalid_identifiers: Vec<String>,
}

struct PruneDiscovery {
    totals: ReconcileTotals,
    qdrant: BackendPhase,
    falkor: BackendPhase,
    orphan_ids: Vec<String>,
    invalid_identifiers: Vec<String>,
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
    fn falkor_project_scopes(&mut self) -> Result<Option<Vec<String>>, WikiError>;
    fn sql_project_scopes(&mut self) -> Result<Vec<String>, WikiError>;
    fn authority_project_ids(&mut self) -> Result<BTreeSet<String>, WikiError>;
    fn reconcile_project(&mut self, project_id: &str) -> Result<SweepOutcome, WikiError>;
}

struct SystemPruneBackend {
    conn: Client,
    configs: BackendConfigs,
    qdrant_project_scopes: BTreeSet<String>,
    falkor_project_scopes: BTreeSet<String>,
}

impl SystemPruneBackend {
    fn connect() -> Result<Self, WikiError> {
        let mut conn = require_postgres_index_readwrite(COMMAND)?;
        let configs = optional_backend_configs(&mut conn, COMMAND)?;
        Ok(Self {
            conn,
            configs,
            qdrant_project_scopes: BTreeSet::new(),
            falkor_project_scopes: BTreeSet::new(),
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
        Ok(Some(collections))
    }

    fn falkor_project_scopes(&mut self) -> Result<Option<Vec<String>>, WikiError> {
        let scopes = self
            .configs
            .falkor
            .as_ref()
            .map(crate::falkor_graph::list_project_scopes)
            .transpose()?;
        self.falkor_project_scopes = scopes.iter().flatten().cloned().collect();
        Ok(scopes)
    }

    fn sql_project_scopes(&mut self) -> Result<Vec<String>, WikiError> {
        collect_sql_project_scopes(&mut self.conn)
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
    let falkor_scopes = backend.falkor_project_scopes()?;
    let sql_scopes = backend.sql_project_scopes()?;
    let authority = backend.authority_project_ids()?;
    let discovery = classify_discovery(
        &authority,
        &sql_scopes,
        qdrant_collections.as_deref(),
        falkor_scopes.as_deref(),
    );

    if !discovery.orphan_ids.is_empty() && !force && !confirm(&discovery.orphan_ids)? {
        return Ok(PruneSummary {
            command: COMMAND,
            status: "aborted",
            totals: discovery.totals,
            qdrant: discovery.qdrant,
            falkor: discovery.falkor,
            affected_scope_ids: Vec::new(),
            invalid_identifiers: discovery.invalid_identifiers,
        });
    }

    let sweep = sweep_projects(&discovery.orphan_ids, |project_id| {
        backend.reconcile_project(project_id)
    });
    let mut totals = discovery.totals;
    totals.active += sweep.active;
    totals.deleted = sweep.deleted;
    totals.already_missing = sweep.already_missing;
    totals.busy = sweep.busy;
    totals.failed = sweep.failed;
    Ok(PruneSummary {
        command: COMMAND,
        status: "completed",
        totals,
        qdrant: discovery.qdrant,
        falkor: discovery.falkor,
        affected_scope_ids: sweep.affected_scope_ids,
        invalid_identifiers: discovery.invalid_identifiers,
    })
}

fn classify_discovery(
    authority: &BTreeSet<String>,
    sql_scopes: &[String],
    qdrant_collections: Option<&[String]>,
    falkor_scopes: Option<&[String]>,
) -> PruneDiscovery {
    let mut project_ids = BTreeSet::new();
    let mut invalid = BTreeSet::new();
    let mut scanned = 0;

    for project_id in sql_scopes {
        scanned += 1;
        record_project_id(project_id, &mut project_ids, &mut invalid);
    }

    let qdrant_scanned = qdrant_collections.map_or(0, |collections| {
        let mut phase_scanned = 0;
        for collection in collections {
            if let Some(project_id) = collection.strip_prefix("gwiki_project_") {
                phase_scanned += 1;
                record_project_id(project_id, &mut project_ids, &mut invalid);
            } else if collection.starts_with("gwiki_") {
                // Topic and malformed gwiki collections are observable and
                // intentionally excluded from automated deletion.
                phase_scanned += 1;
                invalid.insert(collection.clone());
            }
        }
        scanned += phase_scanned;
        phase_scanned
    });

    let falkor_scanned = falkor_scopes.map_or(0, |project_scopes| {
        for project_id in project_scopes {
            record_project_id(project_id, &mut project_ids, &mut invalid);
        }
        scanned += project_scopes.len();
        project_scopes.len()
    });

    let active = project_ids
        .iter()
        .filter(|project_id| authority.contains(*project_id))
        .count();
    let orphan_ids = project_ids
        .into_iter()
        .filter(|project_id| !authority.contains(project_id))
        .collect::<Vec<_>>();
    let invalid_count = invalid.len();
    PruneDiscovery {
        totals: ReconcileTotals {
            scanned,
            active,
            orphaned: orphan_ids.len(),
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
        orphan_ids,
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

fn sweep_projects(
    project_ids: &[String],
    mut reconcile: impl FnMut(&str) -> Result<SweepOutcome, WikiError>,
) -> SweepTotals {
    let mut totals = SweepTotals::default();
    for project_id in project_ids {
        match reconcile(project_id) {
            Ok(SweepOutcome::Active) => totals.active += 1,
            Ok(SweepOutcome::Deleted) => {
                totals.deleted += 1;
                record_affected(&mut totals.affected_scope_ids, project_id);
            }
            Ok(SweepOutcome::AlreadyMissing) => {
                totals.already_missing += 1;
                record_affected(&mut totals.affected_scope_ids, project_id);
            }
            Ok(SweepOutcome::Busy) => {
                totals.busy += 1;
                record_affected(&mut totals.affected_scope_ids, project_id);
            }
            Err(error) => {
                totals.failed += 1;
                record_affected(&mut totals.affected_scope_ids, project_id);
                eprintln!("Warning: gwiki prune failed for project {project_id}: {error}");
            }
        }
    }
    totals
}

fn record_affected(affected: &mut Vec<String>, project_id: &str) {
    if affected.len() < AFFECTED_ID_LIMIT {
        affected.push(project_id.to_string());
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

fn collect_sql_project_scopes(conn: &mut impl GenericClient) -> Result<Vec<String>, WikiError> {
    let rows = conn
        .query(&project_scope_discovery_sql(), &[&"project"])
        .map_err(|error| postgres_error("discover project scopes across gwiki tables", error))?;
    Ok(rows
        .into_iter()
        .map(|row| row.get::<_, String>("scope_id"))
        .collect())
}

fn project_scope_discovery_sql() -> String {
    let unions = GWIKI_POSTGRES_TABLES
        .iter()
        .map(|table| {
            format!(
                "SELECT scope_id FROM {} WHERE scope_kind = $1",
                table.name()
            )
        })
        .collect::<Vec<_>>()
        .join(" UNION ");
    format!("SELECT scope_id FROM ({unions}) scopes ORDER BY scope_id")
}

fn postgres_error(action: &str, error: postgres::Error) -> WikiError {
    WikiError::Config {
        detail: format!("failed to {action} for gwiki prune: {error}"),
    }
}

fn confirm_prune(project_ids: &[String]) -> Result<bool, WikiError> {
    eprintln!(
        "Pending gwiki prune: {} orphan project scope(s).",
        project_ids.len()
    );
    for project_id in project_ids.iter().take(AFFECTED_ID_LIMIT) {
        eprintln!("  project: {project_id}");
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
    fn discovery_unions_backends_and_reports_topics_without_deleting_them() {
        let authority = BTreeSet::from([LIVE.to_string()]);
        let discovery = classify_discovery(
            &authority,
            &[SQL_ORPHAN.to_string()],
            Some(&[
                format!("gwiki_project_{LIVE}"),
                format!("gwiki_project_{QDRANT_ORPHAN}"),
                "gwiki_topic_rust".to_string(),
                "gwiki_broken".to_string(),
                "unrelated".to_string(),
            ]),
            Some(&[FALKOR_ORPHAN.to_string()]),
        );

        assert_eq!(discovery.totals.scanned, 6);
        assert_eq!(discovery.totals.active, 1);
        assert_eq!(discovery.totals.orphaned, 3);
        assert_eq!(discovery.totals.invalid, 2);
        assert_eq!(
            discovery.orphan_ids,
            vec![
                SQL_ORPHAN.to_string(),
                QDRANT_ORPHAN.to_string(),
                FALKOR_ORPHAN.to_string(),
            ]
        );
        assert_eq!(
            discovery.invalid_identifiers,
            vec!["gwiki_broken".to_string(), "gwiki_topic_rust".to_string()]
        );
    }

    #[test]
    fn sql_discovery_query_provably_unions_every_gwiki_table() {
        let query = project_scope_discovery_sql();
        for table in GWIKI_POSTGRES_TABLES {
            assert!(query.contains(table.name()), "missing {}", table.name());
        }
        assert_eq!(query.matches("scope_kind = $1").count(), 5);
        assert_eq!(query.matches(" UNION ").count(), 4);
    }

    #[test]
    fn sweep_continues_after_failure_and_caps_affected_ids() {
        let ids = (0..14)
            .map(|index| format!("project-{index}"))
            .collect::<Vec<_>>();
        let totals = sweep_projects(&ids, |project_id| match project_id {
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
    }

    #[test]
    fn missing_qdrant_skips_only_qdrant_work() {
        let mut backend = FakeBackend {
            sql: vec![SQL_ORPHAN.to_string()],
            falkor: Some(vec![FALKOR_ORPHAN.to_string()]),
            ..FakeBackend::default()
        };

        let summary = run_prune(&mut backend, true, |_| Ok(true)).expect("prune succeeds");

        assert_eq!(summary.qdrant.status, "skipped");
        assert_eq!(summary.falkor.status, "ready");
        assert_eq!(summary.totals.deleted, 2);
        assert_eq!(backend.reconciled.len(), 2);
    }

    #[test]
    fn configured_projection_discovery_failure_aborts_before_mutation() {
        let mut backend = FakeBackend {
            qdrant_error: true,
            sql: vec![SQL_ORPHAN.to_string()],
            ..FakeBackend::default()
        };

        let error = run_prune(&mut backend, true, |_| Ok(true)).expect_err("preflight fails");

        assert!(error.to_string().contains("fixture Qdrant failure"));
        assert!(backend.reconciled.is_empty());
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
                "CREATE TEMP TABLE {} (scope_kind TEXT NOT NULL, scope_id TEXT NOT NULL) ON COMMIT DROP",
                table.name()
            ))?;
        }
        conn.execute(
            "INSERT INTO gwiki_ingestions (scope_kind, scope_id) VALUES ('project', $1)",
            &[&SQL_ORPHAN],
        )?;

        assert_eq!(collect_sql_project_scopes(&mut conn)?, vec![SQL_ORPHAN]);
        Ok(())
    }

    #[derive(Default)]
    struct FakeBackend {
        authority: BTreeSet<String>,
        sql: Vec<String>,
        qdrant: Option<Vec<String>>,
        falkor: Option<Vec<String>>,
        qdrant_error: bool,
        reconciled: Vec<String>,
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

        fn falkor_project_scopes(&mut self) -> Result<Option<Vec<String>>, WikiError> {
            Ok(self.falkor.clone())
        }

        fn sql_project_scopes(&mut self) -> Result<Vec<String>, WikiError> {
            Ok(self.sql.clone())
        }

        fn authority_project_ids(&mut self) -> Result<BTreeSet<String>, WikiError> {
            Ok(self.authority.clone())
        }

        fn reconcile_project(&mut self, project_id: &str) -> Result<SweepOutcome, WikiError> {
            self.reconciled.push(project_id.to_string());
            Ok(self
                .outcomes
                .get(project_id)
                .copied()
                .unwrap_or(SweepOutcome::Deleted))
        }
    }
}
