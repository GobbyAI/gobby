//! Bounded channel admission, revocation, and reconnects.
use super::{
    HubNodes,
    registry::{PersistOutcome, Registry},
};
use axum::{
    body::Body,
    extract::{
        FromRequestParts,
        ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade},
    },
    http::{Request, Response, StatusCode},
    response::IntoResponse,
};
use futures_util::{Sink, SinkExt, StreamExt};
use gobby_core::postgres_pool::Pool;
use std::sync::Arc;
use std::time::Duration;
use tokio::{sync::oneshot, time::Instant};
use uuid::Uuid;

#[derive(Clone, Copy)]
pub struct ChannelTiming {
    pub ping_interval: Duration,
    pub backoff_start: Duration,
    pub backoff_max: Duration,
    pub attempt_timeout: Duration,
    pub hello_timeout: Duration,
    pub idle_timeout: Duration,
    pub key_recheck: Duration,
    pub recheck_deadline: Duration,
    pub heartbeat_write: Duration,
}
impl Default for ChannelTiming {
    fn default() -> Self {
        Self {
            ping_interval: Duration::from_secs(30),
            backoff_start: Duration::from_secs(1),
            backoff_max: Duration::from_secs(60),
            attempt_timeout: Duration::from_secs(20),
            hello_timeout: Duration::from_secs(10),
            idle_timeout: Duration::from_secs(90),
            key_recheck: Duration::from_secs(25),
            recheck_deadline: Duration::from_secs(4),
            heartbeat_write: Duration::from_secs(60),
        }
    }
}
#[derive(Debug, PartialEq, Eq)]
pub enum AckOutcome {
    Live,
    Close(u16),
    Drop,
}
pub async fn send_ack<S: Sink<Message> + Unpin>(
    sink: &mut S,
    registry: &Registry,
    machine: Uuid,
    connection: Uuid,
    close: &mut oneshot::Receiver<(u16, &'static str)>,
    admission_end: Instant,
) -> AckOutcome {
    let message = Message::Text(
        serde_json::json!({
            "type": "ack", "machine_id": machine
        })
        .to_string()
        .into(),
    );
    let outcome = tokio::select! {
        biased;
        signal = close => AckOutcome::Close(signal.map_or(4409, |(code, _)| code)),
        _ = tokio::time::sleep_until(admission_end) => AckOutcome::Drop,
        result = sink.send(message) => {
            if result.is_err() { AckOutcome::Drop }
            else if registry.mark_live(machine, connection) { AckOutcome::Live }
            else { AckOutcome::Close(4409) }
        }
    };
    if outcome != AckOutcome::Live {
        registry.remove(machine, connection);
    }
    outcome
}
pub async fn key_tick<S: Sink<Message> + Unpin>(
    sink: &mut S,
    pool: &Pool,
    registry: &Registry,
    machine: Uuid,
    connection: Uuid,
    key: Uuid,
    deadline: Instant,
) -> bool {
    let started = Instant::now();
    match tokio::time::timeout_at(deadline, key_valid(pool, key)).await {
        Ok(Ok(false)) => {
            registry.remove(machine, connection);
            close_until(sink, 4401, deadline).await;
            true
        }
        Ok(Ok(true)) => false,
        Ok(Err(error)) => {
            eprintln!("node channel key recheck unproven: {error:#}");
            false
        }
        Err(_) => {
            eprintln!(
                "node channel key recheck timed out after {:?}; budget {:?}; pool {:?}",
                started.elapsed(),
                deadline.saturating_duration_since(started),
                pool.status()
            );
            false
        }
    }
}

async fn key_valid(pool: &Pool, key: Uuid) -> anyhow::Result<bool> {
    let client = pool.get().await?;
    Ok(client
        .query_opt(
            "SELECT 1 FROM api_keys WHERE id=$1::text::uuid AND revoked_at IS NULL",
            &[&key.to_string()],
        )
        .await?
        .is_some())
}

async fn close_until<S: Sink<Message> + Unpin>(sink: &mut S, code: u16, deadline: Instant) {
    let _ = tokio::time::timeout_at(
        deadline,
        sink.send(Message::Close(Some(CloseFrame {
            code,
            reason: "".into(),
        }))),
    )
    .await;
}

/// Only the authenticated hub front door mounts this endpoint.
pub async fn handle(nodes: Arc<HubNodes>, request: Request<Body>) -> Response<Body> {
    let identity = ["x-gobby-machine-id", "x-gobby-key-id"].map(|name| {
        request
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<Uuid>().ok())
    });
    let [Some(machine), Some(key)] = identity else {
        return (
            StatusCode::UNAUTHORIZED,
            [("content-type", "application/json")],
            r#"{"error":"missing_auth","message":"A machine-bound API key is required"}"#,
        )
            .into_response();
    };
    let (mut parts, _) = request.into_parts();
    match WebSocketUpgrade::from_request_parts(&mut parts, &()).await {
        Ok(upgrade) => upgrade
            .on_upgrade(move |socket| hub_channel(socket, nodes, machine, key))
            .into_response(),
        Err(rejection) => rejection.into_response(),
    }
}

#[derive(serde::Deserialize)]
struct Hello {
    #[serde(rename = "type")]
    kind: String,
    node_version: String,
    platform: String,
}

fn hello_version(message: Message) -> Option<String> {
    let Message::Text(text) = message else {
        return None;
    };
    let hello: Hello = serde_json::from_str(&text).ok()?;
    (hello.kind == "hello"
        && hello.node_version.chars().count() <= 64
        && hello.platform.chars().count() <= 64)
        .then_some(hello.node_version)
}

fn persist_heartbeat(
    nodes: Arc<HubNodes>,
    machine: Uuid,
    connection: Uuid,
    version: String,
) -> tokio::task::JoinHandle<PersistOutcome<anyhow::Result<()>>> {
    tokio::spawn(async move {
        nodes.registry.persist(machine, connection, async {
            let client = nodes.db.get().await?;
            client.execute(
                "UPDATE machines SET last_heartbeat_at=now(),node_version=$2 WHERE id=$1::text::uuid",
                &[&machine.to_string(), &version],
            ).await?;
            Ok(())
        }).await
    })
}

async fn hub_channel(mut socket: WebSocket, nodes: Arc<HubNodes>, machine: Uuid, key: Uuid) {
    let timing = nodes.timing;
    let upgraded = Instant::now();
    let hello_deadline = upgraded + timing.hello_timeout;
    let admission_end = hello_deadline + timing.recheck_deadline;
    let mut rechecks = tokio::time::interval_at(upgraded + timing.key_recheck, timing.key_recheck);
    rechecks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let version = match tokio::time::timeout_at(hello_deadline, socket.recv()).await {
        Ok(Some(Ok(message))) => match hello_version(message) {
            Some(version) => version,
            None => {
                close_until(&mut socket, 1008, Instant::now() + timing.recheck_deadline).await;
                return;
            }
        },
        Err(_) => {
            close_until(&mut socket, 1011, Instant::now() + timing.recheck_deadline).await;
            return;
        }
        _ => return,
    };
    let (connection, mut close) = nodes.registry.register(machine);
    let mut last_write = Instant::now();
    let mut write = persist_heartbeat(nodes.clone(), machine, connection, version.clone());
    let admission = tokio::select! {
        biased;
        signal = &mut close => Err(signal.map_or(4409, |(code, _)| code)),
        _ = tokio::time::sleep_until(hello_deadline) => Err(1011),
        result = &mut write => match result {
            Ok(PersistOutcome::Written(Ok(()))) => Ok(()),
            _ => Err(1011),
        }
    };
    if let Err(code) = admission {
        nodes.registry.remove(machine, connection);
        close_until(&mut socket, code, Instant::now() + timing.recheck_deadline).await;
        return;
    }
    let proven = tokio::select! {
        biased;
        signal = &mut close => Err(signal.map_or(4409, |(code, _)| code)),
        result = tokio::time::timeout(timing.recheck_deadline, key_valid(&nodes.db, key)) =>
            match result {
                Ok(Ok(true)) => Ok(()),
                Ok(Ok(false)) => Err(4401),
                _ => Err(1011),
            },
    };
    if let Err(code) = proven {
        nodes.registry.remove(machine, connection);
        close_until(&mut socket, code, Instant::now() + timing.recheck_deadline).await;
        return;
    }
    match send_ack(
        &mut socket,
        &nodes.registry,
        machine,
        connection,
        &mut close,
        admission_end,
    )
    .await
    {
        AckOutcome::Live => {}
        AckOutcome::Close(code) => {
            close_until(&mut socket, code, Instant::now() + timing.recheck_deadline).await;
            return;
        }
        AckOutcome::Drop => return,
    }
    let mut idle = Instant::now() + timing.idle_timeout;
    loop {
        tokio::select! {
            biased;
            signal = &mut close => {
                close_until(&mut socket, signal.map_or(4409, |(code, _)| code),
                    Instant::now() + timing.recheck_deadline).await;
                break;
            },
            _ = tokio::time::sleep_until(idle) => {
                nodes.registry.remove(machine, connection);
                close_until(&mut socket, 4408, Instant::now() + timing.recheck_deadline).await;
                break;
            },
            tick = rechecks.tick() => {
                if key_tick(&mut socket, &nodes.db, &nodes.registry, machine, connection, key,
                    tick + timing.recheck_deadline).await { break; }
            },
            frame = socket.recv() => {
                let Some(Ok(frame)) = frame else { break; };
                idle = Instant::now() + timing.idle_timeout;
                match frame {
                    Message::Close(_) => break,
                    Message::Ping(_) if last_write.elapsed() >= timing.heartbeat_write && write.is_finished() => {
                        last_write = Instant::now();
                        write = persist_heartbeat(nodes.clone(), machine, connection, version.clone());
                    },
                    _ => {},
                }
            },
        }
    }
    nodes.registry.remove(machine, connection);
}

#[cfg(unix)]
trait ChannelIo: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send {}
#[cfg(unix)]
impl<T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send> ChannelIo for T {}

#[cfg(unix)]
async fn connect_client(
    node: &crate::state::NodeServices,
) -> anyhow::Result<tokio_tungstenite::WebSocketStream<Box<dyn ChannelIo>>> {
    use anyhow::Context;
    use tokio_tungstenite::tungstenite::{Message as PeerMessage, client::IntoClientRequest};
    let key = gobby_core::local_token::read_api_key_at(
        node.bootstrap_path
            .parent()
            .context("bootstrap has no parent directory")?,
    )?;
    let relay = &node.relay;
    let stream = tokio::net::TcpStream::connect(&relay.dial).await?;
    let stream: Box<dyn ChannelIo> = if let Some(config) = &relay.tls {
        let name = tokio_rustls::rustls::pki_types::ServerName::try_from(relay.host.clone())?;
        Box::new(
            tokio_rustls::TlsConnector::from(config.clone())
                .connect(name, stream)
                .await?,
        )
    } else {
        Box::new(stream)
    };
    let scheme = if relay.tls.is_some() { "wss" } else { "ws" };
    let mut request =
        format!("{scheme}://{}/api/nodes/channel", relay.authority).into_client_request()?;
    request
        .headers_mut()
        .insert("authorization", format!("Bearer {key}").parse()?);
    let (mut socket, _) =
        tokio_tungstenite::client_async_with_config(request, stream, None).await?;
    socket
        .send(PeerMessage::Text(
            serde_json::json!({
                "type": "hello", "node_version": env!("CARGO_PKG_VERSION"),
                "platform": format!("{}/{}", std::env::consts::OS, std::env::consts::ARCH),
            })
            .to_string()
            .into(),
        ))
        .await?;
    let message = socket.next().await.context("channel closed before ack")??;
    let ack: serde_json::Value = serde_json::from_str(message.to_text()?)?;
    anyhow::ensure!(
        ack["type"] == "ack"
            && ack["machine_id"]
                .as_str()
                .is_some_and(|id| id.parse::<Uuid>().is_ok()),
        "invalid channel ack"
    );
    Ok(socket)
}

#[cfg(unix)]
pub async fn run_client(node: Arc<crate::state::NodeServices>, timing: ChannelTiming) {
    use tokio_tungstenite::tungstenite::Message as PeerMessage;
    let mut backoff = timing.backoff_start;
    loop {
        if let Ok(Ok(mut socket)) =
            tokio::time::timeout(timing.attempt_timeout, connect_client(&node)).await
        {
            backoff = timing.backoff_start;
            let mut idle = Instant::now() + timing.idle_timeout;
            let mut pings = tokio::time::interval_at(
                Instant::now() + timing.ping_interval,
                timing.ping_interval,
            );
            pings.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tokio::select! {
                    biased;
                    _ = tokio::time::sleep_until(idle) => break,
                    frame = socket.next() => {
                        match frame {
                            Some(Ok(PeerMessage::Close(_))) | Some(Err(_)) | None => break,
                            Some(Ok(_)) => idle = Instant::now() + timing.idle_timeout,
                        }
                    },
                    _ = pings.tick() => {
                        if !matches!(tokio::time::timeout_at(idle,
                            socket.send(PeerMessage::Ping(Vec::new().into()))).await, Ok(Ok(()))) {
                            break;
                        }
                    },
                }
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = backoff.saturating_mul(2).min(timing.backoff_max);
    }
}
