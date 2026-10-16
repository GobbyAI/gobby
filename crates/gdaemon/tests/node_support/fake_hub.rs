use anyhow::Result;
use axum::{
    Router,
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, Method, StatusCode},
    response::Response,
};
use serde_json::Value;
use std::{
    collections::{HashMap, VecDeque},
    sync::{Arc, Mutex},
};
use tokio::{net::TcpListener, task::JoinHandle};

#[derive(Clone, Debug)]
pub struct RecordedRequest {
    pub method: Method,
    pub path: String,
    pub headers: HeaderMap,
    pub body: Value,
}

#[derive(Default)]
struct HubState {
    requests: Vec<RecordedRequest>,
    scripts: HashMap<String, VecDeque<(StatusCode, Value)>>,
}

pub struct FakeHub {
    pub origin: String,
    state: Arc<Mutex<HubState>>,
    server: JoinHandle<()>,
}

impl FakeHub {
    pub async fn start() -> Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let origin = format!("http://{}", listener.local_addr()?);
        let state = Arc::new(Mutex::new(HubState::default()));
        let app = Router::new().fallback(record).with_state(state.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.expect("fake hub server");
        });
        Ok(Self {
            origin,
            state,
            server,
        })
    }

    pub fn script(&self, path: &str, replies: Vec<(StatusCode, Value)>) {
        self.state
            .lock()
            .unwrap()
            .scripts
            .insert(path.to_owned(), replies.into());
    }

    pub fn requests(&self) -> Vec<RecordedRequest> {
        self.state.lock().unwrap().requests.clone()
    }

    pub async fn shutdown(self) {
        self.server.abort();
        let _ = self.server.await;
    }
}

async fn record(State(state): State<Arc<Mutex<HubState>>>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let bytes = axum::body::to_bytes(body, 1024 * 1024).await.unwrap();
    let body = serde_json::from_slice(&bytes).unwrap();
    let path = parts.uri.path().to_owned();
    let mut state = state.lock().unwrap();
    state.requests.push(RecordedRequest {
        method: parts.method,
        path: path.clone(),
        headers: parts.headers,
        body,
    });
    let (status, body) = state
        .scripts
        .get_mut(&path)
        .and_then(VecDeque::pop_front)
        .unwrap_or((StatusCode::NOT_FOUND, Value::Null));
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(Body::from(serde_json::to_vec(&body).unwrap()))
        .unwrap()
}
