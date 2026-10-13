use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use super::super::{
    EpochAuthority, EpochBinding, HostFailure, HostInfo, HostOptions, HostSupervisor,
};

#[derive(Default)]
struct Exits(
    Mutex<Vec<(uuid::Uuid, String)>>,
    std::sync::atomic::AtomicBool,
);

impl EpochAuthority for Exits {
    fn bindings<'a>(
        &'a self,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn observe_exit<'a>(
        &'a self,
        machine: uuid::Uuid,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(async move {
            if self.1.swap(false, std::sync::atomic::Ordering::SeqCst) {
                return Err(super::authority_failure(
                    "injected exit-record storage failure",
                ));
            }
            self.0.lock().unwrap().push((machine, epoch.into()));
            Ok(())
        })
    }
}

#[tokio::test]
async fn supervisor_records_owned_child_exit_once_and_keeps_live_epochs() -> anyhow::Result<()> {
    let machine = uuid::Uuid::new_v4();
    let exits = Arc::new(Exits::default());
    let supervisor = HostSupervisor::new(
        HostOptions {
            machine_id: machine.to_string(),
            ..HostOptions::default()
        },
        exits.clone(),
    );
    let process = tokio::process::Command::new("/bin/sleep")
        .arg("600")
        .kill_on_drop(true)
        .spawn()?;
    let mut known = Some(
        HostInfo {
            pid: process.id().unwrap(),
            epoch: "opaque-observed".into(),
            version: "fixture".into(),
            adopted: false,
        }
        .into(),
    );
    let mut child = Some(process);
    super::observe_process_exit(&supervisor.inner, &mut known, &mut child).await?;
    assert!(exits.0.lock().unwrap().is_empty());
    assert!(known.is_some());
    child.as_mut().unwrap().kill().await?;
    super::observe_process_exit(&supervisor.inner, &mut known, &mut child).await?;
    assert_eq!(
        *exits.0.lock().unwrap(),
        vec![(machine, "opaque-observed".into())]
    );
    assert!(known.is_none());
    assert!(child.is_none());
    super::observe_process_exit(&supervisor.inner, &mut known, &mut child).await?;
    assert_eq!(exits.0.lock().unwrap().len(), 1);
    Ok(())
}

#[tokio::test]
async fn supervisor_retries_exit_record_even_if_the_pid_is_reused() -> anyhow::Result<()> {
    let machine = uuid::Uuid::new_v4();
    let exits = Arc::new(Exits::default());
    let supervisor = HostSupervisor::new(
        HostOptions {
            machine_id: machine.to_string(),
            ..HostOptions::default()
        },
        exits.clone(),
    );
    let process = tokio::process::Command::new("/bin/sleep")
        .arg("600")
        .kill_on_drop(true)
        .spawn()?;
    let mut known = Some(
        HostInfo {
            pid: process.id().unwrap(),
            epoch: "exit-before-storage-failure".into(),
            version: "fixture".into(),
            adopted: false,
        }
        .into(),
    );
    let mut child = Some(process);
    child.as_mut().unwrap().kill().await?;
    exits.1.store(true, std::sync::atomic::Ordering::SeqCst);
    assert!(
        super::observe_process_exit(&supervisor.inner, &mut known, &mut child)
            .await
            .is_err()
    );
    assert!(exits.0.lock().unwrap().is_empty());
    // Deterministically model reuse with a known live PID before retrying.
    known.as_mut().unwrap().info.pid = std::process::id();
    child = None;
    super::observe_process_exit(&supervisor.inner, &mut known, &mut child).await?;
    assert_eq!(
        *exits.0.lock().unwrap(),
        vec![(machine, "exit-before-storage-failure".into())]
    );
    assert!(known.is_none());
    Ok(())
}

#[tokio::test]
async fn supervisor_records_confirmed_drain_exit_before_pid_disappears() -> anyhow::Result<()> {
    let machine = uuid::Uuid::new_v4();
    let exits = Arc::new(Exits::default());
    let supervisor = HostSupervisor::new(
        HostOptions {
            machine_id: machine.to_string(),
            ..HostOptions::default()
        },
        exits.clone(),
    );
    let pid = std::process::id();
    *supervisor.inner.last_host.lock().unwrap() = Some(HostInfo {
        pid,
        epoch: "confirmed-shutdown".into(),
        version: "fixture".into(),
        adopted: true,
    });
    // The shutdown observer has exit proof; a zombie/reused PID may still exist.
    super::record_confirmed_exit(&supervisor.inner, pid).await?;
    assert_eq!(
        *exits.0.lock().unwrap(),
        vec![(machine, "confirmed-shutdown".into())]
    );
    assert!(supervisor.inner.last_host.lock().unwrap().is_none());
    Ok(())
}
