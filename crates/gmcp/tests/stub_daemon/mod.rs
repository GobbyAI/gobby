use serde_json::Value;
use std::collections::{BTreeMap, VecDeque};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::{
    Arc, Condvar, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Debug)]
pub struct Request {
    pub method: String,
    pub path: String,
    pub headers: BTreeMap<String, String>,
    pub body: Value,
}

pub struct Response {
    pub status: u16,
    pub body: String,
    pub delay: Duration,
}

impl Response {
    pub fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            body: body.to_string(),
            delay: Duration::ZERO,
        }
    }
}

pub struct StubDaemon {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    requests: Arc<Mutex<Vec<Request>>>,
    arrived: Arc<Condvar>,
    worker: Option<JoinHandle<()>>,
}

impl StubDaemon {
    pub fn start(responses: Vec<Response>) -> anyhow::Result<Self> {
        Self::bind("127.0.0.1:0".parse()?, responses)
    }

    pub fn bind(address: SocketAddr, responses: Vec<Response>) -> anyhow::Result<Self> {
        let listener = TcpListener::bind(address)?;
        let address = listener.local_addr()?;
        listener.set_nonblocking(true)?;
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stopped = stop.clone();
        let recorded = requests.clone();
        let arrived = Arc::new(Condvar::new());
        let notification = arrived.clone();
        let worker = thread::spawn(move || {
            let mut responses: VecDeque<_> = responses.into();
            let mut handlers = Vec::new();
            while !stopped.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        // Accepted sockets inherit nonblocking mode on macOS.
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(2)))
                            .unwrap();
                        let request = read_request(&mut stream);
                        recorded.lock().unwrap().push(request);
                        notification.notify_all();
                        let response = responses.pop_front().expect("unexpected request to stub");
                        handlers.push(thread::spawn(move || {
                            thread::sleep(response.delay);
                            let wire = format!("HTTP/1.1 {} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}", response.status, response.body.len(), response.body);
                            let _ = stream.write_all(wire.as_bytes());
                        }));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2))
                    }
                    Err(error) => panic!("stub accept: {error}"),
                }
            }
            for handler in handlers {
                handler.join().unwrap();
            }
        });
        Ok(Self {
            address,
            stop,
            requests,
            arrived,
            worker: Some(worker),
        })
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.address)
    }
    pub fn requests(&self) -> Vec<Request> {
        self.requests.lock().unwrap().clone()
    }
    pub fn wait_for_requests(&self, count: usize) {
        let (requests, timeout) = self
            .arrived
            .wait_timeout_while(
                self.requests.lock().unwrap(),
                Duration::from_secs(2),
                |requests| requests.len() < count,
            )
            .unwrap();
        assert!(
            !timeout.timed_out(),
            "expected {count} requests, got {}",
            requests.len()
        );
    }
}

impl Drop for StubDaemon {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.take().unwrap().join().unwrap();
    }
}

fn read_request(stream: &mut TcpStream) -> Request {
    let mut bytes = Vec::new();
    let header_end = loop {
        let mut byte = [0];
        stream.read_exact(&mut byte).unwrap();
        bytes.push(byte[0]);
        if bytes.ends_with(b"\r\n\r\n") {
            break bytes.len();
        }
    };
    let header = String::from_utf8(bytes[..header_end].to_vec()).unwrap();
    let mut lines = header.lines();
    let mut first = lines.next().unwrap().split_whitespace();
    let method = first.next().unwrap().to_owned();
    let path = first.next().unwrap().to_owned();
    let headers: BTreeMap<_, _> = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(name, value)| (name.to_ascii_lowercase(), value.trim().to_owned()))
        .collect();
    let length = headers
        .get("content-length")
        .map_or(0, |value| value.parse().unwrap());
    let mut body = vec![0; length];
    stream.read_exact(&mut body).unwrap();
    Request {
        method,
        path,
        headers,
        body: if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&body).unwrap()
        },
    }
}
