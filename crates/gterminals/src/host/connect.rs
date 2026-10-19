//! Connecting to and authenticating the gterm host's control socket.

use std::time::Duration;

use tokio::net::UnixStream;
use tokio::process::Child;
use tokio::time::timeout;

use super::process::process_is_host;
use super::{
    Adoption, HostFailure, HostInfo, HostOptions, Inner, READY_DEADLINE, control_failure, pre_io,
    spawn,
};
use crate::control::ControlClient;

/// Opens an authenticated control connection of its own beside the
/// supervisor's. The existing token is read, never created or rotated, and
/// the host pid is verified as adoption verifies it.
pub(crate) async fn open_control(
    options: &HostOptions,
) -> Result<(ControlClient<UnixStream>, HostInfo), HostFailure> {
    let opened = timeout(READY_DEADLINE, async {
        let stream = connect(options)
            .await?
            .ok_or_else(|| HostFailure::PreAdoptionIo("gterm host is not running".into()))?;
        // The handshake always returns the client it authenticated.
        let (Some(client), info) = handshake(options, stream, true, None).await? else {
            return Err(HostFailure::Authentication);
        };
        Ok((client, info))
    })
    .await;
    opened.unwrap_or_else(|_| {
        Err(HostFailure::PreAdoptionIo(
            "host handshake timed out".into(),
        ))
    })
}

pub(super) async fn adopt_or_spawn(
    inner: &Inner,
    child: &mut Option<Child>,
) -> Result<Adoption, HostFailure> {
    match connect(&inner.options).await? {
        Some(stream) => {
            let owned_pid = child.as_ref().and_then(Child::id);
            authenticate(inner, stream, owned_pid.is_none(), owned_pid).await
        }
        None => {
            // A host spawned by this lifecycle may still be binding its socket.
            // Never launch a second candidate while that process is alive.
            if child.is_none() {
                *child = Some(spawn(inner).await?);
            }
            let pid = child
                .as_ref()
                .and_then(Child::id)
                .ok_or(HostFailure::Stopped)?;
            loop {
                match UnixStream::connect(inner.options.socket_dir.join("gterm-control.sock")).await
                {
                    Ok(stream) => return authenticate(inner, stream, false, Some(pid)).await,
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
                        ) => {}
                    Err(error) => return Err(pre_io(error)),
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
        }
    }
}

pub(super) async fn connect(options: &HostOptions) -> Result<Option<UnixStream>, HostFailure> {
    match UnixStream::connect(options.socket_dir.join("gterm-control.sock")).await {
        Ok(stream) => Ok(Some(stream)),
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
            ) =>
        {
            match tokio::fs::read_to_string(options.socket_dir.join("gterm.pid")).await {
                Ok(contents) => {
                    let pid = contents
                        .trim()
                        .parse()
                        .map_err(|_| HostFailure::PidMismatch)?;
                    if process_is_host(pid).await? {
                        return Err(HostFailure::PreAdoptionIo(
                            "live host is not listening".into(),
                        ));
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(pre_io(error)),
            }
            Ok(None)
        }
        Err(error) => Err(pre_io(error)),
    }
}

async fn authenticate(
    inner: &Inner,
    stream: UnixStream,
    adopted: bool,
    spawned_pid: Option<u32>,
) -> Result<Adoption, HostFailure> {
    let adoption = handshake(&inner.options, stream, adopted, spawned_pid).await?;
    let bindings = inner.authority.bindings(&adoption.1.epoch).await?;
    if bindings
        .iter()
        .any(|row| row.machine_id != inner.options.machine_id)
    {
        return Err(HostFailure::EpochRefused);
    }
    Ok(adoption)
}

// Explicit drain authenticates the wire and process, independently of adoption authority.
pub(super) async fn handshake(
    options: &HostOptions,
    stream: UnixStream,
    adopted: bool,
    spawned_pid: Option<u32>,
) -> Result<Adoption, HostFailure> {
    let token = tokio::fs::read_to_string(options.socket_dir.join("gterm-control.token"))
        .await
        .map_err(|_| HostFailure::TokenMismatch)?;
    if token.trim().is_empty() {
        return Err(HostFailure::TokenMismatch);
    }
    let mut client = ControlClient::new(stream);
    let hello = client
        .hello(token.trim())
        .await
        .map_err(|error| control_failure(error, false))?;
    let ping = client
        .ping()
        .await
        .map_err(|error| control_failure(error, false))?;
    verify_pid(options, ping.host_pid).await?;
    if spawned_pid.is_some_and(|pid| pid != ping.host_pid) {
        return Err(HostFailure::PidMismatch);
    }
    Ok((
        Some(client),
        HostInfo {
            pid: ping.host_pid,
            epoch: ping.host_epoch,
            version: hello.version,
            adopted,
        },
    ))
}

pub(super) async fn read_pid(options: &HostOptions) -> Result<u32, HostFailure> {
    tokio::fs::read_to_string(options.socket_dir.join("gterm.pid"))
        .await
        .map_err(pre_io)?
        .trim()
        .parse()
        .map_err(|_| HostFailure::PidMismatch)
}

pub(super) async fn verify_pid(options: &HostOptions, pid: u32) -> Result<(), HostFailure> {
    if read_pid(options).await? != pid || !process_is_host(pid).await? {
        return Err(HostFailure::PidMismatch);
    }
    Ok(())
}
