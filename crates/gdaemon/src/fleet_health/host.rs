//! Host signals: one `ps` table per sample, macOS memory level, and the 1-minute load.

use std::process::Stdio;
use std::time::Duration;

use tokio::process::Command;

use super::classify::HostSignals;

const COMMAND_TIMEOUT: Duration = Duration::from_secs(5);

/// CPU of the watched processes from one `ps -axco %cpu=,comm=` table.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ProcessSnapshot {
    pub fseventsd_cpu_percent: Option<f64>,
    pub syspolicyd_cpu_percent: Option<f64>,
    pub mds_stores_cpu_percent: Option<f64>,
    pub rustc_running: bool,
}

/// Parse `ps -axco %cpu=,comm=` output. Rows for the same name keep the highest CPU.
pub fn parse_process_table(output: &str) -> ProcessSnapshot {
    let mut snapshot = ProcessSnapshot::default();
    for line in output.lines() {
        let mut fields = line.split_whitespace();
        let (Some(cpu), Some(name)) = (fields.next(), fields.next()) else {
            continue;
        };
        let Ok(cpu) = cpu.parse::<f64>() else {
            continue;
        };
        let slot = match name {
            "fseventsd" => &mut snapshot.fseventsd_cpu_percent,
            "syspolicyd" => &mut snapshot.syspolicyd_cpu_percent,
            "mds_stores" => &mut snapshot.mds_stores_cpu_percent,
            "rustc" => {
                snapshot.rustc_running = true;
                continue;
            }
            _ => continue,
        };
        *slot = Some(slot.map_or(cpu, |current| current.max(cpu)));
    }
    snapshot
}

/// Parse the output of `sysctl -n kern.memorystatus_level` (percent of memory free).
pub fn parse_memory_free_percent(output: &str) -> Option<f64> {
    output.trim().parse::<f64>().ok()
}

/// One-minute load divided by the number of cores.
pub fn load_per_core(load_1m: f64, cores: usize) -> f64 {
    load_1m / cores.max(1) as f64
}

/// Read every host signal once. Each signal that cannot be read is `None`.
pub async fn sample_host() -> HostSignals {
    let cores = std::thread::available_parallelism().map_or(1, |count| count.get());
    let load = loadavg_1m().map(|load| load_per_core(load, cores));

    let table = command_output("ps", &["-axco", "%cpu=,comm="]).await;
    let snapshot = table
        .as_deref()
        .map(parse_process_table)
        .unwrap_or_default();

    let memory = command_output("sysctl", &["-n", "kern.memorystatus_level"])
        .await
        .as_deref()
        .and_then(parse_memory_free_percent);

    HostSignals {
        load_per_core: load,
        memory_free_percent: memory,
        fseventsd_cpu_percent: snapshot.fseventsd_cpu_percent,
        syspolicyd_cpu_percent: snapshot.syspolicyd_cpu_percent,
        mds_stores_cpu_percent: snapshot.mds_stores_cpu_percent,
        rustc_running: table.as_ref().map(|_| snapshot.rustc_running),
    }
}

fn loadavg_1m() -> Option<f64> {
    let mut loads = [0.0_f64; 1];
    // SAFETY: `getloadavg` writes at most `nelem` (1) doubles into the buffer, which holds one.
    let written = unsafe { libc::getloadavg(loads.as_mut_ptr(), 1) };
    (written == 1).then_some(loads[0])
}

/// Stdout of a successful command, or `None` when it fails, exits non-zero or times out.
async fn command_output(program: &str, args: &[&str]) -> Option<String> {
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let output = tokio::time::timeout(COMMAND_TIMEOUT, command.output())
        .await
        .ok()?
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

#[cfg(test)]
#[path = "host/tests.rs"]
mod tests;
