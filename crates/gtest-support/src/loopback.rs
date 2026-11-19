//! Loopback-only mode (design-v2 section 7.4; rulings H8, H9, H23, H30, H32, H46, H56).
//!
//! A harness-side wrapper that runs gdaemon with outbound network limited to loopback: a Seatbelt
//! profile on macOS, a network namespace with relays on Linux, and a refusal on Windows. The
//! proofs connect to an off-host destination with and without the mode. Scratch stub (C17): every
//! entry point returns [`ERR_UNBUILT`] until C18 builds the wrapper.

use std::fmt;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::path::Path;
use std::process::Command;

/// The macOS sandbox launcher the wrapper uses (section 7.4.2).
pub const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";

/// The off-host port the default gateway target uses (section 7.4.4).
pub const OFFHOST_DEFAULT_PORT: u16 = 80;

/// The environment variable that overrides the off-host target: `host:port` (section 7.4.4).
pub const OFFHOST_TARGET_ENV: &str = "GOBBY_HARNESS_OFFHOST_TARGET";

/// The C17 stub's refusal, replaced by the real wrapper in C18.
pub const ERR_UNBUILT: &str = "loopback.rs is unbuilt (design-v2 C18)";

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
    /// `unshare` can create a user and network namespace (`unshare -rn`).
    pub unshare: bool,
    /// `bwrap` is installed (`--unshare-net`).
    pub bwrap: bool,
    /// `socat` is installed (the relays).
    pub socat: bool,
}

/// The mechanism a platform and its tools select.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Plan {
    /// Wrap gdaemon in the macOS Seatbelt profile.
    SandboxExec,
    /// Wrap gdaemon in a network namespace built by this tool.
    Namespace(NamespaceTool),
}

/// The namespace tool a Linux plan uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NamespaceTool {
    /// `unshare -rn`, which execs in place so the wrapper keeps gdaemon's pid.
    Unshare,
    /// `bwrap --unshare-net`, which forks, so the worker is not a direct child of gdaemon.
    Bwrap,
}

/// Chooses the mechanism, or refuses with the clear error for it (fail closed, H23, H46).
pub fn plan(platform: Platform, tools: Tools) -> Result<Plan, LoopbackError> {
    let _ = (platform, tools);
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// The exact Seatbelt profile text (section 7.4.2), one rule per line.
pub fn macos_profile() -> &'static str {
    ""
}

/// The `sandbox-exec` argv that runs `program args` under `profile` (section 7.4.2).
pub fn sandbox_exec_argv(profile: &str, program: &Path, args: &[String]) -> Vec<String> {
    let _ = (profile, program, args);
    Vec::new()
}

/// Accepts an off-host destination: IPv4 literal, not loopback, not a local interface address
/// (section 7.4.4).
pub fn validate_offhost(
    target: SocketAddrV4,
    local: &[Ipv4Addr],
) -> Result<SocketAddrV4, LoopbackError> {
    let _ = (target, local);
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// Chooses the off-host destination: the environment override first, then the default gateway at
/// [`OFFHOST_DEFAULT_PORT`]. Each choice is validated (section 7.4.4).
pub fn select_offhost(
    env_value: Option<&str>,
    gateway: Option<Ipv4Addr>,
    local: &[Ipv4Addr],
) -> Result<SocketAddrV4, LoopbackError> {
    let _ = (env_value, gateway, local);
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// The gateway from macOS `route -n get default` output (its `gateway:` line).
pub fn parse_route_gateway(text: &str) -> Option<Ipv4Addr> {
    let _ = text;
    None
}

/// The gateway from Linux `ip -4 route show default` output (the `via` address).
pub fn parse_ip_route_gateway(text: &str) -> Option<Ipv4Addr> {
    let _ = text;
    None
}

/// The default gateway of this host, read with the platform's route tool.
pub fn default_gateway() -> Option<Ipv4Addr> {
    None
}

/// Every IPv4 address of this host's interfaces.
pub fn local_addresses() -> Result<Vec<Ipv4Addr>, LoopbackError> {
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// The off-host destination for this run, from the environment or the default gateway.
pub fn offhost_target() -> Result<SocketAddrV4, LoopbackError> {
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// Which launch tools this host has.
pub fn tools_present() -> Tools {
    Tools::default()
}

/// The class a probe reported (`loopback_probe.py`), with its errno when there was one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeOutcome {
    /// `connected`, `eperm`, `refused`, `timeout` or `other`.
    pub class: String,
    /// The OS errno, when the connect failed with one.
    pub errno: Option<i64>,
}

/// Runs `loopback_probe.py` once against `target`, either directly or under the loopback mode.
/// `scratch` holds the probe's output file.
pub fn run_probe(
    python: &Path,
    probe: &Path,
    target: SocketAddr,
    wrapped: bool,
    scratch: &Path,
) -> Result<ProbeOutcome, LoopbackError> {
    let _ = (python, probe, target, wrapped, scratch);
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}

/// Relays and launch state that one wrapped gdaemon run owns. Dropping it stops the relays.
#[derive(Debug)]
pub struct LoopbackGuard {
    _private: (),
}

/// A command that runs `program args` under the loopback mode, with the relays the namespace
/// needs for `ports` (the harness ports and the database port). The guard must outlive the child.
pub fn wrap_command(
    program: &Path,
    args: &[&str],
    ports: &[u16],
    scratch: &Path,
) -> Result<(Command, LoopbackGuard), LoopbackError> {
    let _ = (program, args, ports, scratch);
    Err(LoopbackError::Mechanism(ERR_UNBUILT))
}
