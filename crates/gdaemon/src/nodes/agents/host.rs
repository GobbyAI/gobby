//! Node-local terminal host integration.
use super::NodeAgents;
use gobby_terminals::{
    control::{ControlClient, ControlError},
    host::{EpochAuthority, EpochBinding, HostFailure, HostOptions, READY_DEADLINE},
};
use std::{
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
    sync::Arc,
    time::Duration,
};
use tokio::{net::UnixStream, sync::Mutex, time::timeout};

pub struct NodeEpochAuthority {
    pub agents: Arc<NodeAgents>,
}

impl EpochAuthority for NodeEpochAuthority {
    fn bindings<'a>(
        &'a self,
        _epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async { Ok(Vec::new()) })
    }

    fn observe_epoch<'a>(
        &'a self,
        _machine: uuid::Uuid,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<(), HostFailure>> + Send + 'a>> {
        Box::pin(async move {
            let mut state = self.agents.state.lock().await;
            if state.host_epoch_id.as_deref() == Some(epoch) {
                return Ok(());
            }
            // Publish the in-memory generation only after its durable commit.
            let mut next = state.clone();
            next.host_epoch = next
                .host_epoch
                .checked_add(1)
                .ok_or_else(|| HostFailure::Authority("node host epoch exhausted".into()))?;
            next.host_epoch_id = Some(epoch.to_owned());
            next.commit(&self.agents.home, &self.agents.changed)
                .map_err(|error| HostFailure::Authority(error.to_string()))?;
            *state = next;
            Ok(())
        })
    }
}

pub fn node_host_options(home: &Path, machine_id: String) -> HostOptions {
    HostOptions {
        socket_dir: home.to_owned(),
        binary_path: home.join("bin/gterm"),
        machine_id,
        gobby_home: Some(home.to_owned()),
        health_interval: Duration::from_secs(5),
        retry_ceiling: Duration::from_secs(30),
        shutdown_grace: Duration::from_secs(10),
        spawn_args: [
            ("--tmux-poll-interval-ms", 150),
            ("--tmux-poll-backoff-ceiling-ms", 5000),
            ("--max-attached-terminals", 64),
            ("--max-attachments-total", 128),
            ("--max-attachments-per-terminal", 8),
            ("--tmux-attach-history-lines", 500),
            ("--tmux-attach-history-max-bytes", 256 * 1024),
        ]
        .into_iter()
        .flat_map(|(flag, value)| [flag.to_owned(), value.to_string()])
        .collect(),
    }
}

pub struct HostControl {
    home: PathBuf,
    client: Mutex<Option<ControlClient<UnixStream>>>,
}

impl HostControl {
    pub fn new(home: &Path) -> Self {
        Self {
            home: home.to_owned(),
            client: Mutex::new(None),
        }
    }

    /// Serialize host operations and reconnect once on transport failure.
    /// Callers must use stable spawn/operation keys when replaying mutations.
    pub async fn call<T>(
        &self,
        mut operation: impl for<'a> FnMut(
            &'a mut ControlClient<UnixStream>,
        ) -> Pin<
            Box<dyn Future<Output = Result<T, ControlError>> + Send + 'a>,
        >,
    ) -> Result<T, ControlError> {
        let mut slot = self.client.lock().await;
        // Taking the client also makes cancellation discard a partial roundtrip.
        let mut client = match slot.take() {
            Some(client) => client,
            None => self.connect().await?,
        };
        for attempt in 0..2 {
            let result = timeout(READY_DEADLINE, operation(&mut client))
                .await
                .unwrap_or_else(|_| Err(deadline_error()));
            match result {
                Ok(value) => {
                    *slot = Some(client);
                    return Ok(value);
                }
                Err(ControlError::Io(_) | ControlError::Closed) if attempt == 0 => {
                    client = self.connect().await?;
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("the second attempt always returns")
    }

    async fn connect(&self) -> Result<ControlClient<UnixStream>, ControlError> {
        timeout(READY_DEADLINE, async {
            let token = tokio::fs::read_to_string(self.home.join("gterm-control.token")).await?;
            if token.trim().is_empty() {
                return Err(ControlError::NotAuthenticated);
            }
            let stream = UnixStream::connect(self.home.join("gterm-control.sock")).await?;
            let mut client = ControlClient::new(stream);
            client.hello(token.trim()).await?;
            Ok(client)
        })
        .await
        .unwrap_or_else(|_| Err(deadline_error()))
    }
}

fn deadline_error() -> ControlError {
    std::io::Error::new(
        std::io::ErrorKind::TimedOut,
        "node host control deadline exceeded",
    )
    .into()
}
