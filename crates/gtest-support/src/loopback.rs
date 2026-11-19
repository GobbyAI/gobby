//! Loopback-only mode (design-v2 section 7.4; rulings H8, H9, H23, H30, H32, H46, H56).
//!
//! A harness-side wrapper that runs gdaemon with outbound network limited to loopback. macOS
//! wraps the process in a Seatbelt profile through `sandbox-exec`. Linux runs it in a network
//! namespace, with socat relays for the harness ports and the database port. Windows refuses:
//! plain std has no network isolation there, so the mode fails closed (H46). The mode is a
//! harness-side wrapper only; no production key reads it (H8, D37).

use std::fmt;
use std::fs;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant};

/// The macOS sandbox launcher the wrapper uses (section 7.4.2).
pub const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";

/// The off-host port the default gateway target uses (section 7.4.4).
pub const OFFHOST_DEFAULT_PORT: u16 = 80;

/// The environment variable that overrides the off-host target: `host:port` (section 7.4.4).
pub const OFFHOST_TARGET_ENV: &str = "GOBBY_HARNESS_OFFHOST_TARGET";

/// No mechanism on this platform (section 7.4.1). Windows fails closed with this text (H46).
pub const ERR_NO_MECHANISM: &str = "loopback-only mode has no mechanism on this platform";

/// macOS without `/usr/bin/sandbox-exec`.
pub const ERR_NO_SANDBOX_EXEC: &str =
    "loopback-only mode needs /usr/bin/sandbox-exec, which is missing";

/// Linux without `unshare -rn` or `bwrap --unshare-net`: refuse, never run unprotected (H23).
pub const ERR_NO_NAMESPACE_TOOL: &str = "loopback-only mode refuses to start: neither unshare -rn nor bwrap --unshare-net can create a network namespace on this host";

/// Linux without `socat`, so the relays that cross the namespace boundary cannot run (H23).
pub const ERR_NO_RELAY_TOOL: &str = "loopback-only mode refuses to start: socat is missing, so the harness and database ports cannot cross the namespace boundary";

/// No validated off-host destination (section 7.4.4; H9: never a pass by skipping).
pub const ERR_NO_OFFHOST: &str = "no off-host destination; measured behaviour reported";

/// The daemon's external connect failure text under mode on, one constant per class (H56). The
/// step-6 run replaces each placeholder with the text the Python transport surfaces. A
/// placeholder never matches a real response, so a test that still holds one fails.
pub const EXTERNAL_CONNECT_EPERM_TEXT: &str =
    "UNMEASURED: the step-6 run fills this from the daemon's EPERM connect error";
/// The daemon's connection-refused text (H56 placeholder, filled at step 6).
pub const EXTERNAL_CONNECT_REFUSED_TEXT: &str =
    "UNMEASURED: the step-6 run fills this from the daemon's refused connect error";
/// The daemon's connect-timeout text (H56 placeholder, filled at step 6).
pub const EXTERNAL_CONNECT_TIMEOUT_TEXT: &str =
    "UNMEASURED: the step-6 run fills this from the daemon's timed-out connect error";

/// How long a relay socket may take to appear before the namespace starts (Linux).
const SOCKET_WAIT: Duration = Duration::from_secs(5);

/// Why a loopback step could not be taken or proven.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoopbackError {
    /// The platform or a required mechanism is missing (fail closed).
    Mechanism(&'static str),
    /// No usable off-host destination, or an invalid target.
    Target(String),
    /// The probe was inconclusive or could not run.
    Probe(String),
    /// A process or file operation failed.
    Io(String),
}

impl fmt::Display for LoopbackError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Mechanism(detail) => write!(f, "{detail}"),
            Self::Target(detail) => write!(f, "target: {detail}"),
            Self::Probe(detail) => write!(f, "probe: {detail}"),
            Self::Io(detail) => write!(f, "io: {detail}"),
        }
    }
}

impl std::error::Error for LoopbackError {}

/// The platform families the planner distinguishes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    /// macOS: Seatbelt through `sandbox-exec`.
    MacOs,
    /// Linux: a network namespace with relays.
    Linux,
    /// Windows and anything else: no mechanism, so the mode refuses.
    Other,
}

/// The platform this build targets.
pub fn current_platform() -> Platform {
    if cfg!(target_os = "macos") {
        Platform::MacOs
    } else if cfg!(target_os = "linux") {
        Platform::Linux
    } else {
        Platform::Other
    }
}

/// Which launch tools the host has. Passed in so the planner runs the same on any host.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Tools {
    /// `/usr/bin/sandbox-exec` exists.
    pub sandbox_exec: bool,
    /// `unshare -rn` can create a user and network namespace.
    pub unshare: bool,
    /// `bwrap --unshare-net` can create a network namespace.
    pub bwrap: bool,
    /// `socat` is installed (the relays).
    pub socat: bool,
}

/// The mechanism a platform and its tools select.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Plan {
    /// Wrap the process in the macOS Seatbelt profile.
    SandboxExec,
    /// Wrap the process in a network namespace built by this tool.
    Namespace(NamespaceTool),
}

/// The namespace tool a Linux plan uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NamespaceTool {
    /// `unshare -rn`, which execs in place so the wrapper keeps the child's pid.
    Unshare,
    /// `bwrap --unshare-net`, which forks, so the child's pid is bwrap's (a found line).
    Bwrap,
}

/// Chooses the mechanism, or refuses with the clear error for it (fail closed, H23, H46).
pub fn plan(platform: Platform, tools: Tools) -> Result<Plan, LoopbackError> {
    match platform {
        Platform::MacOs if tools.sandbox_exec => Ok(Plan::SandboxExec),
        Platform::MacOs => Err(LoopbackError::Mechanism(ERR_NO_SANDBOX_EXEC)),
        Platform::Linux if !(tools.unshare || tools.bwrap) => {
            Err(LoopbackError::Mechanism(ERR_NO_NAMESPACE_TOOL))
        }
        Platform::Linux if !tools.socat => Err(LoopbackError::Mechanism(ERR_NO_RELAY_TOOL)),
        Platform::Linux if tools.unshare => Ok(Plan::Namespace(NamespaceTool::Unshare)),
        Platform::Linux => Ok(Plan::Namespace(NamespaceTool::Bwrap)),
        Platform::Other => Err(LoopbackError::Mechanism(ERR_NO_MECHANISM)),
    }
}

/// The exact Seatbelt profile text (section 7.4.2), one rule per line.
pub fn macos_profile() -> &'static str {
    "(version 1)\n(allow default)\n(deny network-outbound)\n(allow network-outbound (remote ip \"localhost:*\"))\n(allow network-outbound (remote unix-socket (path-regex #\"^/\")))\n"
}

/// The `sandbox-exec` argv that runs `program args` under `profile` (section 7.4.2).
pub fn sandbox_exec_argv(profile: &str, program: &Path, args: &[String]) -> Vec<String> {
    let mut argv = vec![
        SANDBOX_EXEC.to_owned(),
        "-p".to_owned(),
        profile.to_owned(),
        program.display().to_string(),
    ];
    argv.extend(args.iter().cloned());
    argv
}

/// Accepts an off-host destination: a routable IPv4 literal, not loopback, not unspecified, not
/// a local interface address, and a non-zero port (section 7.4.4).
pub fn validate_offhost(
    target: SocketAddrV4,
    local: &[Ipv4Addr],
) -> Result<SocketAddrV4, LoopbackError> {
    let ip = *target.ip();
    if ip.is_loopback() {
        return Err(LoopbackError::Target(format!(
            "{target} is loopback, not off-host"
        )));
    }
    if ip.is_unspecified() || ip.is_broadcast() {
        return Err(LoopbackError::Target(format!("{target} is not a host")));
    }
    if local.contains(&ip) {
        return Err(LoopbackError::Target(format!(
            "{target} is an address of this machine, not off-host"
        )));
    }
    if target.port() == 0 {
        return Err(LoopbackError::Target(format!("{target} has no port")));
    }
    Ok(target)
}

/// Chooses the off-host destination: the environment override first, then the default gateway at
/// [`OFFHOST_DEFAULT_PORT`]. Each choice is validated (section 7.4.4).
pub fn select_offhost(
    env_value: Option<&str>,
    gateway: Option<Ipv4Addr>,
    local: &[Ipv4Addr],
) -> Result<SocketAddrV4, LoopbackError> {
    if let Some(text) = env_value {
        let target = SocketAddrV4::from_str(text).map_err(|_| {
            LoopbackError::Target(format!(
                "{OFFHOST_TARGET_ENV} must be an IPv4 host:port, got {text:?}"
            ))
        })?;
        return validate_offhost(target, local);
    }
    let gateway = gateway.ok_or_else(|| LoopbackError::Target(ERR_NO_OFFHOST.to_owned()))?;
    validate_offhost(SocketAddrV4::new(gateway, OFFHOST_DEFAULT_PORT), local)
}

/// The gateway from macOS `route -n get default` output (its `gateway:` line).
pub fn parse_route_gateway(text: &str) -> Option<Ipv4Addr> {
    text.lines().find_map(|line| {
        let value = line.trim().strip_prefix("gateway:")?;
        Ipv4Addr::from_str(value.trim()).ok()
    })
}

/// The gateway from Linux `ip -4 route show default` output (the address after `via`).
pub fn parse_ip_route_gateway(text: &str) -> Option<Ipv4Addr> {
    text.lines().find_map(|line| {
        let mut fields = line.split_whitespace();
        if fields.next()? != "default" {
            return None;
        }
        let rest: Vec<&str> = fields.collect();
        let at = rest.iter().position(|field| *field == "via")?;
        Ipv4Addr::from_str(rest.get(at + 1)?).ok()
    })
}

/// The default gateway of this host, read with the platform's route tool.
pub fn default_gateway() -> Option<Ipv4Addr> {
    let output = if cfg!(target_os = "macos") {
        Command::new("/sbin/route")
            .args(["-n", "get", "default"])
            .output()
            .ok()?
    } else if cfg!(target_os = "linux") {
        Command::new("ip")
            .args(["-4", "route", "show", "default"])
            .output()
            .ok()?
    } else {
        return None;
    };
    let text = String::from_utf8_lossy(&output.stdout);
    if cfg!(target_os = "macos") {
        parse_route_gateway(&text)
    } else {
        parse_ip_route_gateway(&text)
    }
}

/// Every IPv4 address of this host's interfaces (`getifaddrs`).
#[cfg(unix)]
pub fn local_addresses() -> Result<Vec<Ipv4Addr>, LoopbackError> {
    let interfaces = nix::ifaddrs::getifaddrs()
        .map_err(|error| LoopbackError::Io(format!("list interfaces: {error}")))?;
    Ok(interfaces
        .filter_map(|interface| {
            interface
                .address
                .as_ref()
                .and_then(|address| address.as_sockaddr_in())
                .map(|sockaddr| sockaddr.ip())
        })
        .collect())
}

/// Windows has no interface listing in plain std, so the local-address rule cannot run and the
/// off-host target is refused (fail closed).
#[cfg(windows)]
pub fn local_addresses() -> Result<Vec<Ipv4Addr>, LoopbackError> {
    Err(LoopbackError::Mechanism(ERR_NO_MECHANISM))
}

/// The off-host destination for this run, from the environment or the default gateway.
pub fn offhost_target() -> Result<SocketAddrV4, LoopbackError> {
    let env_value = std::env::var(OFFHOST_TARGET_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty());
    let local = local_addresses()?;
    select_offhost(env_value.as_deref(), default_gateway(), &local)
}

/// Whether `name` is an executable file on the harness's own PATH.
fn on_path(name: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(name).is_file()))
}

/// Runs a namespace tool once with a trivial command to prove it works on this host.
fn namespace_works(tool: &str, args: &[&str]) -> bool {
    cfg!(target_os = "linux")
        && on_path(tool)
        && Command::new(tool)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
}

/// Which launch tools this host has, each proven to work where it is checked by running it.
pub fn tools_present() -> Tools {
    Tools {
        sandbox_exec: Path::new(SANDBOX_EXEC).is_file(),
        unshare: namespace_works("unshare", &["-rn", "true"]),
        bwrap: namespace_works("bwrap", &["--unshare-net", "--bind", "/", "/", "true"]),
        socat: on_path("socat"),
    }
}

/// The class a probe reported (`loopback_probe.py`), with its errno when there was one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeOutcome {
    /// `connected`, `eperm`, `refused`, `timeout` or `other`.
    pub class: String,
    /// The OS errno, when the connect failed with one.
    pub errno: Option<i64>,
}

static PROBE_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Runs `loopback_probe.py` once against `target`, either directly or under the loopback mode.
/// `scratch` holds the probe's output file.
pub fn run_probe(
    python: &Path,
    probe: &Path,
    target: SocketAddr,
    wrapped: bool,
    scratch: &Path,
) -> Result<ProbeOutcome, LoopbackError> {
    let out = scratch.join(format!(
        "probe-{}-{}.json",
        std::process::id(),
        PROBE_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let probe_args = [
        probe.display().to_string(),
        target.to_string(),
        out.display().to_string(),
    ];
    let status = if wrapped {
        // A loopback target is relayed into the namespace, so the probe can reach a listener
        // outside it. An off-host target gets no relay: the namespace has no route to it.
        let outbound: Vec<u16> = if target.ip().is_loopback() {
            vec![target.port()]
        } else {
            Vec::new()
        };
        let args: Vec<&str> = probe_args.iter().map(String::as_str).collect();
        let (mut command, _guard) = wrap_command(python, &args, &[], &outbound, scratch)?;
        command
            .stdin(Stdio::null())
            .status()
            .map_err(|error| LoopbackError::Io(format!("run the probe under the mode: {error}")))?
    } else {
        Command::new(python)
            .args(&probe_args)
            .stdin(Stdio::null())
            .status()
            .map_err(|error| LoopbackError::Io(format!("run the probe: {error}")))?
    };
    if !status.success() {
        return Err(LoopbackError::Probe(format!(
            "the probe exited with {status}"
        )));
    }
    let text = fs::read_to_string(&out)
        .map_err(|error| LoopbackError::Probe(format!("read the probe output: {error}")))?;
    let value: serde_json::Value = serde_json::from_str(&text)
        .map_err(|error| LoopbackError::Probe(format!("the probe output: {error}")))?;
    let class = value["class"]
        .as_str()
        .ok_or_else(|| LoopbackError::Probe("the probe output has no class".to_owned()))?;
    Ok(ProbeOutcome {
        class: class.to_owned(),
        errno: value["errno"].as_i64(),
    })
}

/// The relays and helper processes one wrapped run owns. Dropping it stops the relays.
#[derive(Debug, Default)]
pub struct LoopbackGuard {
    relays: Vec<Child>,
}

impl Drop for LoopbackGuard {
    fn drop(&mut self) {
        for relay in &mut self.relays {
            let _ = relay.kill();
            let _ = relay.wait();
        }
    }
}

/// A command that runs `program args` under the loopback mode. `inbound` ports are served by the
/// child and reached from outside (the harness ports); `outbound` ports are reached by the child
/// and served outside (the database port). The guard must outlive the child.
pub fn wrap_command(
    program: &Path,
    args: &[&str],
    inbound: &[u16],
    outbound: &[u16],
    scratch: &Path,
) -> Result<(Command, LoopbackGuard), LoopbackError> {
    match plan(current_platform(), tools_present())? {
        Plan::SandboxExec => {
            let owned: Vec<String> = args.iter().map(|arg| (*arg).to_owned()).collect();
            let mut argv = sandbox_exec_argv(macos_profile(), program, &owned);
            let first = argv.remove(0);
            let mut command = Command::new(first);
            command.args(argv);
            Ok((command, LoopbackGuard::default()))
        }
        Plan::Namespace(tool) => namespace_command(tool, program, args, inbound, outbound, scratch),
    }
}

/// Builds `unshare -rn` or `bwrap --unshare-net` around `program`, with socat relays. The
/// namespace script brings loopback up, starts its relays, then execs the program, so the
/// program keeps the pid it was spawned with under `unshare`.
fn namespace_command(
    tool: NamespaceTool,
    program: &Path,
    args: &[&str],
    inbound: &[u16],
    outbound: &[u16],
    scratch: &Path,
) -> Result<(Command, LoopbackGuard), LoopbackError> {
    let mut guard = LoopbackGuard::default();
    // The script runs inside the namespace. Each relay socket lives in `scratch`, which both
    // sides of the namespace see as the same filesystem.
    let mut script = String::from("ip link set lo up || exit 70; ");
    for &port in inbound {
        let socket = scratch.join(format!("in-{port}.sock"));
        // The namespace side creates this socket when its relay starts, so there is nothing to
        // wait for outside: the outside relay connects on demand, and the harness retries.
        guard.relays.push(spawn_relay(&[
            format!("TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork"),
            format!("UNIX-CONNECT:{}", socket.display()),
        ])?);
        script.push_str(&format!(
            "socat UNIX-LISTEN:'{}',fork TCP-CONNECT:127.0.0.1:{port} & ",
            socket.display()
        ));
    }
    for &port in outbound {
        let socket = scratch.join(format!("out-{port}.sock"));
        guard.relays.push(spawn_relay(&[
            format!("UNIX-LISTEN:{},fork", socket.display()),
            format!("TCP-CONNECT:127.0.0.1:{port}"),
        ])?);
        wait_for_socket_path(&socket)?;
        script.push_str(&format!(
            "socat TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:'{}' & ",
            socket.display()
        ));
    }
    script.push_str("exec \"$@\"");
    let mut command = match tool {
        NamespaceTool::Unshare => {
            let mut command = Command::new("unshare");
            command.args(["-rn", "sh", "-c", &script, "loopback-wrap"]);
            command
        }
        NamespaceTool::Bwrap => {
            let mut command = Command::new("bwrap");
            command.args([
                "--unshare-net",
                "--bind",
                "/",
                "/",
                "--die-with-parent",
                "sh",
                "-c",
                &script,
                "loopback-wrap",
            ]);
            command
        }
    };
    command.arg(program).args(args);
    Ok((command, guard))
}

/// Starts one socat relay with the given addresses, its output discarded.
fn spawn_relay(addresses: &[String]) -> Result<Child, LoopbackError> {
    Command::new("socat")
        .args(addresses)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| LoopbackError::Io(format!("start a socat relay: {error}")))
}

/// Waits until an outside relay's socket path exists, so the namespace never starts with a relay
/// that cannot connect.
fn wait_for_socket_path(path: &Path) -> Result<(), LoopbackError> {
    let deadline = Instant::now() + SOCKET_WAIT;
    while !path.exists() {
        if Instant::now() >= deadline {
            return Err(LoopbackError::Io(format!(
                "the relay socket {} never appeared",
                path.display()
            )));
        }
        sleep(Duration::from_millis(20));
    }
    Ok(())
}
