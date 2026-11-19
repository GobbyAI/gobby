//! Loopback-only mode: the proof and its control.
//! Four tests need an unsandboxed shell and are `#[ignore]`d:
//! The ignored tests fail, never skip, when they cannot prove. The rest run in
//! every shell. Probe classes are read from `tests/fixtures/loopback_probe.py`.

use std::fmt::Debug;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener};
use std::path::{Path, PathBuf};

use gobby_test_support::full_daemon::{FullDaemon, checkout_python};
use gobby_test_support::http_sender::post_json;
use gobby_test_support::loopback::{
    self, EXTERNAL_CONNECT_FAILED_TEXT, LoopbackError, NamespaceTool, Plan, Platform, Tools,
};
use serde_json::{Value, json};

use super::full_daemon::python_seed;

const GDAEMON: &str = env!("CARGO_BIN_EXE_gdaemon");
const REPO_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../..");
const SERVER_NAME: &str = "offhost";
const SAMPLE_TOOL: &str = "ping";

/// The Seatbelt profile the mode runs under, one rule per line.
const EXPECTED_PROFILE: &str = "(version 1)\n(allow default)\n(deny network-outbound)\n(allow network-outbound (remote ip \"localhost:*\"))\n(allow network-outbound (remote unix-socket (path-regex #\"^/\")))\n";

fn must<T, E: Debug>(result: Result<T, E>, what: &str) -> T {
    match result {
        Ok(value) => value,
        Err(error) => panic!("{what}: {error:?}"),
    }
}

fn probe_script() -> PathBuf {
    Path::new(REPO_ROOT).join("tests/fixtures/loopback_probe.py")
}

/// The class a blocked connect reports on this platform: `eperm` on macOS (the Seatbelt verdict).
/// A Linux namespace with no route fails the connect with ENETUNREACH, which the probe files as
/// `other` (the Linux blocked class is unproven until the post-port smoke test).
fn blocked_class() -> &'static str {
    if cfg!(target_os = "macos") {
        "eperm"
    } else {
        "other"
    }
}

/// A loopback listener that stays open for the test.
fn loopback_listener() -> (TcpListener, SocketAddr) {
    let listener = must(TcpListener::bind("127.0.0.1:0"), "bind a loopback listener");
    let addr = must(listener.local_addr(), "read the listener address");
    (listener, addr)
}

/// A loopback port with nothing listening: bind, read the port, and drop the listener.
fn closed_loopback_port() -> SocketAddr {
    let (listener, addr) = loopback_listener();
    drop(listener);
    addr
}

/// Runs the probe once and returns its class, failing the test on any error.
fn probe_class(target: SocketAddr, wrapped: bool) -> String {
    let scratch = must(tempfile::tempdir(), "scratch dir");
    let outcome = loopback::run_probe(
        &checkout_python(Path::new(REPO_ROOT)),
        &probe_script(),
        target,
        wrapped,
        scratch.path(),
    );
    must(outcome, "the probe runs").class
}

/// What the daemon's registration answer holds when its own connection got past TCP with the mode
/// off: the daemon made the HTTP attempt and the destination answered. A measured run got
/// `HTTP connection failed: Not Found` against the default gateway; the words after the prefix
/// depend on the destination, so only the prefix is required, with the connect-failure text absent.
const POST_CONNECT_TEXT: &str = "HTTP connection failed: ";

/// The daemon's answers to the off-host registration and to one tool call on the server.
struct Attempt {
    add_status: u16,
    add: Value,
    combined: String,
}

/// Registers the off-host destination as an external HTTP MCP server through the daemon's
/// existing route, then calls one tool on it. The connect fails inside the daemon, and the
/// combined response text carries the failure class. The route is used because the daemon takes
/// its external servers from the API, not from a config key (see the deviation in the report).
fn daemon_connect_failure(daemon: &FullDaemon, target: SocketAddrV4) -> Attempt {
    let base = daemon.front_door();
    let key = daemon.operator_key();
    let server = json!({
        "name": SERVER_NAME,
        "transport": "http",
        "url": format!("http://{target}/mcp"),
        "connect_timeout": 5,
    });
    let (added_status, added) = must(
        post_json(&format!("{base}/api/mcp/servers"), Some(key), &server),
        "the server add answers",
    );
    let (call_status, call) = must(
        post_json(
            &format!("{base}/api/mcp/{SERVER_NAME}/tools/{SAMPLE_TOOL}"),
            Some(key),
            &json!({}),
        ),
        "the tool call answers",
    );
    let combined: Value = json!({"add": {"status": added_status, "body": &added}, "call": {"status": call_status, "body": call}});
    Attempt {
        add_status: added_status,
        add: added,
        combined: combined.to_string(),
    }
}

// Non-ignored: the profile, argv, planner and target rules, and the probe.

#[test]
fn profile_is_the_design_text() {
    assert_eq!(
        loopback::macos_profile(),
        EXPECTED_PROFILE,
        "the profile is not the design text"
    );
}

#[test]
fn sandbox_exec_argv_wraps_gdaemon_with_the_profile() {
    let argv = loopback::sandbox_exec_argv(
        EXPECTED_PROFILE,
        Path::new("/x/gdaemon"),
        &["serve".to_owned()],
    );
    let expected = [
        loopback::SANDBOX_EXEC,
        "-p",
        EXPECTED_PROFILE,
        "/x/gdaemon",
        "serve",
    ];
    assert_eq!(argv, expected, "the argv is not the design argv");
}

#[test]
fn off_host_target_must_be_a_routable_ipv4_literal() {
    let gateway = Ipv4Addr::new(192, 168, 0, 1);
    let local = [Ipv4Addr::new(192, 168, 0, 5)];
    assert!(
        loopback::validate_offhost(SocketAddrV4::new(gateway, 80), &local).is_ok(),
        "a gateway is an off-host destination"
    );
    assert!(
        loopback::validate_offhost(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 80), &local).is_err(),
        "loopback is not off-host"
    );
    assert!(
        loopback::validate_offhost(SocketAddrV4::new(Ipv4Addr::new(127, 0, 0, 2), 80), &local)
            .is_err(),
        "127/8 is not off-host"
    );
    assert!(
        loopback::validate_offhost(SocketAddrV4::new(Ipv4Addr::new(192, 168, 0, 5), 80), &local)
            .is_err(),
        "a local interface address is not off-host"
    );
}

#[test]
fn off_host_selection_takes_the_override_then_the_gateway() {
    let gateway = Ipv4Addr::new(192, 168, 0, 1);
    let local = [Ipv4Addr::new(192, 168, 0, 5)];
    assert_eq!(
        loopback::select_offhost(Some("192.168.0.1:443"), Some(gateway), &local).ok(),
        Some(SocketAddrV4::new(gateway, 443)),
        "the override wins"
    );
    assert_eq!(
        loopback::select_offhost(None, Some(gateway), &local).ok(),
        Some(SocketAddrV4::new(gateway, loopback::OFFHOST_DEFAULT_PORT)),
        "the default gateway at the default port"
    );
    assert!(
        loopback::select_offhost(Some("example.test:80"), Some(gateway), &local).is_err(),
        "a hostname is not an IPv4 literal"
    );
    assert!(
        loopback::select_offhost(None, None, &local).is_err(),
        "no gateway and no override is an error"
    );
}

#[test]
fn gateway_parsers_read_the_macos_and_linux_route_tools() {
    let macos = "   route to: default\ndestination: default\n       mask: default\n    gateway: 192.168.0.1\n  interface: en0\n";
    assert_eq!(
        loopback::parse_route_gateway(macos),
        Some(Ipv4Addr::new(192, 168, 0, 1)),
        "macOS route output parses"
    );
    let linux = "default via 10.0.2.2 dev eth0 proto dhcp metric 100\n";
    assert_eq!(
        loopback::parse_ip_route_gateway(linux),
        Some(Ipv4Addr::new(10, 0, 2, 2)),
        "Linux ip route output parses"
    );
    assert_eq!(loopback::parse_route_gateway(""), None);
}

#[test]
fn planner_fails_closed_without_a_mechanism() {
    let all = Tools {
        sandbox_exec: true,
        unshare: true,
        bwrap: true,
        socat: true,
    };
    assert_eq!(
        loopback::plan(Platform::Other, all),
        Err(LoopbackError::Mechanism(loopback::ERR_NO_MECHANISM)),
        "Windows refuses with the clear error (H46)"
    );
    assert_eq!(
        loopback::plan(Platform::MacOs, Tools::default()),
        Err(LoopbackError::Mechanism(loopback::ERR_NO_SANDBOX_EXEC))
    );
    assert_eq!(
        loopback::plan(Platform::MacOs, all),
        Ok(Plan::SandboxExec),
        "macOS plans Seatbelt"
    );
    assert_eq!(
        loopback::plan(Platform::Linux, Tools::default()),
        Err(LoopbackError::Mechanism(loopback::ERR_NO_NAMESPACE_TOOL))
    );
    let without_socat = Tools {
        unshare: true,
        ..Tools::default()
    };
    assert_eq!(
        loopback::plan(Platform::Linux, without_socat),
        Err(LoopbackError::Mechanism(loopback::ERR_NO_RELAY_TOOL))
    );
    let with_unshare = Tools {
        unshare: true,
        socat: true,
        ..Tools::default()
    };
    assert_eq!(
        loopback::plan(Platform::Linux, with_unshare),
        Ok(Plan::Namespace(NamespaceTool::Unshare))
    );
}

#[test]
fn probe_connects_to_a_loopback_listener_unwrapped() {
    let (_listener, addr) = loopback_listener();
    assert_eq!(
        probe_class(addr, false),
        "connected",
        "the unwrapped probe reaches a loopback listener"
    );
}

#[test]
fn probe_is_refused_on_a_closed_loopback_port() {
    assert_eq!(
        probe_class(closed_loopback_port(), false),
        "refused",
        "a closed loopback port refuses"
    );
}

// Non-ignored, under the mode: the allowed loopback connect and the diagnostic TEST-NET probe.
// Both run the probe under the wrapper, so they need nested sandbox-exec on macOS.

#[test]
fn loopback_connect_allowed_mode_on() {
    let (_listener, addr) = loopback_listener();
    assert_eq!(
        probe_class(addr, true),
        "connected",
        "the mode admits a loopback connect"
    );
}

#[test]
fn testnet_mode_on_eperm() {
    // TEST-NET (192.0.2.0/24) is never routed, so the blocked class needs no control.
    let target = SocketAddr::from(([192, 0, 2, 1], 9));
    assert_eq!(
        probe_class(target, true),
        blocked_class(),
        "the mode blocks TEST-NET (diagnostic)"
    );
}

// Step 6 (ignored): the proof and its control, from an unsandboxed shell only.

#[test]
#[ignore = "protocol step 6 proof: run from an unsandboxed shell with cargo nextest run -p gobby-daemon --test suite --run-ignored only -E 'binary(suite) & test(/^loopback_network::/)'; a default run prints these as skipped with this reason"]
fn off_host_probe_fails_mode_on() {
    let target = must(loopback::offhost_target(), "an off-host destination");
    assert_eq!(
        probe_class(SocketAddr::V4(target), true),
        blocked_class(),
        "the mode blocks an off-host connect (O2)"
    );
}

#[test]
#[ignore = "protocol step 6 proof: run from an unsandboxed shell with cargo nextest run -p gobby-daemon --test suite --run-ignored only -E 'binary(suite) & test(/^loopback_network::/)'; a default run prints these as skipped with this reason"]
fn sandbox_probe_matches_profile() {
    let target = must(loopback::offhost_target(), "an off-host destination");
    assert_eq!(
        probe_class(SocketAddr::V4(target), true),
        blocked_class(),
        "the profile blocks the off-host target"
    );
    let (_listener, addr) = loopback_listener();
    assert_eq!(
        probe_class(addr, true),
        "connected",
        "the profile admits the loopback listener"
    );
}

#[test]
#[ignore = "protocol step 6 proof: run from an unsandboxed shell with cargo nextest run -p gobby-daemon --test suite --run-ignored only -E 'binary(suite) & test(/^loopback_network::/)'; a default run prints these as skipped with this reason"]
fn control_connects_mode_off() {
    let target = must(loopback::offhost_target(), "an off-host destination");
    // The control proves the destination answers TCP with the mode off, in this run.
    let precondition = probe_class(SocketAddr::V4(target), false);
    assert_eq!(
        precondition, "connected",
        "no usable off-host destination; measured behaviour reported"
    );
    let daemon = must(
        FullDaemon::builder(Path::new(GDAEMON), Path::new(REPO_ROOT))
            .seed(python_seed)
            .start(),
        "the control daemon starts with the mode off",
    );
    let attempt = daemon_connect_failure(&daemon, target);
    daemon.teardown().expect("the control daemon tears down");
    // The daemon's own connection must get past TCP: the registration answers without the
    // connect-failure text and with the post-connect answer. The tool call is not judged: after a
    // failed registration it reports only an open circuit breaker, the same under both modes.
    assert!(
        (200..300).contains(&attempt.add_status),
        "the off-host server must register with the mode off: {}",
        attempt.combined
    );
    let add = attempt.add.to_string();
    assert!(
        !add.contains(EXTERNAL_CONNECT_FAILED_TEXT),
        "the mode-off connect failed before TCP completed, which is not a control result: {}",
        attempt.combined
    );
    assert!(
        add.contains(POST_CONNECT_TEXT),
        "the mode-off registration did not get past TCP as expected: {}",
        attempt.combined
    );
}

#[test]
#[ignore = "protocol step 6 proof: run from an unsandboxed shell with cargo nextest run -p gobby-daemon --test suite --run-ignored only -E 'binary(suite) & test(/^loopback_network::/)'; a default run prints these as skipped with this reason"]
fn daemon_outbound_fails_mode_on() {
    let target = must(loopback::offhost_target(), "an off-host destination");
    // The destination must answer TCP with the mode off, and the wrapped probe must be blocked.
    // Without both, a refused or unreachable destination would give the same connect-failure text.
    assert_eq!(
        probe_class(SocketAddr::V4(target), false),
        "connected",
        "no usable off-host destination; measured behaviour reported"
    );
    assert_eq!(
        probe_class(SocketAddr::V4(target), true),
        blocked_class(),
        "the mode must block the off-host connect itself, not a refusal"
    );
    let daemon = must(
        FullDaemon::builder(Path::new(GDAEMON), Path::new(REPO_ROOT))
            .seed(python_seed)
            .loopback_only()
            .start(),
        "the daemon starts under the loopback mode",
    );
    let attempt = daemon_connect_failure(&daemon, target);
    daemon.teardown().expect("the daemon tears down");
    assert!(
        attempt
            .add
            .to_string()
            .contains(EXTERNAL_CONNECT_FAILED_TEXT),
        "the daemon's external connect was not blocked: {}",
        attempt.combined
    );
}
