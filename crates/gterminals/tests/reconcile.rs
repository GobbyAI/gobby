mod common;

use std::time::Duration;

use common::{Hub, terminal};
use gobby_terminals::host::{EpochAuthority, HostOptions, HostSupervisor};
use gobby_terminals::reconcile::{self, Effects, Observation, Options, ProcessStatus};
use gobby_terminals::repository::{self, Backend, Ownership, State, Terminal};
use serde_json::json;
use uuid::Uuid;

#[tokio::test]
async fn terminal_row_round_trips_all_runtime_fields() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for state in [State::Pending, State::Live, State::Orphaned, State::Exited] {
        let mut row = terminal();
        row.state = state;
        if state != State::Pending {
            row.host_epoch = Some("epoch-E".into());
            row.locator = Some(json!({"host_terminal_id": "slot-1"}));
            row.locator_key = Some(format!("native:epoch-E:{}", row.id));
        }
        hub.insert(&row).await?;
        assert_eq!(hub.get(row.id).await?, row, "native {state:?}");
    }
    let mut row = terminal();
    row.backend = Backend::Tmux;
    row.ownership = Ownership::External;
    row.state = State::Live;
    row.spawn_key = None;
    row.process = None;
    row.locator = Some(json!({"socket": "/tmp/owned", "pane": "%42"}));
    row.locator_key = Some("tmux:/tmp/owned:1:2:%42".into());
    hub.insert(&row).await?;
    assert_eq!(hub.get(row.id).await?, row, "external tmux");
    Ok(())
}

struct Runtime {
    killed: std::sync::Mutex<Vec<String>>,
    epochs: std::sync::Mutex<Vec<String>>,
    process: ProcessStatus,
    failure: Option<&'static str>,
    kill_proven: bool,
    held: Option<Uuid>,
    snapshot_signal: Option<std::sync::Arc<tokio::sync::Notify>>,
}

impl Default for Runtime {
    fn default() -> Self {
        Self {
            killed: std::sync::Mutex::default(),
            epochs: std::sync::Mutex::default(),
            process: ProcessStatus::Dead,
            failure: None,
            kill_proven: true,
            held: None,
            snapshot_signal: None,
        }
    }
}

impl Effects for Runtime {
    fn holds(&self, id: Uuid) -> bool {
        if let Some(signal) = &self.snapshot_signal {
            signal.notify_one();
        }
        self.held == Some(id)
    }

    async fn process_status(&self, _terminal: &Terminal) -> anyhow::Result<ProcessStatus> {
        Ok(self.process)
    }

    async fn kill(&self, host_epoch: &str, host_terminal_id: &str) -> anyhow::Result<bool> {
        self.epochs
            .lock()
            .expect("epoch log")
            .push(host_epoch.into());
        self.killed
            .lock()
            .expect("kill log")
            .push(host_terminal_id.into());
        anyhow::ensure!(self.failure != Some("kill"), "injected kill uncertainty");
        Ok(self.kill_proven)
    }

    async fn settle_run(
        &self,
        tx: &gobby_core::postgres_pool::Transaction<'_>,
        before: &Terminal,
        after: &Terminal,
    ) -> anyhow::Result<()> {
        anyhow::ensure!(
            self.failure != Some("before_run_write"),
            "injected before run write failure"
        );
        if let Some(run_id) = before.agent_run_id {
            let status = match after.state {
                State::Live => "running",
                State::Pending => "pending",
                State::Exited | State::Orphaned => "cancelled",
            };
            let changed = tx
                .execute(
                    "UPDATE agent_runs SET status=$1, terminal_id=$2::text::uuid,
                 terminal_reason=CASE WHEN $1='cancelled' THEN 'daemon_stop' ELSE NULL END
                 WHERE id=$3::text::uuid AND machine_id=$4::text::uuid
                 AND (terminal_id IS NULL OR terminal_id=$2::text::uuid)",
                    &[
                        &status,
                        &before.id.to_string(),
                        &run_id.to_string(),
                        &before.machine_id.to_string(),
                    ],
                )
                .await?;
            anyhow::ensure!(changed == 1, "missing or foreign run ownership");
        }
        anyhow::ensure!(
            self.failure != Some("after_run_write"),
            "injected after run write failure"
        );
        Ok(())
    }
}

fn options() -> Options {
    Options {
        now: "2026-10-01T12:02:00Z".parse().expect("observation time"),
        spawn_in_doubt: Duration::from_secs(30),
        settle_indeterminate: false,
        dead_host_epochs: Default::default(),
    }
}

#[tokio::test]
async fn host_exit_evidence_is_durable_and_machine_scoped() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let machine = Uuid::new_v4();
    let foreign = Uuid::new_v4();
    let authority = std::sync::Arc::new(hub.epoch_authority()?);
    authority.observe_epoch(machine, "opaque-old").await?;
    authority.observe_exit(machine, "opaque-old").await?;
    authority.observe_epoch(machine, "opaque-current").await?;
    authority.observe_epoch(foreign, "foreign-old").await?;
    authority.observe_exit(foreign, "foreign-old").await?;

    let supervisor = HostSupervisor::new(
        HostOptions {
            machine_id: machine.to_string(),
            ..HostOptions::default()
        },
        authority,
    );
    let snapshot = supervisor.epoch_snapshot().await?;
    assert_eq!(snapshot.machine_id, machine);
    assert_eq!(snapshot.current_epoch.as_deref(), Some("opaque-current"));
    assert_eq!(snapshot.exited_epochs, ["opaque-old".to_owned()].into());
    drop(supervisor);

    let restarted = HostSupervisor::new(
        HostOptions {
            machine_id: machine.to_string(),
            ..HostOptions::default()
        },
        std::sync::Arc::new(hub.epoch_authority()?),
    );
    assert_eq!(restarted.epoch_snapshot().await?, snapshot);
    Ok(())
}

#[tokio::test]
async fn exited_epoch_cannot_be_revived_or_clear_a_new_current_epoch() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let machine = Uuid::new_v4();
    let authority = hub.epoch_authority()?;
    authority.observe_epoch(machine, "old").await?;
    authority.observe_exit(machine, "old").await?;
    authority.observe_epoch(machine, "current").await?;
    authority.observe_exit(machine, "old").await?;
    assert!(authority.observe_epoch(machine, "old").await.is_err());
    let snapshot = authority.epoch_snapshot(machine).await?;
    assert_eq!(snapshot.current_epoch.as_deref(), Some("current"));
    assert_eq!(snapshot.exited_epochs, ["old".to_owned()].into());
    Ok(())
}

#[tokio::test]
async fn pgidless_absent_rows_exit_instead_of_remaining_unknown() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for process in [None, Some(json!({})), Some(json!({"pgid": null}))] {
        for state in [State::Live, State::Orphaned] {
            let mut row = live(terminal(), "dead");
            row.state = state;
            row.process = process.clone();
            hub.insert(&row).await?;
            seed_run(&hub, &row).await?;
            let runtime = Runtime {
                process: ProcessStatus::Unknown,
                ..Runtime::default()
            };
            let mut settings = options();
            settings.dead_host_epochs.insert("dead".into());
            let observed = gobby_terminals::control::Inventory {
                epoch: "current".into(),
                ..Default::default()
            };
            // The first pass preserves the old epoch's Live -> Orphaned contract.
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&observed),
                &settings,
                &runtime,
            )
            .await?;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&observed),
                &settings,
                &runtime,
            )
            .await?;
            assert_eq!(
                hub.get(row.id).await?.state,
                State::Exited,
                "{state:?}/{process:?}"
            );
            assert_eq!(run_status(&hub, &row).await?, "cancelled");
        }
    }
    Ok(())
}

#[tokio::test]
async fn dead_epoch_live_rows_interrupt_runs_and_replay_without_writes() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = live(terminal(), "epoch-DEAD");
    hub.insert(&row).await?;
    seed_run(&hub, &row).await?;
    let host = gobby_terminals::control::Inventory {
        epoch: "epoch-E".into(),
        ..Default::default()
    };
    let mut settings = options();
    settings.dead_host_epochs.insert("epoch-DEAD".into());
    let runtime = Runtime {
        process: ProcessStatus::Alive,
        ..Default::default()
    };
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &settings,
        &runtime,
    )
    .await?;
    let after = hub.get(row.id).await?;
    assert_eq!(after.state, State::Orphaned);
    assert_eq!(after.process, row.process);
    assert_eq!(run_status(&hub, &row).await?, "cancelled");
    hub.fault(Some("terminal_write")).await?;
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &settings,
        &runtime,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?, after);
    Ok(())
}

#[tokio::test]
async fn dead_epoch_orphans_require_proven_process_death() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for status in [
        ProcessStatus::Dead,
        ProcessStatus::Alive,
        ProcessStatus::Unknown,
    ] {
        let mut row = live(terminal(), "epoch-DEAD");
        row.state = State::Orphaned;
        hub.insert(&row).await?;
        seed_run(&hub, &row).await?;
        let host = gobby_terminals::control::Inventory {
            epoch: "epoch-E".into(),
            ..Default::default()
        };
        let mut settings = options();
        settings.dead_host_epochs.insert("epoch-DEAD".into());
        let runtime = Runtime {
            process: status,
            ..Default::default()
        };
        reconcile::reconcile(
            &hub.pool,
            row.machine_id,
            Observation::Accepted(&host),
            &settings,
            &runtime,
        )
        .await?;
        if status == ProcessStatus::Dead {
            let after = hub.get(row.id).await?;
            assert_eq!(after.state, State::Exited);
            assert_eq!(run_status(&hub, &row).await?, "cancelled");
            hub.fault(Some("terminal_write")).await?;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &settings,
                &runtime,
            )
            .await?;
            assert_eq!(hub.get(row.id).await?, after);
            hub.fault(None).await?;
        } else {
            assert_eq!(hub.get(row.id).await?, row);
            assert_eq!(run_status(&hub, &row).await?, "pending");
        }
    }
    Ok(())
}

#[tokio::test]
async fn dead_epoch_evidence_preserves_unproven_listed_and_held_rows() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = live(terminal(), "epoch-DEAD");
    let same = live(
        Terminal {
            machine_id: row.machine_id,
            ..terminal()
        },
        "epoch-E",
    );
    let newer = live(
        Terminal {
            machine_id: row.machine_id,
            ..terminal()
        },
        "epoch-NEW",
    );
    let foreign = live(terminal(), "epoch-DEAD");
    let held = live(
        Terminal {
            machine_id: row.machine_id,
            ..terminal()
        },
        "epoch-DEAD",
    );
    let listed = live(
        Terminal {
            machine_id: row.machine_id,
            ..terminal()
        },
        "epoch-DEAD",
    );
    for candidate in [&row, &same, &newer, &foreign, &held, &listed] {
        hub.insert(candidate).await?;
        seed_run(&hub, candidate).await?;
    }
    let host = inventory(&listed, "committed");
    let runtime = Runtime {
        process: ProcessStatus::Alive,
        held: Some(held.id),
        ..Default::default()
    };
    // Empty evidence introduces no dead-epoch settlements.
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &runtime,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?, row);
    let mut settings = options();
    settings
        .dead_host_epochs
        .extend(["epoch-DEAD".into(), "epoch-E".into()]);
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &settings,
        &runtime,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?.state, State::Orphaned);
    for candidate in [&same, &newer, &foreign, &held, &listed] {
        assert_eq!(hub.get(candidate.id).await?, *candidate);
        assert_eq!(run_status(&hub, candidate).await?, "pending");
    }
    Ok(())
}

#[tokio::test]
async fn dead_epoch_settlement_rolls_back_terminal_and_run_together() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for state in [State::Live, State::Orphaned] {
        for boundary in [
            "terminal_write",
            "before_run_write",
            "after_run_write",
            "commit",
        ] {
            let mut row = live(terminal(), "epoch-DEAD");
            row.state = state;
            hub.insert(&row).await?;
            seed_run(&hub, &row).await?;
            let host = gobby_terminals::control::Inventory {
                epoch: "epoch-E".into(),
                ..Default::default()
            };
            let mut settings = options();
            settings.dead_host_epochs.insert("epoch-DEAD".into());
            hub.fault(Some(boundary)).await?;
            let runtime = Runtime {
                failure: Some(boundary),
                ..Default::default()
            };
            assert!(
                reconcile::reconcile(
                    &hub.pool,
                    row.machine_id,
                    Observation::Accepted(&host),
                    &settings,
                    &runtime
                )
                .await
                .is_err(),
                "{state:?}/{boundary}"
            );
            assert_eq!(hub.get(row.id).await?, row);
            assert_eq!(run_status(&hub, &row).await?, "pending");
            hub.fault(None).await?;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &settings,
                &Runtime::default(),
            )
            .await?;
            assert_eq!(
                hub.get(row.id).await?.state,
                if state == State::Live {
                    State::Orphaned
                } else {
                    State::Exited
                }
            );
            assert_eq!(run_status(&hub, &row).await?, "cancelled");
        }
    }
    Ok(())
}

#[tokio::test]
async fn dead_epoch_row_lock_rechecks_rebound_identity() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for change in ["epoch", "attempt", "machine", "run"] {
        let row = live(terminal(), "epoch-DEAD");
        hub.insert(&row).await?;
        seed_run(&hub, &row).await?;
        let host = gobby_terminals::control::Inventory {
            epoch: "epoch-E".into(),
            ..Default::default()
        };
        let mut settings = options();
        settings.dead_host_epochs.insert("epoch-DEAD".into());
        let locked = tokio::sync::Notify::new();
        let captured = std::sync::Arc::new(tokio::sync::Notify::new());
        let runtime = Runtime {
            snapshot_signal: Some(captured.clone()),
            ..Default::default()
        };
        let rebind = hub.pool.transaction(None, async |tx| {
            repository::get_for_update(tx, row.id).await?.expect("locked row");
            locked.notify_one();
            tokio::time::timeout(Duration::from_secs(5), captured.notified()).await?;
            let sql = match change {
                "epoch" => "UPDATE terminals SET host_epoch='epoch-NEW' WHERE id=$1::text::uuid",
                "attempt" => "UPDATE terminals SET attempt_generation=attempt_generation+1 WHERE id=$1::text::uuid",
                "machine" => "UPDATE terminals SET machine_id=$2::text::uuid WHERE id=$1::text::uuid",
                "run" => "UPDATE terminals SET agent_run_id=$2::text::uuid WHERE id=$1::text::uuid",
                _ => unreachable!("fixture change"),
            };
            if matches!(change, "machine" | "run") {
                tx.execute(sql, &[&row.id.to_string(), &Uuid::new_v4().to_string()]).await?;
            } else {
                tx.execute(sql, &[&row.id.to_string()]).await?;
            }
            anyhow::Ok(repository::get(tx, row.id).await?.expect("rebound row"))
        });
        let observe = async {
            locked.notified().await;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &settings,
                &runtime,
            )
            .await
        };
        let (expected, outcome) = tokio::join!(rebind, observe);
        outcome?;
        assert_eq!(hub.get(row.id).await?, expected?, "stale {change} snapshot");
        assert_eq!(run_status(&hub, &row).await?, "pending");
    }
    Ok(())
}

fn inventory(row: &Terminal, commit: &str) -> gobby_terminals::control::Inventory {
    gobby_terminals::control::Inventory {
        epoch: "epoch-E".into(),
        seq: 8,
        terminals: vec![json!({
            "terminal_id": row.id.to_string(), "spawn_key": row.spawn_key,
            "host_terminal_id": "slot-1", "commit_state": commit,
            "pgid": 1234, "start_time": 123.25
        })],
    }
}

async fn seed_run(hub: &Hub, row: &Terminal) -> anyhow::Result<()> {
    hub.pool.transaction(None, async |tx| {
        tx.execute(
            "INSERT INTO agent_runs(id,machine_id,status) VALUES($1::text::uuid,$2::text::uuid,'pending')",
            &[&row.agent_run_id.expect("run id").to_string(), &row.machine_id.to_string()],
        ).await?;
        anyhow::Ok(())
    }).await
}

async fn run_status(hub: &Hub, row: &Terminal) -> anyhow::Result<String> {
    hub.pool
        .transaction(None, async |tx| {
            Ok(tx
                .query_one(
                    "SELECT status FROM agent_runs WHERE id=$1::text::uuid",
                    &[&row.agent_run_id.expect("run id").to_string()],
                )
                .await?
                .try_get("status")?)
        })
        .await
}

#[tokio::test]
async fn matching_inventory_promotes_once() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = terminal();
    hub.insert(&row).await?;
    seed_run(&hub, &row).await?;
    let host = inventory(&row, "committed");
    let runtime = Runtime::default();
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &runtime,
    )
    .await?;
    let first = hub.get(row.id).await?;
    assert_eq!(first.state, State::Live);
    assert_eq!(first.host_epoch.as_deref(), Some("epoch-E"));
    assert_eq!(first.locator, Some(json!({"host_terminal_id": "slot-1"})));
    assert_eq!(first.locator_key.as_deref(), Some("native:epoch-E:slot-1"));
    assert_eq!(first.unresolved_writes, row.unresolved_writes);
    assert_eq!(
        first.automatic_write_quarantined_at,
        row.automatic_write_quarantined_at
    );
    assert_eq!(run_status(&hub, &row).await?, "running");
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &runtime,
    )
    .await?;
    assert_eq!(
        hub.get(row.id).await?,
        first,
        "replay must not write the row again"
    );
    assert!(runtime.killed.lock().expect("kill log").is_empty());
    Ok(())
}

fn live(mut row: Terminal, epoch: &str) -> Terminal {
    row.state = State::Live;
    row.host_epoch = Some(epoch.into());
    row.locator = Some(json!({"host_terminal_id": row.id.to_string()}));
    row.locator_key = Some(format!("native:{epoch}:{}", row.id));
    row
}

#[tokio::test]
async fn absence_is_scoped_to_observed_machine_and_epoch() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let pending = Terminal {
        agent_run_id: None,
        ..terminal()
    };
    let same = live(
        Terminal {
            machine_id: pending.machine_id,
            agent_run_id: None,
            ..terminal()
        },
        "epoch-E",
    );
    let newer = live(
        Terminal {
            machine_id: pending.machine_id,
            agent_run_id: None,
            ..terminal()
        },
        "epoch-NEW",
    );
    let foreign = live(terminal(), "epoch-E");
    let young = Terminal {
        machine_id: pending.machine_id,
        agent_run_id: None,
        attempt_started_at: options().now,
        ..terminal()
    };
    for row in [&pending, &same, &newer, &foreign, &young] {
        hub.insert(row).await?;
    }
    let host = gobby_terminals::control::Inventory {
        epoch: "epoch-E".into(),
        ..Default::default()
    };
    reconcile::reconcile(
        &hub.pool,
        pending.machine_id,
        Observation::Accepted(&host),
        &options(),
        &Runtime::default(),
    )
    .await?;
    assert_eq!(hub.get(pending.id).await?.state, State::Exited);
    assert_eq!(hub.get(same.id).await?.state, State::Exited);
    assert_eq!(hub.get(newer.id).await?, newer);
    assert_eq!(hub.get(foreign.id).await?, foreign);
    assert_eq!(hub.get(young.id).await?, young);
    Ok(())
}

#[tokio::test]
async fn identity_conflicts_fail_closed_without_touching_refused_host() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = Terminal {
        agent_run_id: None,
        ..terminal()
    };
    hub.insert(&row).await?;
    let runtime = Runtime::default();
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Refused,
        &options(),
        &runtime,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?, row);
    assert!(runtime.killed.lock().expect("kill log").is_empty());
    let mut duplicate = inventory(&row, "committed");
    duplicate.terminals.push(duplicate.terminals[0].clone());
    assert!(
        reconcile::reconcile(
            &hub.pool,
            row.machine_id,
            Observation::Accepted(&duplicate),
            &options(),
            &runtime
        )
        .await
        .is_err()
    );
    assert_eq!(hub.get(row.id).await?, row);
    let mut wrong_key = inventory(&row, "committed");
    wrong_key.terminals[0]["spawn_key"] = json!("foreign-attempt");
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&wrong_key),
        &options(),
        &runtime,
    )
    .await?;
    assert_eq!(*runtime.killed.lock().expect("kill log"), vec!["slot-1"]);
    assert_eq!(*runtime.epochs.lock().expect("epoch log"), vec!["epoch-E"]);
    assert_eq!(hub.get(row.id).await?.state, State::Exited);
    Ok(())
}

#[tokio::test]
async fn spawn_settlement_is_atomic_across_failure_boundaries() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for commit in ["committed", "prepared"] {
        for boundary in [
            "terminal_write",
            "before_run_write",
            "after_run_write",
            "commit",
        ] {
            let row = terminal();
            hub.insert(&row).await?;
            seed_run(&hub, &row).await?;
            let mut host = inventory(&row, commit);
            host.terminals[0]["host_terminal_id"] = json!(row.id.to_string());
            hub.fault(Some(boundary)).await?;
            let runtime = Runtime {
                failure: Some(boundary),
                ..Default::default()
            };
            let mut settings = options();
            settings.settle_indeterminate = true;
            let failed = reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &settings,
                &runtime,
            )
            .await;
            assert!(
                failed.is_err(),
                "injected evidence boundary: {commit}/{boundary}"
            );
            assert_eq!(
                hub.get(row.id).await?,
                row,
                "terminal update must roll back: {commit}/{boundary}"
            );
            assert_eq!(
                run_status(&hub, &row).await?,
                "pending",
                "run evidence must roll back: {commit}/{boundary}"
            );
            // A prepared child remains tracked by the complete pre-state even if a kill already succeeded.
            assert_eq!(hub.get(row.id).await?.process, row.process);
            hub.fault(None).await?;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &settings,
                &Runtime::default(),
            )
            .await?;
            let after = hub.get(row.id).await?;
            assert_eq!(
                after.state,
                if commit == "committed" {
                    State::Live
                } else {
                    State::Exited
                }
            );
            assert_eq!(
                run_status(&hub, &row).await?,
                if commit == "committed" {
                    "running"
                } else {
                    "cancelled"
                }
            );
        }
    }
    Ok(())
}

#[tokio::test]
async fn duplicate_spawn_keys_and_host_slots_are_rejected_before_any_effect() -> anyhow::Result<()>
{
    let hub = Hub::new().await?;
    let row = terminal();
    hub.insert(&row).await?;
    seed_run(&hub, &row).await?;
    for conflict in ["spawn_key", "host_slot"] {
        let mut host = inventory(&row, "committed");
        let mut duplicate = host.terminals[0].clone();
        duplicate["terminal_id"] = json!(Uuid::new_v4().to_string());
        if conflict == "spawn_key" {
            duplicate["host_terminal_id"] = json!("slot-2");
        } else {
            duplicate["spawn_key"] = json!("another-spawn");
        }
        host.terminals.push(duplicate);
        let runtime = Runtime::default();
        assert!(
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &options(),
                &runtime
            )
            .await
            .is_err(),
            "{conflict}"
        );
        assert_eq!(
            hub.get(row.id).await?,
            row,
            "reject entire conflicting inventory: {conflict}"
        );
        assert!(runtime.killed.lock().expect("kill log").is_empty());
    }
    Ok(())
}

#[tokio::test]
async fn listed_foreign_rows_new_epochs_and_local_spawn_holds_are_fenced() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let machine = Uuid::new_v4();
    let foreign = terminal();
    let newer = live(
        Terminal {
            machine_id: machine,
            ..terminal()
        },
        "epoch-NEW",
    );
    let held = Terminal {
        machine_id: machine,
        ..terminal()
    };
    for row in [&foreign, &newer, &held] {
        hub.insert(row).await?;
    }
    let runtime = Runtime {
        held: Some(held.id),
        ..Default::default()
    };
    let mut host = inventory(&foreign, "committed");
    let mut new_entry = inventory(&newer, "committed").terminals.remove(0);
    new_entry["host_terminal_id"] = json!("new-slot");
    host.terminals.push(new_entry);
    let mut held_entry = inventory(&held, "committed").terminals.remove(0);
    held_entry["host_terminal_id"] = json!("held-slot");
    host.terminals.push(held_entry);
    host.terminals
        .push(json!({"terminal_id": "tmux:/tmp/owned:1:2:%42", "spawn_key": null}));
    reconcile::reconcile(
        &hub.pool,
        machine,
        Observation::Accepted(&host),
        &options(),
        &runtime,
    )
    .await?;
    assert_eq!(*runtime.killed.lock().expect("kill log"), vec!["slot-1"]);
    for row in [&foreign, &newer, &held] {
        assert_eq!(hub.get(row.id).await?, *row);
    }
    Ok(())
}

#[tokio::test]
async fn prepared_children_require_kill_truth_and_keep_reap_evidence() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for uncertain in [false, true] {
        let row = terminal();
        hub.insert(&row).await?;
        seed_run(&hub, &row).await?;
        let host = inventory(&row, "prepared");
        let runtime = Runtime {
            kill_proven: false,
            failure: if uncertain { Some("kill") } else { None },
            ..Default::default()
        };
        let mut settings = options();
        settings.settle_indeterminate = true;
        let result = reconcile::reconcile(
            &hub.pool,
            row.machine_id,
            Observation::Accepted(&host),
            &settings,
            &runtime,
        )
        .await;
        assert_eq!(result.is_err(), uncertain);
        let pending = hub.get(row.id).await?;
        assert_eq!(pending.state, State::Pending);
        assert_eq!(
            pending.process.as_ref().expect("tracked process")["pgid"],
            1234
        );
        assert_eq!(
            pending.process.as_ref().expect("tracked process")["attempt_owner"],
            "retained"
        );
        assert_eq!(run_status(&hub, &row).await?, "pending");
        if uncertain {
            assert_eq!(pending, row);
        }
    }
    Ok(())
}

#[tokio::test]
async fn unknown_or_foreign_children_cannot_succeed_without_proven_kill() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for known_foreign in [false, true] {
        let row = terminal();
        if known_foreign {
            hub.insert(&row).await?;
        }
        let host = inventory(&row, "committed");
        let runtime = Runtime {
            kill_proven: false,
            ..Default::default()
        };
        assert!(
            reconcile::reconcile(
                &hub.pool,
                Uuid::new_v4(),
                Observation::Accepted(&host),
                &options(),
                &runtime
            )
            .await
            .is_err(),
            "unproven kill must reject reconciliation, foreign={known_foreign}"
        );
        if known_foreign {
            assert_eq!(hub.get(row.id).await?, row);
        }
    }
    Ok(())
}

#[tokio::test]
async fn absence_preserves_alive_groups_and_settles_orphans() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = live(
        Terminal {
            agent_run_id: None,
            ..terminal()
        },
        "epoch-E",
    );
    hub.insert(&row).await?;
    let host = gobby_terminals::control::Inventory {
        epoch: "epoch-E".into(),
        ..Default::default()
    };
    let alive = Runtime {
        process: ProcessStatus::Alive,
        ..Default::default()
    };
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &alive,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?, row);
    let uncertain = Runtime {
        process: ProcessStatus::Unknown,
        ..Default::default()
    };
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &uncertain,
    )
    .await?;
    assert_eq!(hub.get(row.id).await?.state, State::Orphaned);
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &options(),
        &alive,
    )
    .await?;
    assert_eq!(
        hub.get(row.id).await?.state,
        State::Exited,
        "answered inventory proves same-epoch orphan absence"
    );
    Ok(())
}

#[tokio::test]
async fn orphan_rebind_replaces_stale_process_identity() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for pgid in [None, Some(5678)] {
        let mut row = live(
            Terminal {
                agent_run_id: None,
                ..terminal()
            },
            "epoch-E",
        );
        row.state = State::Orphaned;
        row.process = Some(json!({
            "host_terminal_id": "old-slot", "pgid": 1234,
            "start_time": 123.25, "attempt_owner": "retained"
        }));
        hub.insert(&row).await?;
        let mut host = inventory(&row, "committed");
        let listing = host.terminals[0].as_object_mut().expect("inventory object");
        listing.remove("pgid");
        listing.remove("start_time");
        if let Some(pgid) = pgid {
            listing.insert("pgid".into(), json!(pgid));
        }
        reconcile::reconcile(
            &hub.pool,
            row.machine_id,
            Observation::Accepted(&host),
            &options(),
            &Runtime::default(),
        )
        .await?;
        let rebound = hub.get(row.id).await?;
        let process = rebound.process.as_ref().expect("rebound process");
        assert_eq!(rebound.state, State::Orphaned);
        assert_eq!(rebound.locator, Some(json!({"host_terminal_id": "slot-1"})));
        assert_eq!(process["host_terminal_id"], "slot-1");
        assert_eq!(process["attempt_owner"], "retained");
        assert_eq!(process.get("pgid"), pgid.map(|id| json!(id)).as_ref());
        assert!(
            process.get("start_time").is_none(),
            "stale start_time retained"
        );
        let before_repeat = rebound;
        reconcile::reconcile(
            &hub.pool,
            row.machine_id,
            Observation::Accepted(&host),
            &options(),
            &Runtime::default(),
        )
        .await?;
        assert_eq!(hub.get(row.id).await?, before_repeat);
    }
    Ok(())
}

#[tokio::test]
async fn future_pending_attempt_is_not_stale_with_zero_grace() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let mut settings = options();
    settings.spawn_in_doubt = Duration::ZERO;
    let row = Terminal {
        agent_run_id: None,
        attempt_started_at: settings.now + chrono::TimeDelta::seconds(1),
        ..terminal()
    };
    hub.insert(&row).await?;
    let host = gobby_terminals::control::Inventory {
        epoch: "epoch-E".into(),
        ..Default::default()
    };
    reconcile::reconcile(
        &hub.pool,
        row.machine_id,
        Observation::Accepted(&host),
        &settings,
        &Runtime::default(),
    )
    .await?;
    assert_eq!(
        hub.get(row.id).await?,
        row,
        "future start time cannot prove stale pending absence"
    );
    Ok(())
}

#[tokio::test]
async fn missing_or_foreign_run_evidence_cannot_promote_a_terminal() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for foreign in [false, true] {
        let row = terminal();
        hub.insert(&row).await?;
        if foreign {
            seed_run(&hub, &row).await?;
            hub.pool
                .transaction(None, async |tx| {
                    tx.execute(
                        "UPDATE agent_runs SET terminal_id=$1::text::uuid WHERE id=$2::text::uuid",
                        &[
                            &Uuid::new_v4().to_string(),
                            &row.agent_run_id.expect("run").to_string(),
                        ],
                    )
                    .await?;
                    anyhow::Ok(())
                })
                .await?;
        }
        let host = inventory(&row, "committed");
        assert!(
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &options(),
                &Runtime::default()
            )
            .await
            .is_err()
        );
        assert_eq!(hub.get(row.id).await?, row);
        if foreign {
            assert_eq!(run_status(&hub, &row).await?, "pending");
        }
    }
    Ok(())
}

#[tokio::test]
async fn row_lock_rechecks_epoch_and_attempt_after_the_inventory_snapshot() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    for change in ["epoch", "attempt"] {
        let row = if change == "epoch" {
            live(terminal(), "epoch-E")
        } else {
            terminal()
        };
        hub.insert(&row).await?;
        let host = inventory(&row, "committed");
        let locked = tokio::sync::Notify::new();
        let captured = std::sync::Arc::new(tokio::sync::Notify::new());
        let runtime = Runtime {
            snapshot_signal: Some(captured.clone()),
            ..Default::default()
        };
        let rebind = hub.pool.transaction(None, async |tx| {
            repository::get_for_update(tx, row.id)
                .await?
                .expect("locked row");
            locked.notify_one();
            tokio::time::timeout(Duration::from_secs(5), captured.notified()).await?;
            if change == "epoch" {
                tx.execute(
                    "UPDATE terminals SET host_epoch='epoch-NEW',
                    locator_key='native:epoch-NEW:slot-NEW',
                    locator='{\"host_terminal_id\":\"slot-NEW\"}'::jsonb WHERE id=$1::text::uuid",
                    &[&row.id.to_string()],
                )
                .await?;
            } else {
                tx.execute(
                    "UPDATE terminals SET attempt_generation=attempt_generation+1,
                    attempt_started_at=$2::text::timestamptz WHERE id=$1::text::uuid",
                    &[&row.id.to_string(), &options().now.to_rfc3339()],
                )
                .await?;
            }
            anyhow::Ok(repository::get(tx, row.id).await?.expect("rebound row"))
        });
        let observe = async {
            locked.notified().await;
            reconcile::reconcile(
                &hub.pool,
                row.machine_id,
                Observation::Accepted(&host),
                &options(),
                &runtime,
            )
            .await
        };
        let (expected, outcome) = tokio::join!(rebind, observe);
        outcome?;
        assert_eq!(
            hub.get(row.id).await?,
            expected?,
            "captured {change} is stale"
        );
        assert!(runtime.killed.lock().expect("kill log").is_empty());
    }
    Ok(())
}
