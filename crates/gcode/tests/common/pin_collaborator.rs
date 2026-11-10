//! Isolated daemon/Qdrant collaborator for pinned projection CLI acceptance tests.
use gobby_core::grant::GrantBundle;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub const TOKEN: &str = "pin-projection-test-token";

pub struct Collaborator {
    pub url: String,
    pub requests: Arc<Mutex<Vec<String>>>,
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl Collaborator {
    pub fn start(listener: TcpListener, grant: GrantBundle, symbol_id: String) -> Self {
        let address = listener.local_addr().expect("service address");
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let worker = {
            let requests = Arc::clone(&requests);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                while let Ok((mut stream, _)) = listener.accept() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    stream
                        .set_read_timeout(Some(Duration::from_secs(5)))
                        .expect("request timeout");
                    let request = read_http(&mut stream);
                    if !request.contains("HTTP/") {
                        continue;
                    }
                    let line = request.lines().next().expect("request line");
                    let body: Value = request
                        .split_once("\r\n\r\n")
                        .and_then(|(_, body)| serde_json::from_str(body).ok())
                        .unwrap_or(Value::Null);
                    let response = if line.contains("/handshake/challenge") {
                        let nonce = decode(body["nonce"].as_str().expect("nonce"));
                        let key = hmac(TOKEN.as_bytes(), b"gobby-interactive-proof-v1");
                        let proof = hmac(&key, &nonce)
                            .iter()
                            .map(|byte| format!("{byte:02x}"))
                            .collect::<String>();
                        json!({"proof":proof})
                    } else if line.contains("/api/runtime/handshake") {
                        json!({"grant":grant,"deployment_token":grant.deployment.token,"fencing_epoch":grant.deployment.fencing_epoch})
                    } else if line.contains("/api/runtime/config") {
                        json!({"config_revision":grant.config_revision,"settings":{"ai.embeddings.dim":"3"}})
                    } else if line.contains("/api/config/effective") {
                        json!({"revision":1,"config":{"ai.embeddings.dim":"3"}})
                    } else if line.contains("/embeddings") {
                        let count = body["input"].as_array().map_or(1, Vec::len);
                        json!({"model":"embed-small","dim":3,"embeddings":vec![vec![0.1,0.2,0.3];count]})
                    } else if line.contains("/points/search") {
                        json!({"result":[{"id":symbol_id,"score":0.9}]})
                    } else if line.starts_with("GET /collections/") {
                        json!({"result":{"config":{"params":{"vectors":{"size":3,"distance":"Cosine"}}}}})
                    } else if line.starts_with("PUT /collections/")
                        || line.contains("/points/delete")
                    {
                        json!({"result":{"status":"completed","operation_id":1},"status":"ok"})
                    } else if line.starts_with("POST /collections/")
                        && line.contains("/points/count")
                    {
                        // The prune count before a filtered delete: this scripted store holds no points.
                        json!({"result":{"count":0}})
                    } else {
                        panic!("unexpected collaborator request: {line}");
                    };
                    requests.lock().expect("request log").push(request);
                    let body = response.to_string();
                    write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).expect("response");
                }
            })
        };
        Self {
            url: format!("http://{address}"),
            address,
            requests,
            stop,
            worker: Some(worker),
        }
    }
}

impl Drop for Collaborator {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.address);
        if let Some(worker) = self.worker.take() {
            let outcome = worker.join();
            if !thread::panicking() {
                outcome.expect("collaborator thread");
            }
        }
    }
}

fn read_http(stream: &mut TcpStream) -> String {
    let mut bytes = Vec::new();
    let mut buffer = [0; 4096];
    loop {
        let read = stream.read(&mut buffer).expect("HTTP request");
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&buffer[..read]);
        if let Some(end) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
            let headers = String::from_utf8_lossy(&bytes[..end]);
            let length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().expect("content length"))
                })
                .unwrap_or(0);
            if bytes.len() >= end + 4 + length {
                break;
            }
        }
    }
    String::from_utf8(bytes).expect("request UTF-8")
}

// Matches the existing concurrent_vector_grant fixture's interactive proof.
fn hmac(key: &[u8], data: &[u8]) -> Vec<u8> {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|byte| byte ^ 0x36));
    inner.update(data);
    let mut outer = Sha256::new();
    outer.update(block.map(|byte| byte ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().to_vec()
}

fn decode(input: &str) -> Vec<u8> {
    let mut bits = 0u32;
    let mut count = 0;
    let mut bytes = Vec::new();
    for byte in input.bytes().take_while(|byte| *byte != b'=') {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => panic!("invalid nonce"),
        };
        bits = (bits << 6) | u32::from(value);
        count += 6;
        if count >= 8 {
            count -= 8;
            bytes.push((bits >> count) as u8);
        }
    }
    bytes
}
