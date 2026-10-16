#![cfg(unix)]

use anyhow::{Context, Result, ensure};
use futures_util::FutureExt;
use gobby_daemon::lifecycle::shutdown_intent::{Intent, IntentMarker};
use gobby_daemon::{
    lease::{ActiveDaemonLease, LeaseMode},
    lifecycle::{
        backend::{BackendConfig, BackendStatus, BackendSupervisor, SupervisorExit, crash_delay},
        pid_file::{Role, claim_pid_file},
    },
};
use gobby_terminals::write_api::WriteRefused;
use serde_json::json;
use std::fs;
use std::{
    path::Path,
    time::{Duration, Instant},
};
use tempfile::TempDir;
use tokio::{
    sync::watch,
    time::{sleep, timeout},
};
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

struct TestDb {
    admin: Client,
    url: String,
    schema: String,
}

impl TestDb {
    fn lease(&self, machine: &str) -> Result<ActiveDaemonLease> {
        ActiveDaemonLease::new(&self.url, machine, "lifecycle-test", LeaseMode::Hub)
    }

    async fn supervisor(&self, home: &Path, script: &str) -> Result<BackendSupervisor> {
        let mut lease = self.lease("supervisor")?;
        ensure!(lease.try_acquire().await?, "fixture must hold lease");
        let claim = claim_pid_file(&home.join("gobby.pid"), Role::Daemon)?
            .ok_or_else(|| anyhow::anyhow!("fixture PID claim unavailable"))?;
        let argv = serde_json::to_string(&["uv", "run", "python", "-c", script])?;
        let mut config = BackendConfig::from_command_json(home, Some(&argv))?;
        config
            .environment
            .insert("HOME".into(), home.as_os_str().to_owned());
        config.drain_timeout = Duration::from_millis(100);
        config.heartbeat_interval = Duration::from_millis(100);
        Ok(BackendSupervisor::new(lease, claim, config))
    }
}

async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL lifecycle test");
        return Ok(());
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "lifecycle tests require protected isolated test hub"
    );
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    timeout(
        Duration::from_secs(60),
        admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-lease-tests'), 1)"),
    )
    .await??;
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

async fn wait_for_file(path: &Path) -> Result<String> {
    timeout(Duration::from_secs(10), async {
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
    .await?
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
        assert_eq!(supervisor.run(rx.clone()).await?, SupervisorExit::Refused);
        assert!(
            standby.try_acquire().await?,
            "refusal releases lease to healthy standby"
        );
        assert_eq!(standby.fence().map(|f| f.epoch), Some(2));
        let healthy_home=TempDir::new()?;
        let claim=claim_pid_file(&healthy_home.path().join("gobby.pid"),Role::Daemon)?.context("standby PID claim")?;
        let script="import os,pathlib; (pathlib.Path(os.environ['GOBBY_HOME'])/'ready').write_text('healthy'); open(int(os.environ['GOBBY_SUPERVISOR_FD']),'rb').read()";
        let argv=serde_json::to_string(&["uv","run","python","-c",script])?;
        let mut config=BackendConfig::from_command_json(healthy_home.path(),Some(&argv))?;
        config.drain_timeout=Duration::from_millis(100);
        let mut healthy=BackendSupervisor::new(standby,claim,config);
        let (stop,stop_rx)=watch::channel(false);
        let task=tokio::spawn(async move {healthy.run(stop_rx).await});
        let _abort=AbortOnDrop(task.abort_handle());
        assert_eq!(wait_for_file(&healthy_home.path().join("ready")).await?,"healthy");
        stop.send(true)?;
        assert_eq!(timeout(Duration::from_secs(5),task).await???,SupervisorExit::Stopped);
        assert_eq!(supervisor.run(rx).await?, SupervisorExit::Refused);
        let mut standby=db.lease("next-standby")?;
        assert!(standby.try_acquire().await?, "refused is never a contender");
        Ok(())
    })
    .await
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
        let argv = serde_json::to_string(&["uv", "run", "python", "-c", script])?;
        let mut config = BackendConfig::from_command_json(home.path(), Some(&argv))?;
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
    let argv = serde_json::to_string(&["uv", "run", "python", "-c", script])?;
    let mut config = BackendConfig::from_command_json(home.path(), Some(&argv))?;
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
        let argv = serde_json::to_string(&["uv", "run", "python", "-c", &script])?;
        let mut config = BackendConfig::from_command_json(home.path(), Some(&argv))?;
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
            "python",
            "-c",
            "raise RuntimeError('standby spawned')",
        ])?;
        let config = BackendConfig::from_command_json(home.path(), Some(&command))?;
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

const IGNORE_TERM: &str = r#"import os,pathlib,signal,time
home=pathlib.Path(os.environ['GOBBY_HOME'])
def term(*args): (home/'term').write_text('TERM')
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
            timeout(Duration::from_secs(5), task).await???,
            SupervisorExit::Stopped
        );
        assert!(started.elapsed() >= Duration::from_millis(90));
        assert!(started.elapsed() < Duration::from_secs(5));
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
        let argv = serde_json::to_string(&["uv", "run", "python", "-c", IGNORE_TERM])?;
        let mut config = BackendConfig::from_command_json(home.path(), Some(&argv))?;
        config
            .environment
            .insert("HOME".into(), home.path().as_os_str().to_owned());
        config.drain_timeout = Duration::from_millis(100);
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
            timeout(Duration::from_secs(5), task).await???,
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

const EOF_BACKEND: &str = r#"import os,pathlib,json
home=pathlib.Path(os.environ['GOBBY_HOME']); fd=int(os.environ['GOBBY_SUPERVISOR_FD'])
opened=[]
for candidate in range(3,512):
    try: os.fstat(candidate); opened.append(candidate)
    except OSError: pass
(home/'descriptors').write_text(json.dumps({'pid':os.getpid(),'open':opened,'liveness':fd}))
assert os.read(fd,4096).endswith(b'\n')
assert os.read(fd,1)==b''
(home/'eof').write_text('EOF')
"#;

#[tokio::test]
async fn supervisor_death_gives_backend_eof() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let command = serde_json::to_string(&["uv", "run", "python", "-c", EOF_BACKEND])?;
        let executable = std::env::current_exe()?;
        let peer_path = executable
            .parent()
            .context("test target directory")?
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
            "only liveness read FD survives exec"
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
        assert_eq!(timeout(Duration::from_secs(10),supervisor.run(rx)).await??,SupervisorExit::Stopped);
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
