#![cfg(unix)]

use anyhow::{Context, Result, ensure};
use futures_util::FutureExt;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_daemon::lifecycle::shutdown_intent::{Intent, IntentMarker};
use gobby_daemon::{
    lease::{ActiveDaemonLease, LeaseMode, NamedLock},
    lifecycle::{
        backend::{
            BackendConfig, BackendStatus, BackendSupervisor, LeaseScoped, SupervisorExit,
            crash_delay,
        },
        pid_file::{Role, claim_pid_file},
    },
};
use gobby_terminals::write_api::WriteRefused;

use serde_json::json;
use std::fs;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tempfile::TempDir;
use tokio::{
    sync::watch,
    time::{sleep, timeout},
};
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

use super::lease::{LIFETIME_OPEN, hub_lock};

use axum::body::{Body, to_bytes};
use axum::http::{Method, Request, StatusCode};
use gobby_core::bootstrap::{HubDatabaseBootstrap, RouteBackend, RunMode};
use gobby_core::family::lifecycle::{Activation, HookError, JobRun, StopFailure, StopMode};
use gobby_core::family::{ConfigEpoch, Extensions, FramePublisher, PublishRefused};
use gobby_core::route_family::{BoxFut, RouteFamily};
use gobby_daemon::family_start::{ConfigEpochSource, PrepareInputs, prepare};
use gobby_daemon::front_door::FrontDoorState;
use gobby_daemon::front_door::routes::terminal_route_latch;
use gobby_daemon::lease::LeaseState;
use gobby_daemon::state::RuntimeState;
use std::collections::BTreeMap;
use std::sync::Mutex;
use tokio::sync::Notify;

/// The repo's synced `.venv`. A cold managed env makes `uv run` sync the whole project inside
/// the test deadline.
fn repo_venv() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.venv")
}

fn backend_argv(script: &str) -> Result<String> {
    // `uv run` is another process plus project discovery on every start. That
    // startup, with the interpreter and the gobby import, is what stretches
    // under host load. The synced venv interpreter imports gobby on its own.
    let python = repo_venv().join("bin").join("python");
    let python = python.to_str().context("venv python path is not utf-8")?;
    serde_json::to_string(&[python, "-c", script]).context("encode backend argv")
}

fn backend_config(home: &Path, argv: &str) -> Result<BackendConfig> {
    let mut config = BackendConfig::from_command_json(home, Some(argv))?;
    config.environment.insert(
        "UV_PROJECT_ENVIRONMENT".into(),
        repo_venv().into_os_string(),
    );
    // The environment is already seeded; a child must never sync protected files.
    config.environment.insert("UV_NO_SYNC".into(), "1".into());
    Ok(config)
}

#[test]
fn backend_argv_execs_the_venv_python() -> Result<()> {
    let parsed: Vec<String> = serde_json::from_str(&backend_argv("pass")?)?;
    let python = repo_venv().join("bin").join("python");
    assert_eq!(
        parsed,
        vec![
            python
                .to_str()
                .context("venv python path is not utf-8")?
                .to_owned(),
            "-c".into(),
            "pass".into(),
        ]
    );
    Ok(())
}

struct TestDb {
    admin: Client,
    url: String,
    schema: String,
}

impl TestDb {
    fn lease(&self, machine: &str) -> Result<ActiveDaemonLease> {
        let pool = Pool::build(
            &self.url,
            PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-lifecycle-test".into(),
                acquire_timeout: Duration::from_secs(5),
            },
        )?;
        ActiveDaemonLease::new(Arc::new(pool), machine, "lifecycle-test", LeaseMode::Hub)
    }

    async fn supervisor(&self, home: &Path, script: &str) -> Result<BackendSupervisor> {
        self.supervisor_with(home, script, |_| {}).await
    }

    async fn supervisor_with(
        &self,
        home: &Path,
        script: &str,
        tune: impl FnOnce(&mut BackendConfig),
    ) -> Result<BackendSupervisor> {
        let mut lease = self.lease("supervisor")?;
        ensure!(lease.try_acquire().await?, "fixture must hold lease");
        let claim = claim_pid_file(&home.join("gobby.pid"), Role::Daemon)?
            .ok_or_else(|| anyhow::anyhow!("fixture PID claim unavailable"))?;
        let argv = backend_argv(script)?;
        let mut config = backend_config(home, &argv)?;
        config
            .environment
            .insert("HOME".into(), home.as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        // Under fleet load a backend can take seconds to exit on its own. A short
        // grace drains it mid-exit; the lingering test sets 100ms to cover that path.
        config.exit_grace = Duration::from_secs(5);
        config.report_interval = Duration::from_secs(60);
        tune(&mut config);
        Ok(BackendSupervisor::new(lease, claim, config))
    }
}

async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL lifecycle test");
        return Ok(());
    };
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "lifecycle tests require protected isolated test hub"
    );
    config.application_name(format!("gdaemon-lifecycle-fixture:{}", std::process::id()));
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    // The lock is shared with every worktree's lease and lifecycle runs on this hub, and a
    // waiter queues behind whole test bodies from those runs. The bound must outlast several
    // of them, so it is a hang guard, not a fairness budget.
    super::lease::lock_fixture_key(&admin, Duration::from_secs(300)).await?;
    // Armed only after the lock is held, so a waiter's own timeout still covers the queue.
    let _watchdog = hub_lock::arm(hub_lock::HOLD_DEADLINE);
    let schema = format!("test_rust_lifecycle_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; CREATE TABLE {schema}.deployment_runtime \
        (deployment_token text PRIMARY KEY, fencing_epoch bigint NOT NULL, \
        grant_signing_secret text NOT NULL, epoch_updated_at timestamptz NOT NULL)"
        ))
        .await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = TestDb {
        admin,
        schema: schema.clone(),
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
    };
    let outcome = std::panic::AssertUnwindSafe(case(&db)).catch_unwind().await;
    let cleanup = db
        .admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
        .await;
    drop(db);
    driver.abort();
    match outcome {
        Ok(result) => {
            cleanup?;
            result
        }
        Err(panic) => {
            if let Err(error) = cleanup {
                eprintln!("cleanup: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

async fn wait_for_status(
    state: &mut watch::Receiver<BackendStatus>,
    predicate: impl Fn(&BackendStatus) -> bool,
) -> Result<BackendStatus> {
    timeout(Duration::from_secs(10), async {
        loop {
            let current = state.borrow_and_update().clone();
            if predicate(&current) {
                return Ok(current);
            }
            state.changed().await?;
        }
    })
    .await?
}

struct AbortOnDrop(tokio::task::AbortHandle);
impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// The peer must build its pool, acquire the lease, claim its PID file and start a `uv run`
/// backend before any file appears. That chain runs under the same host load as the rest of
/// the suite, so the bound is a hang guard sized for it, and the error names the awaited file.
async fn wait_for_file(path: &Path) -> Result<String> {
    timeout(Duration::from_secs(60), async {
        loop {
            match tokio::fs::read_to_string(path).await {
                Ok(value) if !value.is_empty() => return Ok(value),
                Ok(_) => sleep(Duration::from_millis(10)).await,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    sleep(Duration::from_millis(10)).await
                }
                Err(error) => return Err(error.into()),
            }
        }
    })
    .await
    .with_context(|| format!("waiting for {}", path.display()))?
}

#[tokio::test]
async fn start_refusal_is_not_respawned() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = "import os,pathlib,sys; p=pathlib.Path(os.environ['GOBBY_HOME'])/'starts'; p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('schema mismatch',file=sys.stderr); sys.exit(78)";
        let mut supervisor = db.supervisor(home.path(), script).await?;
        let (_shutdown, rx) = watch::channel(false);
        assert_eq!(timeout(Duration::from_secs(10), supervisor.run(rx.clone())).await??,
            SupervisorExit::Refused);
        assert_eq!(*supervisor.subscribe().borrow(), BackendStatus::Refused { reason: "schema mismatch".into() });
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "x");
        assert_eq!(supervisor.run(rx).await?, SupervisorExit::Refused);
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "x");
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_none(),
            "refused supervisor keeps machine-local PID claim");
        Ok(())
    }).await
}

#[tokio::test]
async fn refused_daemon_releases_lease_to_standby() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let mut supervisor = db
            .supervisor(home.path(), "import sys; sys.exit(78)")
            .await?;
        let mut standby = db.lease("standby")?;
        assert!(!standby.try_acquire().await?);
        let (_tx, rx) = watch::channel(false);
        assert_eq!(
            timeout(Duration::from_secs(10), supervisor.run(rx.clone())).await??,
            SupervisorExit::Refused
        );
        assert!(
            standby.try_acquire().await?,
            "refusal releases lease to healthy standby"
        );
        assert_eq!(standby.fence().map(|f| f.epoch), Some(2));
        let healthy_home=TempDir::new()?;
        let claim=claim_pid_file(&healthy_home.path().join("gobby.pid"),Role::Daemon)?.context("standby PID claim")?;
        let script="import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'ready').write_text('healthy'); open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()";
        let argv=serde_json::to_string(&["uv","run","--no-sync","python","-c",script])?;
        let mut config=backend_config(healthy_home.path(), &argv)?;
        config.drain_timeout=Duration::from_millis(100);
        let mut healthy=BackendSupervisor::new(standby,claim,config);
        let (stop,stop_rx)=watch::channel(false);
        let task=tokio::spawn(async move {healthy.run(stop_rx).await});
        let _abort=AbortOnDrop(task.abort_handle());
        assert_eq!(wait_for_file(&healthy_home.path().join("ready")).await?,"healthy");
        stop.send(true)?;
        assert_eq!(timeout(Duration::from_secs(5),task).await???,SupervisorExit::Stopped);
        assert_eq!(
            timeout(Duration::from_secs(10), supervisor.run(rx)).await??,
            SupervisorExit::Refused
        );
        let mut standby=db.lease("next-standby")?;
        assert!(standby.try_acquire().await?, "refused is never a contender");
        Ok(())
    })
    .await
}

#[tokio::test]
async fn lifecycle_backend_disables_environment_sync() -> Result<()> {
    let home = TempDir::new()?;
    let claim = claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?
        .context("fixture PID claim")?;
    // The explicit flag keeps RED safe before the fixture exports UV_NO_SYNC.
    let script = "import os,pathlib,sys; (pathlib.Path(os.environ['GOBBY_HOME'])/'sync').write_text(os.environ.get('UV_NO_SYNC', 'missing')); sys.exit(78)";
    let argv = serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", script])?;
    let config = backend_config(home.path(), &argv)?;
    let mut supervisor = BackendSupervisor::with_lease(None, claim, config);
    let (_stop, rx) = watch::channel(false);
    assert_eq!(
        timeout(Duration::from_secs(10), supervisor.run(rx)).await??,
        SupervisorExit::Refused
    );
    assert_eq!(fs::read_to_string(home.path().join("sync"))?, "1");
    Ok(())
}

#[tokio::test]
async fn supervisor_restarts_crashed_backend() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = r#"import os,pathlib,time
p=pathlib.Path(os.environ['GOBBY_HOME'])/'starts'
n=len(p.read_text())+1 if p.exists() else 1
p.write_text('a'*n)
(p.parent/f'spawn-{n}').write_text(str(time.monotonic()))
if n<3: raise SystemExit(1)
(pathlib.Path(os.environ['GOBBY_HOME'])/'stable').write_text('ready')
open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()
"#;
        let mut supervisor = db.supervisor(home.path(), script).await?;
        let mut state = supervisor.subscribe();
        let (tx, rx) = watch::channel(false);
        let task = tokio::spawn(async move {
            let outcome = supervisor.run(rx).await;
            (outcome, supervisor)
        });
        let _abort = AbortOnDrop(task.abort_handle());
        let first =
            wait_for_status(&mut state, |s| matches!(s, BackendStatus::Backoff { .. })).await?;
        assert_eq!(
            first,
            BackendStatus::Backoff {
                delay: Duration::from_secs(1)
            }
        );
        wait_for_status(&mut state, |s| matches!(s, BackendStatus::Starting { .. })).await?;
        let second =
            wait_for_status(&mut state, |s| matches!(s, BackendStatus::Backoff { .. })).await?;
        assert_eq!(
            second,
            BackendStatus::Backoff {
                delay: Duration::from_secs(2)
            }
        );
        wait_for_status(&mut state, |s| matches!(s, BackendStatus::Starting { .. })).await?;
        assert_eq!(wait_for_file(&home.path().join("stable")).await?, "ready");
        let first: f64 = fs::read_to_string(home.path().join("spawn-1"))?.parse()?;
        let second: f64 = fs::read_to_string(home.path().join("spawn-2"))?.parse()?;
        let third: f64 = fs::read_to_string(home.path().join("spawn-3"))?.parse()?;
        assert!(
            second - first >= 1.0,
            "first crash waits at least one second"
        );
        assert!(
            third - second >= 2.0,
            "second crash waits at least two seconds"
        );
        tx.send(true)?;
        let (outcome, _supervisor) = timeout(Duration::from_secs(5), task).await??;
        assert_eq!(outcome?, SupervisorExit::Stopped);
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "aaa");
        Ok(())
    })
    .await
}

#[tokio::test]
async fn backend_write_token_rotates_per_spawn_over_the_pipe_only() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = r#"import os,pathlib,json,sys
home=pathlib.Path(os.environ['GOBBY_HOME']); fd=int(os.environ['GOBBY_SUPERVISOR_FD'])
line=os.read(fd,4096); token=line.decode().removesuffix('\n')
n=len(list(home.glob('token-*')))+1
(home/f'token-{n}').write_text(json.dumps({'line':line.decode(),'env':any(token in v for v in os.environ.values()),'argv':any(token in a for a in sys.argv)}))
if n<2: raise SystemExit(1)
os.read(fd,1)
"#;
        let mut lease = db.lease("token-rotation")?;
        ensure!(lease.try_acquire().await?, "fixture must hold lease");
        let claim = claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?
            .context("fixture PID claim")?;
        let argv = serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", script])?;
        let mut config = backend_config(home.path(), &argv)?;
        config.drain_timeout = Duration::from_millis(100);
        let slot = config.write_token.clone();
        assert_eq!(slot.authorize(None), Err(WriteRefused::NoBackend));
        let mut supervisor = BackendSupervisor::new(lease, claim, config);
        let (stop, stop_rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(stop_rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        let mut tokens = Vec::new();
        for n in 1..=2 {
            let report: serde_json::Value =
                serde_json::from_str(&wait_for_file(&home.path().join(format!("token-{n}"))).await?)?;
            assert_eq!(report["env"], json!(false), "token never travels in env");
            assert_eq!(report["argv"], json!(false), "token never travels in argv");
            let line = report["line"].as_str().context("token line")?;
            let token = line.strip_suffix('\n').context("token is one newline-ended line")?;
            assert_eq!(token.len(), 43, "32 random bytes, base64url without padding");
            tokens.push(token.to_owned());
        }
        assert_ne!(tokens[0], tokens[1], "each spawn mints a fresh token");
        assert_eq!(slot.authorize(Some(&tokens[1])), Ok(()));
        assert_eq!(slot.authorize(Some(&tokens[0])), Err(WriteRefused::WrongToken));
        stop.send(true)?;
        assert_eq!(timeout(Duration::from_secs(5), task).await???, SupervisorExit::Stopped);
        assert_eq!(slot.authorize(Some(&tokens[1])), Err(WriteRefused::NoBackend));
        Ok(())
    })
    .await
}

#[test]
fn crash_backoff_caps_at_thirty_seconds() {
    let delays: Vec<_> = (0..8).map(|n| crash_delay(n).as_secs()).collect();
    assert_eq!(delays, [1, 2, 4, 8, 16, 30, 30, 30]);
    assert_eq!(crash_delay(u32::MAX), Duration::from_secs(30));
}

#[tokio::test]
async fn terminal_drain_marker_reaches_runtime_owner() -> Result<()> {
    database_case(async |db| {
        for (intent, expected) in [("stop", SupervisorExit::StopTerminals), ("restart", SupervisorExit::RestartTerminals)] {
            let home = TempDir::new()?;
            let script = format!("import os,pathlib,json,time; p=pathlib.Path(os.environ['GOBBY_HOME']); (p/'shutdown_intent_active.json').write_text(json.dumps({{'intent':'{intent}','timestamp':time.time(),'details':{{'drain_terminals':True}}}})); raise SystemExit(0)");
            let mut supervisor = db.supervisor(home.path(), &script).await?;
            let (stop, rx) = watch::channel(false);
            assert_eq!(timeout(Duration::from_secs(5), supervisor.run(rx.clone())).await??, expected);
            let pid_file = home.path().join("gobby.pid");
            if expected == SupervisorExit::RestartTerminals {
                assert!(claim_pid_file(&pid_file, Role::Daemon)?.is_none(), "host restart lost native PID ownership");
                stop.send(true)?;
                assert_eq!(supervisor.run(rx).await?, SupervisorExit::Stopped);
            }
            // The PID artifact persists; a fresh claim proves that stop released ownership.
            let successor = claim_pid_file(&pid_file, Role::Daemon)?
                .context("stopped supervisor retained native PID ownership")?;
            drop(successor);
        }
        Ok(())
    }).await
}

#[tokio::test]
async fn healthy_backend_resets_crash_backoff() -> Result<()> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let home = TempDir::new()?;
    let control = tokio::net::UnixListener::bind(home.path().join("exit.sock"))?;
    let script = r#"import os,pathlib,socket
home=pathlib.Path(os.environ['GOBBY_HOME'])
counter=home/'runs'
n=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(n))
if n==1: raise SystemExit(1)
control=socket.socket(socket.AF_UNIX)
control.connect(str(home/'exit.sock'))
control.sendall(b'ready')
control.recv(1)
raise SystemExit(1)
"#;
    let argv = serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", script])?;
    let mut config = backend_config(home.path(), &argv)?;
    config.drain_timeout = Duration::from_millis(100);
    let claim =
        claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("test PID claim")?;
    let mut supervisor = BackendSupervisor::with_lease(None, claim, config);
    let mut state = supervisor.subscribe();
    let (stop, rx) = watch::channel(false);
    let task = tokio::spawn(async move { supervisor.run(rx).await });
    let _abort = AbortOnDrop(task.abort_handle());
    wait_for_status(&mut state, |s| matches!(s, BackendStatus::Backoff { .. })).await?;
    let (mut control, _) = timeout(Duration::from_secs(5), control.accept()).await??;
    let mut ready = [0; 5];
    timeout(Duration::from_secs(5), control.read_exact(&mut ready)).await??;
    assert_eq!(&ready, b"ready");
    tokio::time::pause();
    tokio::time::advance(Duration::from_secs(31)).await;
    tokio::time::resume();
    control.write_all(b"x").await?;
    wait_for_status(&mut state, |s| matches!(s, BackendStatus::Backoff { .. })).await?;
    assert_eq!(
        *state.borrow(),
        BackendStatus::Backoff {
            delay: Duration::from_secs(1)
        }
    );
    stop.send(true)?;
    assert_eq!(
        timeout(Duration::from_secs(5), task).await???,
        SupervisorExit::Stopped
    );
    Ok(())
}

#[tokio::test]
async fn exited_backend_leaves_no_process_group_descendant() -> Result<()> {
    use tokio::io::AsyncWriteExt;
    for code in [1, 78, 0] {
        let home = TempDir::new()?;
        let control = tokio::net::UnixListener::bind(home.path().join("exit.sock"))?;
        let script = format!(
            r#"import os,pathlib,socket,subprocess,sys
home=pathlib.Path(os.environ['GOBBY_HOME'])
if (home/'first').exists():
    open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()
    raise SystemExit(0)
(home/'first').write_text('first')
child=subprocess.Popen([sys.executable,'-c','import signal; signal.pause()'])
(home/'descendant').write_text(str(child.pid))
control=socket.socket(socket.AF_UNIX)
control.connect(str(home/'exit.sock'))
control.recv(1)
if {code}==0:
    import json,time
    (home/'shutdown_intent_active.json').write_text(json.dumps({{'source':'test','intent':'restart','timestamp':time.time()}}))
raise SystemExit({code})
"#
        );
        let argv = serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", &script])?;
        let mut config = backend_config(home.path(), &argv)?;
        config.drain_timeout = Duration::from_millis(100);
        let claim = claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?
            .context("test PID claim")?;
        let mut supervisor = BackendSupervisor::with_lease(None, claim, config);
        let mut state = supervisor.subscribe();
        let (stop, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        let pid: i32 = wait_for_file(&home.path().join("descendant"))
            .await?
            .parse()?;
        let first_backend = state.borrow().clone();
        let (mut control, _) = timeout(Duration::from_secs(5), control.accept()).await??;
        control.write_all(b"x").await?;
        if code == 78 {
            wait_for_status(&mut state, |s| matches!(s, BackendStatus::Refused { .. })).await?;
        } else if code == 1 {
            wait_for_status(&mut state, |s| matches!(s, BackendStatus::Backoff { .. })).await?;
        } else {
            wait_for_status(&mut state, |s| {
                matches!(s, BackendStatus::Starting { .. }) && s != &first_backend
            })
            .await?;
        }
        if code != 78 {
            stop.send(true)?;
        }
        timeout(Duration::from_secs(5), task).await???;
        timeout(Duration::from_secs(5), async {
            let mut poll = tokio::time::interval(Duration::from_millis(20));
            loop {
                // SAFETY: signal zero observes only the PID written by this test's backend.
                if unsafe { libc::kill(pid, 0) } != 0 {
                    break;
                }
                poll.tick().await;
            }
        })
        .await
        .context("descendant survived leader exit")?;
    }
    Ok(())
}

#[tokio::test]
async fn python_written_shutdown_marker_is_a_golden() -> Result<()> {
    let home = TempDir::new()?;
    let raw = include_bytes!("../../../tests/fixtures/shutdown_intent_restart.json");
    let mut marker = IntentMarker::new(home.path());
    marker.before_spawn().await?;
    fs::write(home.path().join("shutdown_intent_active.json"), raw)?;
    assert_eq!(marker.after_exit(2010.0).await?, Some(Intent::Restart));
    assert_eq!(
        fs::read(home.path().join("shutdown_intent_active.json"))?,
        raw
    );
    Ok(())
}

#[tokio::test]
async fn standby_does_not_spawn_a_backend() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = db.lease("standby")?;
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let command = serde_json::to_string(&[
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            "raise RuntimeError('standby spawned')",
        ])?;
        let config = backend_config(home.path(), &command)?;
        let mut supervisor = BackendSupervisor::new(lease, claim, config);
        let (_tx, rx) = watch::channel(false);
        assert_eq!(supervisor.run(rx).await?, SupervisorExit::Standby);
        assert_eq!(*supervisor.subscribe().borrow(), BackendStatus::Standby);
        let mut contender = db.lease("active")?;
        assert!(contender.try_acquire().await?);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn pre_spawn_stop_marker_and_shutdown_in_backoff_do_not_respawn() -> Result<()> {
    database_case(async |db| {
        let home=TempDir::new()?;
        let marker=home.path().join("shutdown_intent_active.json");
        let now=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs_f64();
        let raw=serde_json::to_vec(&json!({"intent":"stop","timestamp":now,"sender_pid":1}))?;
        fs::write(&marker,&raw)?;
        let script="import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'starts').write_text('x'); raise SystemExit(1)";
        let mut supervisor=db.supervisor(home.path(),script).await?;
        let mut state=supervisor.subscribe();
        let (tx,rx)=watch::channel(false);
        let task=tokio::spawn(async move {supervisor.run(rx).await});
        let _abort=AbortOnDrop(task.abort_handle());
        assert_eq!(wait_for_status(&mut state,|s|matches!(s,BackendStatus::Backoff{..})).await?,
            BackendStatus::Backoff{delay:Duration::from_secs(1)});
        tx.send(true)?;
        assert_eq!(timeout(Duration::from_millis(500),task).await???,SupervisorExit::Stopped);
        assert_eq!(fs::read_to_string(home.path().join("starts"))?,"x");
        assert_eq!(fs::read(marker)?,raw,"pre-spawn marker kept for hooks");
        Ok(())
    }).await
}

/// Grace between SIGTERM and SIGKILL for the tests that check the TERM marker. A TERM handler
/// can run late under host load, so the grace must outlast it. At 100 ms the kill won that
/// race and both tests failed with ENOENT reading the marker.
const TERM_GRACE: Duration = Duration::from_secs(2);

const IGNORE_TERM: &str = r#"import os,pathlib,signal,time
home=pathlib.Path(os.environ['GOBBY_HOME'])
def term(*args):
    time.sleep(.3)
    (home/'term').write_text('TERM')
signal.signal(signal.SIGTERM,term)
(home/'ready').write_text(str(os.getpid()))
while True: time.sleep(.01)
"#;

#[tokio::test]
async fn shutdown_sends_term_then_bounded_kill() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let mut supervisor = db.supervisor(home.path(), IGNORE_TERM).await?;
        let (tx, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        let pid: i32 = wait_for_file(&home.path().join("ready")).await?.parse()?;
        let started = Instant::now();
        tx.send(true)?;
        assert_eq!(
            timeout(Duration::from_secs(10), task).await???,
            SupervisorExit::Stopped
        );
        assert!(started.elapsed() >= Duration::from_millis(90));
        assert!(started.elapsed() < Duration::from_secs(10));
        assert_eq!(fs::read_to_string(home.path().join("term"))?, "TERM");
        // SAFETY: signal zero only probes the already reaped isolated child.
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_some());
        Ok(())
    })
    .await
}

#[tokio::test]
async fn lease_loss_drains_the_backend_before_releasing_pid() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let mut lease = db.lease("loss-owner")?;
        ensure!(lease.try_acquire().await?);
        let application = lease.application_name().to_string();
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("claim")?;
        let argv = backend_argv(IGNORE_TERM)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        let mut supervisor = BackendSupervisor::new(lease, claim, config);
        let (_tx, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        let pid: i32 = wait_for_file(&home.path().join("ready")).await?.parse()?;
        let rows = db
            .admin
            .query(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=$1",
                &[&application],
            )
            .await?;
        assert_eq!(rows.len(), 1);
        assert_eq!(
            timeout(Duration::from_secs(10), task).await???,
            SupervisorExit::LeaseLost
        );
        assert_eq!(fs::read_to_string(home.path().join("term"))?, "TERM");
        // SAFETY: signal zero probes only the known isolated backend PID.
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        let mut standby = db.lease("after-loss")?;
        assert!(standby.try_acquire().await?);
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_some());
        Ok(())
    })
    .await
}

const EOF_BACKEND: &str = r#"import os,pathlib,json,stat
home=pathlib.Path(os.environ['GOBBY_HOME']); fd=int(os.environ['GOBBY_SUPERVISOR_FD'])
opened=[]
details={}
for candidate in range(3,512):
    try:
        info=os.fstat(candidate); opened.append(candidate)
        details[str(candidate)]={'mode':stat.S_IFMT(info.st_mode),'device':info.st_dev,'inode':info.st_ino}
        if pathlib.Path('/proc/self/fd').is_dir():
            details[str(candidate)]['target']=os.readlink('/proc/self/fd/'+str(candidate))
    except OSError: pass
(home/'descriptors').write_text(json.dumps({'pid':os.getpid(),'open':opened,'liveness':fd,'details':details,'cargo_makeflags':os.environ.get('CARGO_MAKEFLAGS'),'makeflags':os.environ.get('MAKEFLAGS')}))
assert os.read(fd,4096).endswith(b'\n')
assert os.read(fd,1)==b''
(home/'eof').write_text('EOF')
"#;

#[tokio::test]
async fn supervisor_death_gives_backend_eof() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let command =
            serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", EOF_BACKEND])?;
        let peer_path = Path::new(env!("CARGO_BIN_EXE_gdaemon"))
            .parent()
            .context("debug target directory")?
            .join("examples/backend_supervisor_fixture");
        assert!(
            peer_path.is_file(),
            "build backend_supervisor_fixture example first"
        );
        let mut peer = tokio::process::Command::new(peer_path)
            .env("DATABASE_URL", &db.url)
            .env("GOBBY_TEST_PROTECT", "1")
            .env("GOBBY_BACKEND_COMMAND", command)
            .env("UV_PROJECT_ENVIRONMENT", repo_venv())
            .env("GOBBY_LIFECYCLE_PEER_HOME", home.path())
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::inherit())
            .kill_on_drop(true)
            .spawn()?;
        let report: serde_json::Value =
            serde_json::from_str(&wait_for_file(&home.path().join("descriptors")).await?)?;
        assert_eq!(
            report["open"],
            json!([report["liveness"]]),
            "only liveness read FD survives exec: {report}"
        );
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_none());
        peer.start_kill()?;
        assert!(
            !timeout(Duration::from_secs(5), peer.wait())
                .await??
                .success()
        );
        assert_eq!(wait_for_file(&home.path().join("eof")).await?, "EOF");
        assert!(
            claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_some(),
            "orphan cannot retain supervisor PID lock"
        );
        let mut standby = db.lease("after-death")?;
        assert!(standby.try_acquire().await?);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn shutdown_intent_marker_drives_respawn() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script=r#"import os,pathlib,time
from gobby.shutdown_intent import write_shutdown_intent
home=pathlib.Path(os.environ['GOBBY_HOME']); p=home/'starts'
n=len(p.read_text())+1 if p.exists() else 1
p.write_text('a'*n)
if n==1: write_shutdown_intent('test_restart','restart',home=home)
if n==3: write_shutdown_intent('test_stop','stop',home=home)
raise SystemExit(0 if n!=2 else 1)
"#;
        let mut supervisor=db.supervisor(home.path(),script).await?;
        let (_tx,rx)=watch::channel(false);
        let started=Instant::now();
        // Hang guard for three interpreter starts plus the 1 s crash backoff.
        // The whole test finished in 6.5 s at 1-minute load about 50. Import
        // time grew with load; supervisor transitions stayed under 0.3 s.
        // 15 s is 8.5 s over that completion.
        assert_eq!(timeout(Duration::from_secs(15),supervisor.run(rx)).await??,SupervisorExit::Stopped);
        assert!(started.elapsed()>=Duration::from_secs(1), "consumed restart must back off second crash");
        assert_eq!(fs::read_to_string(home.path().join("starts"))?,"aaa");
        let epoch:i64=db.admin.query_one(&format!("SELECT fencing_epoch FROM {}.deployment_runtime WHERE deployment_token='lifecycle-test'",db.schema),&[]).await?.get(0);
        assert_eq!(epoch,1,"backend-only restart preserves lease epoch");
        assert!(claim_pid_file(&home.path().join("gobby.pid"),Role::Maintenance)?.is_some(),"stop releases PID ownership");
        Ok(())
    }).await
}

#[tokio::test]
async fn backend_stop_marker_exits_without_respawn() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = r#"import os,pathlib
from gobby.shutdown_intent import write_shutdown_intent
home=pathlib.Path(os.environ['GOBBY_HOME']); starts=home/'starts'
with starts.open('a') as output: output.write('a')
write_shutdown_intent('test_stop','stop',home=home)
raise SystemExit(0)
"#;
        let mut supervisor = db.supervisor(home.path(), script).await?;
        let (_tx, rx) = watch::channel(false);
        assert_eq!(
            timeout(Duration::from_secs(10), supervisor.run(rx)).await??,
            SupervisorExit::Stopped
        );
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "a");
        assert!(!home.path().join("shutdown_intent_active.json").exists());
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_some());
        Ok(())
    })
    .await
}

#[tokio::test]
async fn full_restart_marker_exits_without_respawn_and_survives_for_successor() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = r#"import os,pathlib
from gobby.shutdown_intent import write_shutdown_intent
home=pathlib.Path(os.environ['GOBBY_HOME']); starts=home/'starts'
with starts.open('a') as output: output.write('a')
write_shutdown_intent('cli_restart','restart',home=home,details={'restart_scope':'supervisor'})
raise SystemExit(0)
"#;
        let mut supervisor = db.supervisor(home.path(), script).await?;
        let (_tx, rx) = watch::channel(false);
        assert_eq!(
            timeout(Duration::from_secs(10), supervisor.run(rx)).await??,
            SupervisorExit::Stopped
        );
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "a");
        let path = home.path().join("shutdown_intent_active.json");
        let bytes = fs::read(&path)?;
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&bytes)?["intent"],
            "restart"
        );
        assert!(claim_pid_file(&home.path().join("gobby.pid"), Role::Maintenance)?.is_some());
        let mut successor = IntentMarker::new(home.path());
        successor.before_spawn().await?;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs_f64();
        assert_eq!(successor.after_exit(now).await?, None);
        assert_eq!(fs::read(path)?, bytes);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn future_shutdown_marker_uses_crash_backoff() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = "import os,pathlib,json,time; p=pathlib.Path(os.environ['GOBBY_HOME']); (p/'shutdown_intent_active.json').write_text(json.dumps({'intent':'restart','timestamp':time.time()+1})); raise SystemExit(1)";
        let mut supervisor = db.supervisor(home.path(), script).await?;
        let mut state = supervisor.subscribe();
        let (stop, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        assert_eq!(
            wait_for_status(&mut state, |status| matches!(status, BackendStatus::Backoff { .. })).await?,
            BackendStatus::Backoff { delay: Duration::from_secs(1) }
        );
        stop.send(true)?;
        assert_eq!(timeout(Duration::from_secs(5), task).await???, SupervisorExit::Stopped);
        assert!(home.path().join("shutdown_intent_active.json").is_file());
        Ok(())
    }).await
}

#[tokio::test]
async fn future_shutdown_marker_is_inert_until_its_timestamp() -> Result<()> {
    for intent in ["restart", "stop"] {
        let home = TempDir::new()?;
        let path = home.path().join("shutdown_intent_active.json");
        let mut marker = IntentMarker::new(home.path());
        marker.before_spawn().await?;
        let raw = serde_json::to_vec(&json!({"intent": intent, "timestamp": 1000.0}))?;
        fs::write(&path, &raw)?;
        assert_eq!(marker.after_exit(999.0).await?, None);
        assert_eq!(fs::read(&path)?, raw, "future marker is preserved");
        let expected = if intent == "restart" {
            Intent::Restart
        } else {
            Intent::Stop
        };
        assert_eq!(marker.after_exit(1000.0).await?, Some(expected));
        assert_eq!(
            marker.after_exit(1000.0).await?,
            None,
            "fresh action is one-use"
        );
    }
    Ok(())
}

#[tokio::test]
async fn shutdown_marker_is_fresh_changed_and_used_once() -> anyhow::Result<()> {
    let home = TempDir::new()?;
    let path = home.path().join("shutdown_intent_active.json");
    let restart = serde_json::to_vec(&json!({
        "intent": "restart", "timestamp": 1000.0, "sender_pid": 42, "source": "cli_restart"
    }))?;
    let mut marker = IntentMarker::new(home.path());
    marker.before_spawn().await?;
    fs::write(&path, &restart)?;
    assert_eq!(marker.after_exit(1010.0).await?, Some(Intent::Restart));
    assert_eq!(
        fs::read(&path)?,
        restart,
        "hook suppression marker stays present"
    );
    assert_eq!(
        marker.after_exit(1010.0).await?,
        None,
        "consumed bytes cannot act twice"
    );
    marker.before_spawn().await?;
    assert_eq!(
        marker.after_exit(1010.0).await?,
        None,
        "pre-spawn bytes are not a new intent"
    );
    fs::write(&path, br#"{"intent":"stop","timestamp":900.0}"#)?;
    assert_eq!(
        marker.after_exit(1020.0).await?,
        None,
        "age equal to 120s is stale"
    );
    fs::write(&path, br#"{"intent":"stop","timestamp":1011.0}"#)?;
    assert_eq!(marker.after_exit(1020.0).await?, Some(Intent::Stop));
    fs::write(&path, &restart)?;
    assert_eq!(
        marker.after_exit(1020.0).await?,
        None,
        "replayed consumed bytes stay inert"
    );
    Ok(())
}

const FRONT_DOOR_SECRET: &str = "lifecycle-front-door-secret";

const SECRET_BACKEND: &str = r#"import os,pathlib,json,sys
home=pathlib.Path(os.environ['GOBBY_HOME']); value=os.environ.get('GOBBY_FRONT_DOOR_SECRET_FD'); secret=None
if value is not None:
    with os.fdopen(int(value),'rb') as pipe: secret=pipe.read().decode()
(home/'secret').write_text(json.dumps({'secret':secret,'environ':dict(os.environ),'argv':sys.orig_argv}))
os.read(int(os.environ['GOBBY_SUPERVISOR_FD']),1)
"#;

#[tokio::test]
async fn front_door_secret_reaches_backend_only_on_its_pipe() -> Result<()> {
    let home = TempDir::new()?;
    let claim = claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?
        .context("fixture PID claim")?;
    let argv = serde_json::to_string(&["uv", "run", "--no-sync", "python", "-c", SECRET_BACKEND])?;
    let mut config = backend_config(home.path(), &argv)?;
    config.front_door_secret = Some(FRONT_DOOR_SECRET.into());
    config.drain_timeout = Duration::from_millis(100);
    let mut supervisor = BackendSupervisor::with_lease(None, claim, config);
    let (stop, stop_rx) = watch::channel(false);
    let task = tokio::spawn(async move { supervisor.run(stop_rx).await });
    let _abort = AbortOnDrop(task.abort_handle());
    let report: serde_json::Value =
        serde_json::from_str(&wait_for_file(&home.path().join("secret")).await?)?;
    assert_eq!(
        report["secret"], FRONT_DOOR_SECRET,
        "the secret arrives on its pipe"
    );
    assert!(report["environ"].get("GOBBY_FRONT_DOOR_SECRET").is_none());
    assert!(
        !report["environ"].to_string().contains(FRONT_DOOR_SECRET),
        "no environment value carries the secret"
    );
    assert!(
        !report["argv"].to_string().contains(FRONT_DOOR_SECRET),
        "argv never carries the secret"
    );
    stop.send(true)?;
    assert_eq!(
        timeout(Duration::from_secs(5), task).await???,
        SupervisorExit::Stopped
    );
    Ok(())
}

/// A backend that announces its own restart and then never exits must not hold the supervisor
/// in `Starting` forever. The announced intent is applied after the exit grace, which respawns
/// the backend; the respawned backend announces a stop that the supervisor also applies.
#[tokio::test]
async fn lingering_backend_after_announced_restart_is_drained_and_respawned() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let script = r#"import os,pathlib,signal
from gobby.shutdown_intent import write_shutdown_intent
home=pathlib.Path(os.environ['GOBBY_HOME']); p=home/'starts'
n=len(p.read_text())+1 if p.exists() else 1
p.write_text('a'*n)
write_shutdown_intent('test_restart' if n==1 else 'test_stop', 'restart' if n==1 else 'stop', home=home)
while True:
    signal.pause()
"#;
        let mut supervisor = db
            .supervisor_with(home.path(), script, |config| {
                config.exit_grace = Duration::from_millis(100);
            })
            .await?;
        let (_tx, rx) = watch::channel(false);
        assert_eq!(
            timeout(Duration::from_secs(15), supervisor.run(rx)).await??,
            SupervisorExit::Stopped,
            "the announced restart and stop are applied to lingering backends"
        );
        assert_eq!(fs::read_to_string(home.path().join("starts"))?, "aa");
        Ok(())
    })
    .await
}

/// A lease lock another task holds past the supervisor's bound fails the supervisor with an error
/// naming the holder, instead of waiting on the lock indefinitely.
#[tokio::test]
async fn lease_lock_held_by_another_task_fails_the_supervisor_at_its_bound() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = Arc::new(NamedLock::new(db.lease("lock-holder")?));
        let _held = lease.lock_as("test lease holder").await;
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv("pass")?;
        let mut config = backend_config(home.path(), &argv)?;
        config.lease_lock_limit = Duration::from_millis(300);
        config.report_interval = Duration::from_millis(100);
        let mut supervisor = BackendSupervisor::with_lease(Some(lease.clone()), claim, config);
        let (_stop, rx) = watch::channel(false);
        // The wait reason the front door reports, observed while the supervisor waits.
        let mut wait = supervisor.subscribe_wait();
        let observe = async {
            while wait.changed().await.is_ok() {
                if let Some(reason) = wait.borrow().clone() {
                    return Some(reason);
                }
            }
            None
        };
        let (outcome, observed) = tokio::join!(
            timeout(Duration::from_secs(10), supervisor.run(rx)),
            timeout(Duration::from_secs(5), observe),
        );
        let reason = observed?.expect("the supervisor publishes its wait reason");
        assert!(
            reason.contains("test lease holder"),
            "the wait reason must name the holder: {reason}"
        );
        let error = outcome?.expect_err("a lock held past the bound must fail the supervisor");
        assert!(
            format!("{error:#}").contains("test lease holder"),
            "the error must name the holder: {error:#}"
        );
        assert!(
            wait.borrow()
                .as_deref()
                .is_some_and(|reason| reason.contains("test lease holder")),
            "the front door must retain the blocker after supervision fails: {:?}",
            *wait.borrow()
        );
        Ok(())
    })
    .await
}

/// While a backend has not exited, the supervisor reports what it waits on at the configured
/// cadence, so a stuck start is visible in the daemon log instead of silent.
#[tokio::test]
async fn silent_backend_wait_is_reported_each_interval() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let (line_tx, mut lines) = tokio::sync::mpsc::unbounded_channel::<String>();
        let mut supervisor = db
            .supervisor_with(
                home.path(),
                "import signal\nwhile True:\n    signal.pause()\n",
                move |config| {
                    config.report_interval = Duration::from_millis(100);
                    config.reporter = Arc::new(move |line: &str| {
                        let _ = line_tx.send(line.to_owned());
                    });
                },
            )
            .await?;
        let (stop, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let mut reports = Vec::new();
        timeout(Duration::from_secs(10), async {
            while reports.len() < 3 {
                reports.push(lines.recv().await.expect("report channel open"));
            }
        })
        .await?;
        stop.send(true)?;
        assert_eq!(
            timeout(Duration::from_secs(10), task).await???,
            SupervisorExit::Stopped
        );
        while let Ok(line) = lines.try_recv() {
            reports.push(line);
        }
        assert!(
            reports
                .iter()
                .all(|line| line.contains("backend pid") && line.contains("waiting on its exit")),
            "{reports:?}"
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn silent_backend_wait_writes_progress_to_startup_timings() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        // The readiness bootstrap creates this file; the supervisor adds progress only to it.
        let timings = home.path().join("logs").join("startup-timings.jsonl");
        std::fs::create_dir_all(timings.parent().context("timings parent")?)?;
        std::fs::write(&timings, "")?;
        let mut supervisor = db
            .supervisor_with(
                home.path(),
                "import signal\nwhile True:\n    signal.pause()\n",
                |config| {
                    config.report_interval = Duration::from_millis(100);
                },
            )
            .await?;
        let (stop, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let observed = timeout(Duration::from_secs(10), async {
            let mut poll = tokio::time::interval(Duration::from_millis(50));
            loop {
                poll.tick().await;
                let text = std::fs::read_to_string(&timings).unwrap_or_default();
                let progress = text
                    .lines()
                    .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
                    .filter(|record| record["state"] == "progress")
                    .count();
                if progress >= 3 {
                    return progress;
                }
            }
        })
        .await;
        stop.send(true)?;
        assert_eq!(
            timeout(Duration::from_secs(10), task).await???,
            SupervisorExit::Stopped
        );
        ensure!(
            observed.is_ok(),
            "no progress reached startup timings during a silent wait: {:?}",
            std::fs::read_to_string(&timings).unwrap_or_default()
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn lease_lock_wait_writes_progress_to_startup_timings() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        // The harness creates this file before launch, so a wait that blocks before the
        // bootstrap child starts is recorded too.
        let timings = home.path().join("logs").join("startup-timings.jsonl");
        std::fs::create_dir_all(timings.parent().context("timings parent")?)?;
        std::fs::write(&timings, "")?;
        let lease = Arc::new(NamedLock::new(db.lease("lock-holder")?));
        let _held = lease.lock_as("test lease holder").await;
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv("pass")?;
        let mut config = backend_config(home.path(), &argv)?;
        config.lease_lock_limit = Duration::from_secs(1);
        config.report_interval = Duration::from_millis(100);
        let mut supervisor = BackendSupervisor::with_lease(Some(lease.clone()), claim, config);
        let (_stop, rx) = watch::channel(false);
        let outcome = timeout(Duration::from_secs(10), supervisor.run(rx)).await?;
        outcome.expect_err("a lock held past the bound must fail the supervisor");
        let text = std::fs::read_to_string(&timings)?;
        let progress = text
            .lines()
            .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
            .filter(|record| record["state"] == "progress")
            .count();
        ensure!(
            progress >= 3,
            "no progress reached startup timings during a lease-lock wait: {text:?}"
        );
        Ok(())
    })
    .await
}

/// A frame publisher with no subscribers.
struct NoFrames;

impl FramePublisher for NoFrames {
    fn publish_frames(
        &self,
        _filter: &serde_json::Value,
        _frames: Vec<String>,
    ) -> Result<(), PublishRefused> {
        Err(PublishRefused)
    }
}

/// A configuration store holding no row.
struct NoStoredEpoch;

impl ConfigEpochSource for NoStoredEpoch {
    fn read(
        &self,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<ConfigEpoch>> + Send + '_>> {
        Box::pin(async { Ok(ConfigEpoch::unpublished()) })
    }
}

/// Records the lease state at the moment a family job's run is dropped.
struct LeaseAtJobEnd {
    lease: watch::Receiver<LeaseState>,
    seen: Arc<Mutex<Option<LeaseState>>>,
}

impl Drop for LeaseAtJobEnd {
    fn drop(&mut self) {
        let state = *self.lease.borrow();
        *self
            .seen
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(state);
    }
}

/// A family whose activation registers one job that never finishes on its own.
struct HoldsAJob {
    lease: watch::Receiver<LeaseState>,
    running: Arc<Notify>,
    seen: Arc<Mutex<Option<LeaseState>>>,
}

impl RouteFamily<FrontDoorState> for HoldsAJob {
    fn name(&self) -> &'static str {
        "fixture_holds_a_job"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> axum::Router<FrontDoorState> {
        axum::Router::new()
    }

    fn activate<'a>(
        &'a self,
        activation: &'a mut Activation<'_>,
    ) -> BoxFut<'a, Result<(), HookError>> {
        let lease = self.lease.clone();
        let running = Arc::clone(&self.running);
        let seen = Arc::clone(&self.seen);
        let job: JobRun = Arc::new(move || {
            let guard = LeaseAtJobEnd {
                lease: lease.clone(),
                seen: Arc::clone(&seen),
            };
            let running = Arc::clone(&running);
            Box::pin(async move {
                let _guard = guard;
                running.notify_one();
                std::future::pending::<()>().await;
                Ok(())
            })
        });
        let registered =
            activation.register_job("fixture_holds_a_job.work", Duration::from_secs(1), job);
        Box::pin(async move {
            registered.map_err(|refused| HookError::Ordinary {
                message: format!("the fixture job was refused: {refused:?}"),
            })
        })
    }
}

const READY_UNTIL_EOF: &str = "import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'ready').write_text('ready'); open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()";

#[tokio::test]
async fn an_orderly_stop_drains_the_family_jobs_before_the_lease_is_released() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = db.lease("jobs-owner")?;
        let lease_state = lease.subscribe();
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(READY_UNTIL_EOF)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        let write_token = config.write_token.clone();
        let supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        let state = RuntimeState {
            mode: RunMode::Hub,
            lease: Some(lease),
            lease_state: Some(lease_state.clone()),
            backend: supervisor.subscribe(),
            backend_wait: supervisor.subscribe_wait(),
            write_token,
            terminal_route: Default::default(),
        };
        let running = Arc::new(Notify::new());
        let seen: Arc<Mutex<Option<LeaseState>>> = Arc::default();
        let family: &'static HoldsAJob = Box::leak(Box::new(HoldsAJob {
            lease: lease_state.clone(),
            running: Arc::clone(&running),
            seen: Arc::clone(&seen),
        }));
        let families = prepare(PrepareInputs {
            serve_pool: None,
            home: home.path().to_owned(),
            routes: BTreeMap::from([(family.name().to_owned(), RouteBackend::Native)]),
            publisher: Arc::new(NoFrames),
            backend: None,
            lease: Some(lease_state),
            epoch_source: Arc::new(NoStoredEpoch),
            cpu_count: 8,
            families: vec![family],
            extensions: Extensions::new(),
            startup: None,
        })
        .await
        .map_err(|failure| anyhow::anyhow!("{}: {}", failure.family, failure.message))?;
        let bootstrap = HubDatabaseBootstrap {
            mode: RunMode::Hub,
            hub_daemon_url: None,
            hub_cert: None,
            database_url: None,
            api_key: None,
            daemon_url: None,
            bind_host: "127.0.0.1".into(),
            daemon_port: 0,
            websocket_port: 0,
            front_door: Default::default(),
        };
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        let owner_home = home.path().to_owned();
        let (stop, shutdown) = watch::channel(false);
        let task = tokio::spawn(async move {
            state
                .run(
                    supervisor, shutdown, bootstrap, terminals, owner_home, families,
                )
                .await
        });
        let _abort = AbortOnDrop(task.abort_handle());

        timeout(Duration::from_secs(30), running.notified())
            .await
            .context("the family job never ran")?;
        stop.send(true)?;
        timeout(Duration::from_secs(30), task).await???;

        let at_job_end = *seen
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert_eq!(
            at_job_end,
            Some(LeaseState::Active),
            "the family job was still running when the lease was released"
        );
        Ok(())
    })
    .await
}

/// The lease state and the moment a timed family job's first run was dropped.
#[derive(Default)]
struct JobEnd {
    first: Mutex<Option<(LeaseState, Instant)>>,
    signal: Notify,
}

impl JobEnd {
    /// Waits for the first run's end.
    async fn wait(&self) -> (LeaseState, Instant) {
        loop {
            let ended = self.signal.notified();
            if let Some(end) = *self
                .first
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
            {
                return end;
            }
            ended.await;
        }
    }
}

/// Records the first end of a timed family job's run.
struct FirstJobEnd {
    lease: watch::Receiver<LeaseState>,
    ended: Arc<JobEnd>,
}

impl Drop for FirstJobEnd {
    fn drop(&mut self) {
        let state = *self.lease.borrow();
        self.ended
            .first
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get_or_insert((state, Instant::now()));
        self.ended.signal.notify_one();
    }
}

/// A family whose activation registers one job that never finishes on its own, signalling
/// each start and timing the first end.
struct TimesAJob {
    lease: watch::Receiver<LeaseState>,
    running: Arc<Notify>,
    ended: Arc<JobEnd>,
}

impl RouteFamily<FrontDoorState> for TimesAJob {
    fn name(&self) -> &'static str {
        "fixture_times_a_job"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> axum::Router<FrontDoorState> {
        axum::Router::new()
    }

    fn activate<'a>(
        &'a self,
        activation: &'a mut Activation<'_>,
    ) -> BoxFut<'a, Result<(), HookError>> {
        let lease = self.lease.clone();
        let running = Arc::clone(&self.running);
        let ended = Arc::clone(&self.ended);
        let job: JobRun = Arc::new(move || {
            let guard = FirstJobEnd {
                lease: lease.clone(),
                ended: Arc::clone(&ended),
            };
            let running = Arc::clone(&running);
            Box::pin(async move {
                let _guard = guard;
                running.notify_one();
                std::future::pending::<()>().await;
                Ok(())
            })
        });
        let registered =
            activation.register_job("fixture_times_a_job.work", Duration::from_secs(1), job);
        Box::pin(async move {
            registered.map_err(|refused| HookError::Ordinary {
                message: format!("the fixture job was refused: {refused:?}"),
            })
        })
    }
}

#[tokio::test]
async fn a_lost_lease_cancels_the_family_jobs_before_the_lease_is_released() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = db.lease("lost-jobs-owner")?;
        let application = lease.application_name().to_string();
        let observer = lease.subscribe();
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(READY_UNTIL_EOF)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        let write_token = config.write_token.clone();
        let supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        let mut statuses = supervisor.subscribe();
        // The runtime is given no lease watch, so its job host is unfenced: the exit hook of
        // the supervised lifetime is then the only thing that can end the job before release.
        let state = RuntimeState {
            mode: RunMode::Hub,
            lease: Some(lease),
            lease_state: None,
            backend: supervisor.subscribe(),
            backend_wait: supervisor.subscribe_wait(),
            write_token,
            terminal_route: Default::default(),
        };
        let running = Arc::new(Notify::new());
        let ended: Arc<JobEnd> = Arc::default();
        let family: &'static TimesAJob = Box::leak(Box::new(TimesAJob {
            lease: observer.clone(),
            running: Arc::clone(&running),
            ended: Arc::clone(&ended),
        }));
        let families = prepare(PrepareInputs {
            serve_pool: None,
            home: home.path().to_owned(),
            routes: BTreeMap::from([(family.name().to_owned(), RouteBackend::Native)]),
            publisher: Arc::new(NoFrames),
            backend: None,
            lease: Some(observer),
            epoch_source: Arc::new(NoStoredEpoch),
            cpu_count: 8,
            families: vec![family],
            extensions: Extensions::new(),
            startup: None,
        })
        .await
        .map_err(|failure| anyhow::anyhow!("{}: {}", failure.family, failure.message))?;
        let bootstrap = HubDatabaseBootstrap {
            mode: RunMode::Hub,
            hub_daemon_url: None,
            hub_cert: None,
            database_url: None,
            api_key: None,
            daemon_url: None,
            bind_host: "127.0.0.1".into(),
            daemon_port: 0,
            websocket_port: 0,
            front_door: Default::default(),
        };
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        let owner_home = home.path().to_owned();
        let (stop, shutdown) = watch::channel(false);
        let task = tokio::spawn(async move {
            state
                .run(
                    supervisor, shutdown, bootstrap, terminals, owner_home, families,
                )
                .await
        });
        let _abort = AbortOnDrop(task.abort_handle());

        timeout(Duration::from_secs(30), running.notified())
            .await
            .context("the family job never ran")?;
        wait_for_file(&home.path().join("ready")).await?;
        // The supervisor publishes Stopped just before it returns LeaseLost to the hook.
        let stopped = tokio::spawn(async move {
            statuses
                .wait_for(|status| *status == BackendStatus::Stopped)
                .await
                .map(|_| Instant::now())
        });
        let rows = db
            .admin
            .query(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=$1",
                &[&application],
            )
            .await?;
        assert_eq!(rows.len(), 1, "the lease session was not terminated");
        // A peer takes the freed lease, so the runtime stays in standby after the loss.
        let _peer = take_freed_lease(db, "lost-jobs-peer").await?;
        let stopped_at = timeout(Duration::from_secs(30), stopped)
            .await
            .context("the supervisor never stopped the backend")???;
        let (at_job_end, ended_at) = timeout(Duration::from_secs(30), ended.wait())
            .await
            .context("the family job never ended after the lease was lost")?;
        // Longer than the job's interval, so a job scheduled after the loss would have run.
        let started_after_loss = timeout(Duration::from_millis(2500), running.notified())
            .await
            .is_ok();
        stop.send(true)?;
        timeout(Duration::from_secs(30), task).await???;

        assert_eq!(
            at_job_end,
            LeaseState::Lost,
            "the family job was still running when the lost lease was released"
        );
        let grace = ended_at.saturating_duration_since(stopped_at);
        assert!(
            grace < gobby_daemon::heartbeat::SHUTDOWN_GRACE / 2,
            "the family job ended {grace:?} after the lease-lost exit; it must be cancelled at once, not drained"
        );
        assert!(
            !started_after_loss,
            "a family job started after the lease was lost"
        );
        Ok(())
    })
    .await
}

/// Writes the backend's PID once it is ready, then waits for the supervisor's EOF; the
/// shutdown drain's SIGTERM ends it.
const PID_UNTIL_EOF: &str = "import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'ready').write_text(str(os.getpid())); open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()";

#[tokio::test]
async fn a_lease_whose_session_ends_during_the_shutdown_drain_is_seen_as_lost_and_aborts_the_draining_job()
-> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = db.lease("drain-loss-owner")?;
        let application = lease.application_name().to_string();
        let observer = lease.subscribe();
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(PID_UNTIL_EOF)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        let write_token = config.write_token.clone();
        let supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        // The job host is fenced by the lease watch, so a published loss aborts its job.
        let state = RuntimeState {
            mode: RunMode::Hub,
            lease: Some(lease),
            lease_state: Some(observer.clone()),
            backend: supervisor.subscribe(),
            backend_wait: supervisor.subscribe_wait(),
            write_token,
            terminal_route: Default::default(),
        };
        let running = Arc::new(Notify::new());
        let ended: Arc<JobEnd> = Arc::default();
        let family: &'static TimesAJob = Box::leak(Box::new(TimesAJob {
            lease: observer.clone(),
            running: Arc::clone(&running),
            ended: Arc::clone(&ended),
        }));
        let families = prepare(PrepareInputs {
            serve_pool: None,
            home: home.path().to_owned(),
            routes: BTreeMap::from([(family.name().to_owned(), RouteBackend::Native)]),
            publisher: Arc::new(NoFrames),
            backend: None,
            lease: Some(observer),
            epoch_source: Arc::new(NoStoredEpoch),
            cpu_count: 8,
            families: vec![family],
            extensions: Extensions::new(),
            startup: None,
        })
        .await
        .map_err(|failure| anyhow::anyhow!("{}: {}", failure.family, failure.message))?;
        let bootstrap = HubDatabaseBootstrap {
            mode: RunMode::Hub,
            hub_daemon_url: None,
            hub_cert: None,
            database_url: None,
            api_key: None,
            daemon_url: None,
            bind_host: "127.0.0.1".into(),
            daemon_port: 0,
            websocket_port: 0,
            front_door: Default::default(),
        };
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        let owner_home = home.path().to_owned();
        let (stop, shutdown) = watch::channel(false);
        let task = tokio::spawn(async move {
            state
                .run(
                    supervisor, shutdown, bootstrap, terminals, owner_home, families,
                )
                .await
        });
        let _abort = AbortOnDrop(task.abort_handle());

        timeout(Duration::from_secs(30), running.notified())
            .await
            .context("the family job never ran")?;
        let pid: i32 = wait_for_file(&home.path().join("ready")).await?.parse()?;
        stop.send(true)?;
        // The worker drain reaps the backend just before the exit hook drains the jobs, so
        // the session ends after the heartbeat loop has been left, with the job still running.
        timeout(Duration::from_secs(30), async {
            let mut poll = tokio::time::interval(Duration::from_millis(10));
            loop {
                // SAFETY: signal zero observes only the PID written by this test's backend.
                if unsafe { libc::kill(pid, 0) } != 0 {
                    break;
                }
                poll.tick().await;
            }
        })
        .await
        .context("the shutdown drain never reaped the backend")?;
        let terminated_at = Instant::now();
        let rows = db
            .admin
            .query(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=$1",
                &[&application],
            )
            .await?;
        assert_eq!(rows.len(), 1, "the lease session was not terminated");
        let (at_job_end, ended_at) = timeout(Duration::from_secs(30), ended.wait())
            .await
            .context("the family job never ended after the lease session was terminated")?;

        assert_eq!(
            at_job_end,
            LeaseState::Lost,
            "the lease watch did not reach Lost before the draining job ended"
        );
        let grace = ended_at.saturating_duration_since(terminated_at);
        assert!(
            grace < gobby_daemon::heartbeat::SHUTDOWN_GRACE / 2,
            "the draining job ended {grace:?} after the lease session ended; it must be aborted at once, not drained"
        );
        timeout(Duration::from_secs(30), task).await???;
        Ok(())
    })
    .await
}

/// A pre-release heartbeat that fails without a failed lease query (here the lease lock held past
/// the supervisor's bound) publishes no loss of its own; the monitor must still fail closed.
#[tokio::test]
async fn a_pre_release_heartbeat_error_that_publishes_no_loss_is_seen_as_lost_and_aborts_the_draining_job()
-> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let lease = db.lease("drain-lock-owner")?;
        let observer = lease.subscribe();
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(PID_UNTIL_EOF)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        config.lease_lock_limit = Duration::from_millis(500);
        let write_token = config.write_token.clone();
        let supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        // The job host is fenced by the lease watch, so a published loss aborts its job.
        let state = RuntimeState {
            mode: RunMode::Hub,
            lease: Some(Arc::clone(&lease)),
            lease_state: Some(observer.clone()),
            backend: supervisor.subscribe(),
            backend_wait: supervisor.subscribe_wait(),
            write_token,
            terminal_route: Default::default(),
        };
        let running = Arc::new(Notify::new());
        let ended: Arc<JobEnd> = Arc::default();
        let family: &'static TimesAJob = Box::leak(Box::new(TimesAJob {
            lease: observer.clone(),
            running: Arc::clone(&running),
            ended: Arc::clone(&ended),
        }));
        let families = prepare(PrepareInputs {
            serve_pool: None,
            home: home.path().to_owned(),
            routes: BTreeMap::from([(family.name().to_owned(), RouteBackend::Native)]),
            publisher: Arc::new(NoFrames),
            backend: None,
            lease: Some(observer),
            epoch_source: Arc::new(NoStoredEpoch),
            cpu_count: 8,
            families: vec![family],
            extensions: Extensions::new(),
            startup: None,
        })
        .await
        .map_err(|failure| anyhow::anyhow!("{}: {}", failure.family, failure.message))?;
        let bootstrap = HubDatabaseBootstrap {
            mode: RunMode::Hub,
            hub_daemon_url: None,
            hub_cert: None,
            database_url: None,
            api_key: None,
            daemon_url: None,
            bind_host: "127.0.0.1".into(),
            daemon_port: 0,
            websocket_port: 0,
            front_door: Default::default(),
        };
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        let owner_home = home.path().to_owned();
        let (stop, shutdown) = watch::channel(false);
        let task = tokio::spawn(async move {
            state
                .run(
                    supervisor, shutdown, bootstrap, terminals, owner_home, families,
                )
                .await
        });
        let _abort = AbortOnDrop(task.abort_handle());

        timeout(Duration::from_secs(30), running.notified())
            .await
            .context("the family job never ran")?;
        let pid: i32 = wait_for_file(&home.path().join("ready")).await?.parse()?;
        stop.send(true)?;
        timeout(Duration::from_secs(30), async {
            let mut poll = tokio::time::interval(Duration::from_millis(10));
            loop {
                // SAFETY: signal zero observes only the PID written by this test's backend.
                if unsafe { libc::kill(pid, 0) } != 0 {
                    break;
                }
                poll.tick().await;
            }
        })
        .await
        .context("the shutdown drain never reaped the backend")?;
        // The PostgreSQL session stays healthy: only the lock wait of the next pre-release
        // heartbeat fails, at its bound, while the exit hook drains the job.
        let held = lease.lock_as("test lease holder").await;
        let held_at = Instant::now();
        let (at_job_end, ended_at) = timeout(Duration::from_secs(30), ended.wait())
            .await
            .context("the family job never ended while the lease lock was held")?;
        // Release waits on the same lock, so it is freed once the job has ended.
        drop(held);

        assert_eq!(
            at_job_end,
            LeaseState::Lost,
            "the lease watch did not reach Lost after the pre-release heartbeat failed"
        );
        let grace = ended_at.saturating_duration_since(held_at);
        assert!(
            grace < gobby_daemon::heartbeat::SHUTDOWN_GRACE / 2,
            "the draining job ended {grace:?} after the lease lock was held; it must be aborted once the heartbeat fails, not drained"
        );
        timeout(Duration::from_secs(30), task).await???;
        Ok(())
    })
    .await
}

/// A loss the pre-release monitor publishes without the lease lock leaves the session open. The
/// next lifetime's acquisition must not reuse that authority under a Lost watch, or the fenced
/// job host would run no job for the whole lifetime: it acquires afresh and publishes Active.
#[tokio::test]
async fn authority_published_lost_without_the_lock_is_acquired_afresh() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("published-loss")?;
        assert!(owner.try_acquire().await?);
        let old = owner.fence().context("first authority")?.epoch;
        owner.loss().publish();
        assert_eq!(*owner.subscribe().borrow(), LeaseState::Lost);
        assert!(
            owner.try_acquire().await?,
            "the lease was not acquired again after its published loss"
        );
        assert_eq!(
            *owner.subscribe().borrow(),
            LeaseState::Active,
            "the lease watch stayed Lost after the lease was acquired again"
        );
        assert_eq!(
            owner.fence().context("new authority")?.epoch,
            old + 1,
            "a lease published as lost reused its spent authority"
        );
        owner.release().await
    })
    .await
}

/// Takes the lease a terminated session held, once PostgreSQL has freed its lock. The
/// bound is a hang guard: the terminated backend frees the lock as it exits.
async fn take_freed_lease(db: &TestDb, machine: &str) -> Result<ActiveDaemonLease> {
    let mut peer = db.lease(machine)?;
    timeout(Duration::from_secs(10), async {
        while !peer.try_acquire().await? {
            sleep(Duration::from_millis(10)).await;
        }
        anyhow::Ok(())
    })
    .await
    .context("the terminated session's lease was never freed")??;
    Ok(peer)
}

impl TestDb {
    /// The fencing epoch of the lifecycle fixtures' deployment row.
    async fn epoch(&self) -> Result<i64> {
        Ok(self
            .admin
            .query_one(
                &format!(
                    "SELECT fencing_epoch FROM {}.deployment_runtime \
                     WHERE deployment_token = 'lifecycle-test'",
                    self.schema
                ),
                &[],
            )
            .await?
            .try_get(0)?)
    }
}

/// Ends the lease's dedicated PostgreSQL session, as a crashed or partitioned database would.
async fn terminate_lease_session(db: &TestDb, application: &str) -> Result<()> {
    let rows = db
        .admin
        .query(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=$1",
            &[&application],
        )
        .await?;
    ensure!(rows.len() == 1, "the lease session was not terminated");
    Ok(())
}

/// Waits until no session holds the lease's PostgreSQL lock. The bound is a hang guard.
async fn wait_until_unowned(db: &TestDb) -> Result<()> {
    let probe = db.lease("probe")?;
    timeout(Duration::from_secs(10), async {
        while probe.owner().await?.is_some() {
            sleep(Duration::from_millis(10)).await;
        }
        anyhow::Ok(())
    })
    .await
    .context("the lease lock was never freed")?
}

/// Calls a native lease admin route in-process and decodes its JSON body.
pub(super) async fn lease_admin(
    state: &RuntimeState,
    method: Method,
    path: &str,
) -> Result<(StatusCode, serde_json::Value)> {
    let request = Request::builder()
        .method(method)
        .uri(path)
        .body(Body::empty())?;
    let response = gobby_daemon::lease::admin_api::handle(state, request).await;
    let status = response.status();
    let body = serde_json::from_slice(&to_bytes(response.into_body(), 65536).await?)?;
    Ok((status, body))
}

/// A family whose hooks report and can pause. `activate` signals `activating`, waits for
/// `proceed` when given, and registers a job that signals `running` and never ends on its own.
/// `deactivate` records the lease state it first ran under, signals `deactivating`, and waits
/// for `resume` when given.
struct PausesHooks {
    lease: watch::Receiver<LeaseState>,
    activating: Arc<Notify>,
    proceed: Option<Arc<Notify>>,
    running: Arc<Notify>,
    at_deactivate: Arc<Mutex<Option<LeaseState>>>,
    deactivating: Arc<Notify>,
    resume: Option<Arc<Notify>>,
}

impl PausesHooks {
    /// Hooks that never pause, with fresh handles.
    fn new(lease: watch::Receiver<LeaseState>) -> Self {
        Self {
            lease,
            activating: Arc::new(Notify::new()),
            proceed: None,
            running: Arc::new(Notify::new()),
            at_deactivate: Arc::default(),
            deactivating: Arc::new(Notify::new()),
            resume: None,
        }
    }
}

impl RouteFamily<FrontDoorState> for PausesHooks {
    fn name(&self) -> &'static str {
        "fixture_pauses_hooks"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> axum::Router<FrontDoorState> {
        axum::Router::new()
    }

    fn activate<'a>(
        &'a self,
        activation: &'a mut Activation<'_>,
    ) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async move {
            // `notify_one` stores a permit, so a test that waits later still sees the signal.
            self.activating.notify_one();
            if let Some(proceed) = &self.proceed {
                proceed.notified().await;
            }
            let running = Arc::clone(&self.running);
            let job: JobRun = Arc::new(move || {
                let running = Arc::clone(&running);
                Box::pin(async move {
                    running.notify_one();
                    std::future::pending::<()>().await;
                    Ok(())
                })
            });
            activation
                .register_job("fixture_pauses_hooks.work", Duration::from_secs(1), job)
                .map_err(|refused| HookError::Ordinary {
                    message: format!("the fixture job was refused: {refused:?}"),
                })
        })
    }

    fn deactivate<'a>(&'a self, _mode: StopMode) -> BoxFut<'a, Result<(), StopFailure>> {
        Box::pin(async move {
            let state = *self.lease.borrow();
            self.at_deactivate
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .get_or_insert(state);
            self.deactivating.notify_one();
            if let Some(resume) = &self.resume {
                resume.notified().await;
            }
            Ok(())
        })
    }
}

/// A runtime started through `RuntimeState::run` on a fresh lease, with its handles.
struct Runtime {
    state: Arc<RuntimeState>,
    lease: Arc<NamedLock<ActiveDaemonLease>>,
    observer: watch::Receiver<LeaseState>,
    application: String,
    stop: watch::Sender<bool>,
    task: tokio::task::JoinHandle<Result<()>>,
    home: TempDir,
}

/// Starts `RuntimeState::run` with a fenced job host, one family built from the lease's watch,
/// a 100 ms heartbeat and a `TERM_GRACE` drain unless `tune` changes them.
async fn start_runtime(
    db: &TestDb,
    machine: &str,
    script: &str,
    tune: impl FnOnce(&mut BackendConfig),
    family: impl FnOnce(watch::Receiver<LeaseState>) -> &'static dyn RouteFamily<FrontDoorState>,
) -> Result<Runtime> {
    let home = TempDir::new()?;
    let lease = db.lease(machine)?;
    let application = lease.application_name().to_owned();
    let observer = lease.subscribe();
    let lease = Arc::new(NamedLock::new(lease));
    let claim =
        claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
    let argv = backend_argv(script)?;
    let mut config = backend_config(home.path(), &argv)?;
    config
        .environment
        .insert("HOME".into(), home.path().as_os_str().to_owned());
    config.drain_timeout = TERM_GRACE;
    config.heartbeat_interval = Duration::from_millis(100);
    tune(&mut config);
    let write_token = config.write_token.clone();
    let supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
    let state = Arc::new(RuntimeState {
        mode: RunMode::Hub,
        lease: Some(Arc::clone(&lease)),
        lease_state: Some(observer.clone()),
        backend: supervisor.subscribe(),
        backend_wait: supervisor.subscribe_wait(),
        write_token,
        terminal_route: Default::default(),
    });
    let family = family(observer.clone());
    let families = prepare(PrepareInputs {
        serve_pool: None,
        home: home.path().to_owned(),
        routes: BTreeMap::from([(family.name().to_owned(), RouteBackend::Native)]),
        publisher: Arc::new(NoFrames),
        backend: None,
        lease: Some(observer.clone()),
        epoch_source: Arc::new(NoStoredEpoch),
        cpu_count: 8,
        families: vec![family],
        extensions: Extensions::new(),
        startup: None,
    })
    .await
    .map_err(|failure| anyhow::anyhow!("{}: {}", failure.family, failure.message))?;
    let bootstrap = HubDatabaseBootstrap {
        mode: RunMode::Hub,
        hub_daemon_url: None,
        hub_cert: None,
        database_url: None,
        api_key: None,
        daemon_url: None,
        bind_host: "127.0.0.1".into(),
        daemon_port: 0,
        websocket_port: 0,
        front_door: Default::default(),
    };
    let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
    let owner_home = home.path().to_owned();
    let (stop, shutdown) = watch::channel(false);
    let runner = Arc::clone(&state);
    let task = tokio::spawn(async move {
        runner
            .run(
                supervisor, shutdown, bootstrap, terminals, owner_home, families,
            )
            .await
    });
    Ok(Runtime {
        state,
        lease,
        observer,
        application,
        stop,
        task,
        home,
    })
}

/// Counts its starts in `starts`. The first start announces a restart of the terminals (the
/// marker of `terminal_drain_marker_reaches_runtime_owner`) and exits; every later start writes
/// `ready` and waits for the supervisor's EOF. The marker stays on disk, and the next
/// lifetime's pre-spawn snapshot makes it inert, so only the first start announces.
const RESTARTS_TERMINALS_ONCE: &str = r#"import os,pathlib,json,time
home=pathlib.Path(os.environ['GOBBY_HOME']); p=home/'starts'
n=len(p.read_text())+1 if p.exists() else 1
p.write_text('a'*n)
if n==1:
    (home/'shutdown_intent_active.json').write_text(json.dumps({'intent':'restart','timestamp':time.time(),'details':{'drain_terminals':True}}))
    raise SystemExit(0)
(home/'ready').write_text('ready')
open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()
"#;

#[tokio::test]
async fn a_promote_while_a_lost_lease_drains_is_refused_and_installs_no_epoch() -> Result<()> {
    database_case(async |db| {
        let running = Arc::new(Notify::new());
        let family_running = Arc::clone(&running);
        let runtime = start_runtime(
            db,
            "drain-promote-owner",
            IGNORE_TERM,
            |config| config.drain_timeout = Duration::from_secs(5),
            move |lease| {
                Box::leak(Box::new(TimesAJob {
                    lease,
                    running: family_running,
                    ended: Arc::default(),
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        let mut observer = runtime.observer.clone();
        let pid: i32 = wait_for_file(&runtime.home.path().join("ready"))
            .await?
            .parse()?;
        ensure!(
            db.epoch().await? == 1,
            "the runtime did not acquire the first epoch"
        );
        terminate_lease_session(db, &runtime.application).await?;
        timeout(
            Duration::from_secs(10),
            observer.wait_for(|state| *state == LeaseState::Lost),
        )
        .await
        .context("the lost lease was never published")??;
        wait_for_file(&runtime.home.path().join("term")).await?;
        wait_until_unowned(db).await?;
        ensure!(
            !runtime.task.is_finished(),
            "the runtime ended before the drain"
        );
        // SAFETY: signal zero probes only the PID written by this test's backend.
        ensure!(
            unsafe { libc::kill(pid, 0) } == 0,
            "the backend was gone before the promote"
        );

        let (status, body) =
            lease_admin(&runtime.state, Method::POST, "/api/admin/lease/promote").await?;
        assert_eq!(
            status,
            StatusCode::CONFLICT,
            "a promote during the lost lease's drain was not refused: {body}"
        );
        assert_eq!(body["detail"], LIFETIME_OPEN, "{body}");
        assert_eq!(db.epoch().await?, 1, "a refused promote installed an epoch");
        let (status, body) =
            lease_admin(&runtime.state, Method::POST, "/api/admin/lease/recover").await?;
        assert_eq!(
            status,
            StatusCode::CONFLICT,
            "a recover during the lost lease's drain was not refused: {body}"
        );
        timeout(
            Duration::from_secs(30),
            observer.wait_for(|state| *state == LeaseState::Active),
        )
        .await
        .context("the run loop never acquired the lease again after the drain")??;
        assert_eq!(
            db.epoch().await?,
            2,
            "the run loop's own acquisition after the drain is the only new epoch"
        );
        runtime.stop.send(true)?;
        timeout(Duration::from_secs(30), runtime.task).await???;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn a_promote_after_the_lease_is_released_and_before_the_services_stop_is_refused()
-> Result<()> {
    database_case(async |db| {
        let resume = Arc::new(Notify::new());
        let deactivating = Arc::new(Notify::new());
        let (hook_resume, hook_deactivating) = (Arc::clone(&resume), Arc::clone(&deactivating));
        let runtime = start_runtime(
            db,
            "stopping-owner",
            READY_UNTIL_EOF,
            |_| {},
            move |lease| {
                Box::leak(Box::new(PausesHooks {
                    resume: Some(hook_resume),
                    deactivating: hook_deactivating,
                    ..PausesHooks::new(lease)
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        wait_for_file(&runtime.home.path().join("ready")).await?;
        runtime.stop.send(true)?;
        timeout(Duration::from_secs(30), deactivating.notified())
            .await
            .context("the families were never deactivated")?;
        ensure!(
            !runtime.task.is_finished(),
            "the runtime ended before its services stopped"
        );

        let (status, body) =
            lease_admin(&runtime.state, Method::POST, "/api/admin/lease/promote").await?;
        assert_eq!(
            status,
            StatusCode::CONFLICT,
            "a promote on a stopping daemon was not refused: {body}"
        );
        assert_eq!(body["detail"], LIFETIME_OPEN, "{body}");
        assert_eq!(db.epoch().await?, 1, "a stopping daemon installed an epoch");
        resume.notify_one();
        timeout(Duration::from_secs(30), runtime.task).await???;
        let (status, body) =
            lease_admin(&runtime.state, Method::POST, "/api/admin/lease/promote").await?;
        assert_eq!(
            status,
            StatusCode::CONFLICT,
            "a promote after the runtime returned was not refused: {body}"
        );
        assert_eq!(
            db.epoch().await?,
            1,
            "a daemon that has stopped installed an epoch"
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn a_lease_lock_wait_that_ends_supervision_publishes_the_lease_as_lost() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let mut lease = db.lease("lock-wait-owner")?;
        ensure!(lease.try_acquire().await?, "fixture must hold lease");
        let application = lease.application_name().to_owned();
        let observer = lease.subscribe();
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(IGNORE_TERM)?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = TERM_GRACE;
        config.heartbeat_interval = Duration::from_millis(100);
        config.lease_lock_limit = Duration::from_millis(500);
        let mut supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        let (_stop, rx) = watch::channel(false);
        let task = tokio::spawn(async move { supervisor.run(rx).await });
        let _abort = AbortOnDrop(task.abort_handle());
        wait_for_file(&home.path().join("ready")).await?;
        let held = lease.lock_as("test lease holder").await;
        // The heartbeat's lock wait fails at its bound, and supervision ends in the drain.
        wait_for_file(&home.path().join("term")).await?;

        assert_eq!(
            *observer.borrow(),
            LeaseState::Lost,
            "a lock wait that ended supervision left the lease watch reading Active"
        );
        assert!(
            held.fence().is_none(),
            "a lease published lost still offers its fence"
        );
        let probe = db.lease("lock-wait-probe")?;
        assert_eq!(
            probe.owner().await?.map(|owner| owner.application_name),
            Some(application),
            "the failed lock wait touched the lease session"
        );
        drop(held);
        assert_eq!(
            timeout(Duration::from_secs(10), task).await???,
            SupervisorExit::LeaseLost
        );
        let mut peer = db.lease("lock-wait-peer")?;
        assert!(
            peer.try_acquire().await?,
            "the lease was not released after supervision ended"
        );
        peer.release().await
    })
    .await
}

#[tokio::test]
async fn a_restart_of_the_terminals_hands_the_lease_to_the_next_lifetime_at_the_same_epoch()
-> Result<()> {
    database_case(async |db| {
        let at_deactivate: Arc<Mutex<Option<LeaseState>>> = Arc::default();
        let hook_seen = Arc::clone(&at_deactivate);
        let runtime = start_runtime(
            db,
            "restart-owner",
            RESTARTS_TERMINALS_ONCE,
            |_| {},
            move |lease| {
                Box::leak(Box::new(PausesHooks {
                    at_deactivate: hook_seen,
                    ..PausesHooks::new(lease)
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        let changes = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let mut watched = runtime.observer.clone();
        let counted = Arc::clone(&changes);
        let counter = tokio::spawn(async move {
            if watched
                .wait_for(|state| *state == LeaseState::Active)
                .await
                .is_err()
            {
                return;
            }
            while watched.changed().await.is_ok() {
                counted.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            }
        });
        let _counter = AbortOnDrop(counter.abort_handle());
        // Only the second start writes `ready`.
        wait_for_file(&runtime.home.path().join("ready")).await?;

        // Every assertion but the last is made before the stop, whose release publishes.
        assert_eq!(
            fs::read_to_string(runtime.home.path().join("starts"))?,
            "aa"
        );
        assert_eq!(
            *at_deactivate
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner),
            Some(LeaseState::Active),
            "the first lifetime's teardown did not run under the kept lease"
        );
        assert_eq!(
            changes.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "the lease watch changed between the two lifetimes"
        );
        assert_eq!(*runtime.observer.borrow(), LeaseState::Active);
        assert_eq!(db.epoch().await?, 1, "the restart rotated the epoch");
        assert_eq!(
            runtime
                .lease
                .lock_as("test")
                .await
                .fence()
                .map(|fence| fence.epoch),
            Some(1),
            "the next lifetime does not hold the first epoch"
        );
        let (status, body) =
            lease_admin(&runtime.state, Method::GET, "/api/admin/lease/status").await?;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(body["mode"], "active", "{body}");
        runtime.stop.send(true)?;
        timeout(Duration::from_secs(30), runtime.task).await???;
        Ok(())
    })
    .await
}

/// The lease session's last statement and when its state last changed (seconds since the
/// epoch). The fixture role owns both sessions, so `query` is readable.
async fn lease_session_activity(db: &TestDb, application: &str) -> Result<(String, f64)> {
    let rows = db
        .admin
        .query(
            "SELECT query, EXTRACT(EPOCH FROM state_change)::double precision \
             FROM pg_stat_activity WHERE application_name = $1",
            &[&application],
        )
        .await?;
    ensure!(rows.len() == 1, "the lease session is not listed once");
    Ok((rows[0].try_get(0)?, rows[0].try_get(1)?))
}

/// Lease-scoped work whose admission ends the lifetime: `admit` records that it ran and fails.
struct StopsAtAdmission {
    admitted: bool,
}

impl LeaseScoped for StopsAtAdmission {
    fn admit(&mut self) -> Result<()> {
        self.admitted = true;
        Err(anyhow::anyhow!("stopped at admission"))
    }

    fn end(&mut self, _: Option<SupervisorExit>) -> impl Future<Output = ()> + Send {
        std::future::ready(())
    }
}

/// Waits for the paused activation, ends the lease session under it, waits until the lease
/// sees its socket closed, and returns a peer that holds the freed lease. The peer keeps the
/// run loop in standby afterwards, so no second activation pauses.
async fn end_the_session_during_activation(
    db: &TestDb,
    runtime: &Runtime,
    activating: &Notify,
) -> Result<ActiveDaemonLease> {
    timeout(Duration::from_secs(30), activating.notified())
        .await
        .context("the family was never activated")?;
    terminate_lease_session(db, &runtime.application).await?;
    timeout(Duration::from_secs(10), async {
        while runtime.lease.lock_as("test").await.fence().is_some() {
            sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .context("the lease never saw its session closed")?;
    take_freed_lease(db, "activation-peer").await
}

#[tokio::test]
async fn a_lease_session_that_ends_during_activation_leaves_no_active_watch_at_the_stop()
-> Result<()> {
    database_case(async |db| {
        let (activating, proceed) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        let deactivating = Arc::new(Notify::new());
        let at_deactivate: Arc<Mutex<Option<LeaseState>>> = Arc::default();
        let (hook_activating, hook_proceed) = (Arc::clone(&activating), Arc::clone(&proceed));
        let (hook_deactivating, hook_seen) =
            (Arc::clone(&deactivating), Arc::clone(&at_deactivate));
        let runtime = start_runtime(
            db,
            "activation-owner",
            READY_UNTIL_EOF,
            |_| {},
            move |lease| {
                Box::leak(Box::new(PausesHooks {
                    activating: hook_activating,
                    proceed: Some(hook_proceed),
                    at_deactivate: hook_seen,
                    deactivating: hook_deactivating,
                    ..PausesHooks::new(lease)
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        let mut peer = end_the_session_during_activation(db, &runtime, &activating).await?;
        proceed.notify_one();
        timeout(Duration::from_secs(30), deactivating.notified())
            .await
            .context("the families were never deactivated")?;

        let seen = *at_deactivate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert!(
            matches!(seen, Some(state) if state != LeaseState::Active),
            "the families were deactivated under an Active watch on a dead lease: {seen:?}"
        );
        runtime.stop.send(true)?;
        timeout(Duration::from_secs(30), runtime.task).await???;
        peer.release().await
    })
    .await
}

#[tokio::test]
async fn no_family_job_is_admitted_on_a_lease_that_ended_during_activation() -> Result<()> {
    database_case(async |db| {
        let (activating, proceed) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        let (running, deactivating) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        let at_deactivate: Arc<Mutex<Option<LeaseState>>> = Arc::default();
        let (hook_activating, hook_proceed) = (Arc::clone(&activating), Arc::clone(&proceed));
        let (hook_running, hook_deactivating) = (Arc::clone(&running), Arc::clone(&deactivating));
        let hook_seen = Arc::clone(&at_deactivate);
        let runtime = start_runtime(
            db,
            "admission-owner",
            READY_UNTIL_EOF,
            |_| {},
            move |lease| {
                Box::leak(Box::new(PausesHooks {
                    activating: hook_activating,
                    proceed: Some(hook_proceed),
                    running: hook_running,
                    at_deactivate: hook_seen,
                    deactivating: hook_deactivating,
                    ..PausesHooks::new(lease)
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        let mut peer = end_the_session_during_activation(db, &runtime, &activating).await?;
        let held = runtime.lease.lock_as("test lease holder").await;
        proceed.notify_one();
        let ran = timeout(
            gobby_daemon::heartbeat::BASE_CADENCE + Duration::from_secs(3),
            running.notified(),
        )
        .await
        .is_ok();

        assert!(
            !ran,
            "a family job ran on a lease whose session ended during activation"
        );
        drop(held);
        timeout(Duration::from_secs(30), deactivating.notified())
            .await
            .context("the families were never deactivated")?;
        let seen = *at_deactivate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert_ne!(
            seen,
            Some(LeaseState::Active),
            "the families were deactivated under an Active watch on a dead lease"
        );
        runtime.stop.send(true)?;
        timeout(Duration::from_secs(30), runtime.task).await???;
        peer.release().await
    })
    .await
}

#[tokio::test]
async fn a_lease_lock_error_at_the_lifetime_start_leaves_no_active_watch_at_the_stop() -> Result<()>
{
    database_case(async |db| {
        let (activating, proceed) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        let (running, deactivating) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        let at_deactivate: Arc<Mutex<Option<LeaseState>>> = Arc::default();
        let (hook_activating, hook_proceed) = (Arc::clone(&activating), Arc::clone(&proceed));
        let (hook_running, hook_deactivating) = (Arc::clone(&running), Arc::clone(&deactivating));
        let hook_seen = Arc::clone(&at_deactivate);
        let runtime = start_runtime(
            db,
            "entry-lock-owner",
            READY_UNTIL_EOF,
            |config| config.lease_lock_limit = Duration::from_millis(500),
            move |lease| {
                Box::leak(Box::new(PausesHooks {
                    activating: hook_activating,
                    proceed: Some(hook_proceed),
                    running: hook_running,
                    at_deactivate: hook_seen,
                    deactivating: hook_deactivating,
                    ..PausesHooks::new(lease)
                }))
            },
        )
        .await?;
        let _abort = AbortOnDrop(runtime.task.abort_handle());
        timeout(Duration::from_secs(30), activating.notified())
            .await
            .context("the family was never activated")?;
        let held = runtime.lease.lock_as("test lease holder").await;
        proceed.notify_one();
        timeout(Duration::from_secs(30), deactivating.notified())
            .await
            .context("the families were never deactivated")?;
        drop(held);
        let joined = timeout(Duration::from_secs(30), runtime.task).await??;

        let seen = *at_deactivate
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        assert_eq!(
            seen,
            Some(LeaseState::Lost),
            "a lease lock error at the lifetime start left the watch Active through the stop"
        );
        assert!(
            running.notified().now_or_never().is_none(),
            "a family job ran in a lifetime whose start failed"
        );
        assert!(
            joined.is_err(),
            "the runtime did not fail on the lease lock error: {joined:?}"
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn the_supervised_lifetime_admits_only_after_a_heartbeat_on_the_lease_session() -> Result<()>
{
    database_case(async |db| {
        let home = TempDir::new()?;
        let mut lease = db.lease("admission-heartbeat-owner")?;
        ensure!(lease.try_acquire().await?, "fixture must hold lease");
        let application = lease.application_name().to_owned();
        let (before, changed_before) = lease_session_activity(db, &application).await?;
        ensure!(
            before != "SELECT 1",
            "the acquisition's last statement was already a heartbeat"
        );
        let lease = Arc::new(NamedLock::new(lease));
        let claim =
            claim_pid_file(&home.path().join("gobby.pid"), Role::Daemon)?.context("PID claim")?;
        let argv = backend_argv(
            "import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'starts').write_text('a')",
        )?;
        let mut config = backend_config(home.path(), &argv)?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.heartbeat_interval = Duration::from_millis(100);
        let mut supervisor = BackendSupervisor::with_lease(Some(Arc::clone(&lease)), claim, config);
        let (_stop, rx) = watch::channel(false);
        let mut scoped = StopsAtAdmission { admitted: false };
        let result = timeout(
            Duration::from_secs(10),
            supervisor.run_ending(rx, &mut scoped),
        )
        .await
        .context("the lifetime did not end at its admission")?;
        let (after, changed_after) = lease_session_activity(db, &application).await?;

        match &result {
            Err(error) => assert!(
                format!("{error:#}").contains("stopped at admission"),
                "the lifetime failed for another reason: {error:#}"
            ),
            Ok(exit) => panic!("the lifetime did not end at its admission: {exit:?}"),
        }
        assert!(scoped.admitted, "the lifetime never admitted its work");
        assert_eq!(
            after, "SELECT 1",
            "the lifetime admitted its work with no heartbeat on the lease session"
        );
        assert!(
            changed_after > changed_before,
            "the lease session did not run a statement before the admission"
        );
        assert!(
            !home.path().join("starts").exists(),
            "a worker was spawned although the admission failed"
        );
        let mut held = lease.lock_as("test").await;
        assert_eq!(
            held.fence().map(|fence| fence.epoch),
            Some(1),
            "the failed admission released or discarded the lease session"
        );
        held.release().await
    })
    .await
}
