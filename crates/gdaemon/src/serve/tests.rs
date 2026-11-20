//! `gdaemon serve` tests: bootstrap loading, bind addresses, the accept loop and the fd limit.

use super::*;

#[test]
fn disabled_front_door_refuses_with_typed_message() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("bootstrap.yaml");
    std::fs::write(&path, "front_door:\n  enabled: false\n").expect("write bootstrap");

    let error = load_enabled_bootstrap(&path).expect_err("disabled front door must refuse");

    assert!(
        error.to_string().starts_with("front_door_disabled:"),
        "{error}"
    );
}

#[tokio::test]
async fn bind_host_family_matches_the_python_backend() -> Result<()> {
    let localhost = bind_addr("localhost", 60887).await?;
    let ipv6 = bind_addr("::1", 60887).await?;

    assert_eq!(localhost, SocketAddr::from(([127, 0, 0, 1], 60887)));
    assert!(ipv6.is_ipv6());
    Ok(())
}

#[test]
fn missing_bootstrap_serves_default_ports() {
    let dir = tempfile::tempdir().expect("tempdir");

    let bootstrap = load_enabled_bootstrap(&dir.path().join("bootstrap.yaml")).expect("defaults");

    assert_eq!(
        (bootstrap.daemon_port, bootstrap.websocket_port),
        (DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT)
    );
    assert!(bootstrap.front_door.enabled);
}

/// A listener whose first `emfiles` accepts fail with EMFILE.
#[cfg(unix)]
struct ExhaustedListener {
    listener: TcpListener,
    emfiles: std::sync::Mutex<usize>,
}

#[cfg(unix)]
impl AcceptSource for ExhaustedListener {
    fn accept(&self) -> impl Future<Output = std::io::Result<(TcpStream, SocketAddr)>> + Send {
        let exhausted = {
            let mut left = self.emfiles.lock().expect("fixture exhaustion counter");
            let exhausted = *left > 0;
            *left = left.saturating_sub(1);
            exhausted
        };
        async move {
            if exhausted {
                return Err(std::io::Error::from_raw_os_error(libc::EMFILE));
            }
            self.listener.accept().await
        }
    }
}

#[cfg(unix)]
#[tokio::test]
async fn accept_loop_serves_after_transient_emfile() -> Result<()> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let auth = Arc::new(AuthState::new(
        "test-secret".into(),
        None,
        Default::default(),
    )?);
    let state = FrontDoorState::new("127.0.0.1:1".parse()?, BackendState::Down, auth);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let exhausted = ExhaustedListener {
        listener,
        emfiles: 2.into(),
    };
    let mut accept_loop = tokio::spawn(accept(exhausted, FrontDoor::new(state, table), None));

    let round_trip = async {
        let mut client = TcpStream::connect(addr).await?;
        client
            .write_all(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            .await?;
        let mut response = String::new();
        client.read_to_string(&mut response).await?;
        anyhow::Ok(response)
    };
    let response = tokio::select! {
        biased;
        finished = &mut accept_loop => panic!("accept loop exited on EMFILE: {finished:?}"),
        response = tokio::time::timeout(Duration::from_secs(10), round_trip) => response??,
    };

    assert!(response.starts_with("HTTP/1.1 "), "{response}");
    accept_loop.abort();
    Ok(())
}

#[cfg(unix)]
#[test]
fn serve_startup_raises_the_launchd_fd_soft_limit() {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: `limit` is a valid, writable rlimit for getrlimit to fill.
    assert_eq!(
        unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
        0
    );
    assert!(
        limit.rlim_max > 256,
        "hard limit {} leaves no room",
        limit.rlim_max
    );
    limit.rlim_cur = 256;
    // SAFETY: `limit` is initialized and lowers only the soft value, which is
    // always permitted; nextest runs this test in its own process.
    assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) }, 0);

    let (before, after) = raise_nofile_limit(NOFILE_TARGET).expect("raise fd limit");

    // SAFETY: as above for getrlimit.
    assert_eq!(
        unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
        0
    );
    assert_eq!(before, 256);
    assert_eq!(after, NOFILE_TARGET.min(limit.rlim_max));
    assert_eq!(limit.rlim_cur, after);
    assert!(
        limit.rlim_cur > 256,
        "soft limit stayed at {}",
        limit.rlim_cur
    );
}
