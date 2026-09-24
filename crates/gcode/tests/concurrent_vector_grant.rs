//! Concurrent `gcode vector sync-file` processes share one interactive grant
//! handshake. That command is the process the code-index sync gateway starts
//! for each pending file.

#[cfg(test)]
mod serial_db {
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::thread;
    use std::time::Duration;

    use postgres::{Client, NoTls};
    use sha2::{Digest, Sha256};

    use gobby_code::test_env;
    use gobby_core::grant::{
        AiCapability, DirectConnections, GrantBundle, PrincipalKind, managed_direct_grant,
    };

    const CODE_INDEX_UUID_NAMESPACE: uuid::Uuid = uuid::Uuid::from_bytes([
        0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00,
    ]);
    const FILE_PATHS: [&str; 4] = ["src/one.rs", "src/two.rs", "src/three.rs", "src/four.rs"];
    const CLI_TOKEN: &str = "grant-test-token";
    const SLOW_HANDSHAKE: Duration = Duration::from_secs(1);
    const SLOW_EFFECTIVE_CONFIG: Duration = Duration::from_secs(2);

    struct Counts {
        challenges: AtomicUsize,
        handshakes: AtomicUsize,
        configs: AtomicUsize,
        effective: AtomicUsize,
        qdrant: AtomicUsize,
    }

    struct Collaborator {
        url: String,
        counts: Arc<Counts>,
        shutdown: Arc<AtomicBool>,
        join: Option<thread::JoinHandle<()>>,
    }

    impl Drop for Collaborator {
        fn drop(&mut self) {
            self.shutdown.store(true, Ordering::SeqCst);
            if let Some(addr) = self.url.strip_prefix("http://") {
                let _ = TcpStream::connect(addr);
            }
            if let Some(handle) = self.join.take() {
                let _ = handle.join();
            }
        }
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn concurrent_vector_sync_files_share_one_slow_grant_handshake() {
        let database_url = test_env::postgres_test_database_url("concurrent vector grant");
        let project_id =
            uuid::Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, b"gcode-concurrent-vector-grant")
                .to_string();
        let mut conn = Client::connect(&database_url, NoTls).expect("connect PostgreSQL");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };

        let root = tempfile::tempdir().expect("temp project");
        std::fs::create_dir_all(root.path().join(".gobby")).expect("create .gobby");
        std::fs::create_dir_all(root.path().join("src")).expect("create src");
        std::fs::write(
            root.path().join(".gobby/project.json"),
            serde_json::json!({"id": project_id, "name": "concurrent-vector-grant"}).to_string(),
        )
        .expect("write project identity");
        for file_path in FILE_PATHS {
            std::fs::write(root.path().join(file_path), "pub fn item() {}\n")
                .expect("write source");
            seed_pending_file(&mut conn, &project_id, file_path);
        }
        let home = isolated_home(root.path());

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind collaborator");
        let addr = listener.local_addr().expect("collaborator addr");
        let url = format!("http://{addr}");
        let grant = interactive_grant(&project_id, &home, &database_url, &url);
        let collaborator = spawn_collaborator(listener, url, grant);

        let mut handles = Vec::new();
        for file_path in FILE_PATHS {
            let root_path = root.path().to_path_buf();
            let home_path = home.clone();
            let daemon_url = collaborator.url.clone();
            let file_path = file_path.to_string();
            handles.push(thread::spawn(move || {
                sync_file(&root_path, &home_path, &daemon_url, &file_path)
            }));
        }
        let mut failures = Vec::new();
        for handle in handles {
            let output = handle.join().expect("sync thread");
            let stderr = String::from_utf8_lossy(&output.stderr);
            if !output.status.success() || stderr.contains("grant operation timed out") {
                failures.push(format!(
                    "exit {:?}\nstdout: {}\nstderr: {stderr}",
                    output.status.code(),
                    String::from_utf8_lossy(&output.stdout),
                ));
            }
        }
        assert!(
            failures.is_empty(),
            "concurrent vector syncs failed (handshakes={}, challenges={}, configs={}, qdrant={}):\n{}",
            collaborator.counts.handshakes.load(Ordering::SeqCst),
            collaborator.counts.challenges.load(Ordering::SeqCst),
            collaborator.counts.configs.load(Ordering::SeqCst),
            collaborator.counts.qdrant.load(Ordering::SeqCst),
            failures.join("\n---\n")
        );

        let rows = conn
            .query(
                "SELECT file_path, vectors_synced
                 FROM code_indexed_files
                 WHERE project_id = $1
                 ORDER BY file_path",
                &[&uuid_param(&project_id)],
            )
            .expect("read synced rows");
        let synced: Vec<(String, bool)> = rows
            .iter()
            .map(|row| (row.get::<_, String>(0), row.get::<_, bool>(1)))
            .collect();
        let mut expected: Vec<(String, bool)> = FILE_PATHS
            .iter()
            .map(|path| ((*path).to_string(), true))
            .collect();
        expected.sort();
        assert_eq!(
            synced, expected,
            "every pending file reaches vectors_synced"
        );
        assert_eq!(
            collaborator.counts.handshakes.load(Ordering::SeqCst),
            1,
            "one handshake, challenges={}, configs={}, qdrant={}",
            collaborator.counts.challenges.load(Ordering::SeqCst),
            collaborator.counts.configs.load(Ordering::SeqCst),
            collaborator.counts.qdrant.load(Ordering::SeqCst),
        );
        assert_eq!(collaborator.counts.challenges.load(Ordering::SeqCst), 1);
        assert_eq!(
            collaborator.counts.effective.load(Ordering::SeqCst),
            1,
            "one effective-config fetch, qdrant={}",
            collaborator.counts.qdrant.load(Ordering::SeqCst),
        );
        assert!(
            collaborator.counts.qdrant.load(Ordering::SeqCst) >= FILE_PATHS.len(),
            "each sync must reach the scripted vector store"
        );
    }

    fn sync_file(
        root: &Path,
        home: &Path,
        daemon_url: &str,
        file_path: &str,
    ) -> std::process::Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
        command
            .current_dir(root)
            .arg("--allow-stale")
            .arg("--format")
            .arg("json")
            .args(["vector", "sync-file", "--file", file_path, "--project"])
            .arg(root)
            .env("GOBBY_HOME", home)
            .env("GOBBY_DAEMON_URL", daemon_url)
            .env_remove("GOBBY_MANAGED_EXECUTION_BOOTSTRAP")
            .env_remove("GOBBY_SESSION_ID")
            .env_remove("GOBBY_AGENT_RUN_ID")
            .env_remove("GOBBY_MANAGED_EXECUTION_ID")
            .env_remove("GOBBY_AGENT_API_TOKEN");
        command.output().expect("run gcode vector sync-file")
    }

    fn interactive_grant(
        project_id: &str,
        home: &Path,
        database_url: &str,
        qdrant_url: &str,
    ) -> GrantBundle {
        let machine = std::fs::read_to_string(home.join("machine_id")).expect("read machine id");
        let connections = DirectConnections::postgres(database_url).with_qdrant(qdrant_url, None);
        let mut grant = managed_direct_grant(project_id, machine.trim(), &connections);
        grant.principal.kind = PrincipalKind::Interactive;
        grant.principal.execution_id = None;
        grant.principal.session_id = None;
        grant.capabilities.embed = AiCapability::Daemon {};
        grant.deployment.token = gobby_core::grant::deployment_token(home);
        grant.with_checksum()
    }

    fn spawn_collaborator(listener: TcpListener, url: String, grant: GrantBundle) -> Collaborator {
        let handshake_body = serde_json::json!({
            "grant": serde_json::to_value(&grant).expect("grant json"),
            "deployment_token": grant.deployment.token,
            "fencing_epoch": grant.deployment.fencing_epoch,
        })
        .to_string();
        let config_body = serde_json::json!({
            "config_revision": grant.config_revision,
            "settings": {"ai.embeddings.dim": "768"},
        })
        .to_string();
        let counts = Arc::new(Counts {
            challenges: AtomicUsize::new(0),
            handshakes: AtomicUsize::new(0),
            configs: AtomicUsize::new(0),
            effective: AtomicUsize::new(0),
            qdrant: AtomicUsize::new(0),
        });
        let shutdown = Arc::new(AtomicBool::new(false));
        let counts_thread = Arc::clone(&counts);
        let shutdown_thread = Arc::clone(&shutdown);
        let join = thread::spawn(move || {
            serve(
                listener,
                &handshake_body,
                &config_body,
                &counts_thread,
                &shutdown_thread,
            );
        });
        Collaborator {
            url,
            counts,
            shutdown,
            join: Some(join),
        }
    }

    fn serve(
        listener: TcpListener,
        handshake_body: &str,
        config_body: &str,
        counts: &Counts,
        shutdown: &AtomicBool,
    ) {
        while !shutdown.load(Ordering::SeqCst) {
            let Ok((mut stream, _)) = listener.accept() else {
                break;
            };
            if shutdown.load(Ordering::SeqCst) {
                break;
            }
            let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
            let request = read_http(&mut stream);
            if !request.contains("HTTP/") {
                continue;
            }
            let line = request.lines().next().unwrap_or("");
            if line.contains("/handshake/challenge") {
                counts.challenges.fetch_add(1, Ordering::SeqCst);
                let nonce = request
                    .split("\"nonce\":\"")
                    .nth(1)
                    .and_then(|rest| rest.split('"').next())
                    .unwrap_or("");
                let proof = hmac_sha256_hex(CLI_TOKEN.as_bytes(), &b64url_decode(nonce));
                write_json(&mut stream, "200 OK", &format!("{{\"proof\":\"{proof}\"}}"));
            } else if line.contains("/api/runtime/handshake") {
                counts.handshakes.fetch_add(1, Ordering::SeqCst);
                thread::sleep(SLOW_HANDSHAKE);
                write_json(&mut stream, "200 OK", handshake_body);
            } else if line.contains("/api/runtime/config") {
                counts.configs.fetch_add(1, Ordering::SeqCst);
                write_json(&mut stream, "200 OK", config_body);
            } else if line.contains("/api/config/effective") {
                counts.effective.fetch_add(1, Ordering::SeqCst);
                thread::sleep(SLOW_EFFECTIVE_CONFIG);
                write_json(
                    &mut stream,
                    "200 OK",
                    r#"{"revision":1,"config":{"ai.embeddings.dim":"768"}}"#,
                );
            } else if line.starts_with("GET /collections/") {
                counts.qdrant.fetch_add(1, Ordering::SeqCst);
                write_json(&mut stream, "404 Not Found", "{\"status\":\"not found\"}");
            } else if line.starts_with("PUT /collections/") {
                counts.qdrant.fetch_add(1, Ordering::SeqCst);
                write_json(&mut stream, "200 OK", "{\"result\":true}");
            } else {
                write_json(
                    &mut stream,
                    "500 Internal Server Error",
                    "{\"unexpected\":true}",
                );
            }
        }
    }

    fn write_json(stream: &mut impl Write, status: &str, body: &str) {
        let message = format!(
            "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(message.as_bytes());
    }

    fn read_http(stream: &mut impl Read) -> String {
        let mut buf = Vec::new();
        let mut tmp = [0u8; 2048];
        let started = std::time::Instant::now();
        loop {
            if started.elapsed() > Duration::from_secs(2) {
                break;
            }
            match stream.read(&mut tmp) {
                Ok(0) => break,
                Ok(n) => {
                    buf.extend_from_slice(&tmp[..n]);
                    if let Some(header_end) = find_header_end(&buf) {
                        let length = content_length(&buf[..header_end]);
                        if buf.len() >= header_end + 4 + length {
                            break;
                        }
                    }
                    if buf.len() > 256 * 1024 {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        String::from_utf8_lossy(&buf).into_owned()
    }

    fn find_header_end(buf: &[u8]) -> Option<usize> {
        buf.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn content_length(headers: &[u8]) -> usize {
        let text = String::from_utf8_lossy(headers);
        text.lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0)
    }

    fn hmac_sha256_hex(key: &[u8], data: &[u8]) -> String {
        const BLOCK: usize = 64;
        let mut key_block = [0u8; BLOCK];
        if key.len() > BLOCK {
            let digest = Sha256::digest(key);
            key_block[..digest.len()].copy_from_slice(&digest);
        } else {
            key_block[..key.len()].copy_from_slice(key);
        }
        let mut ipad = [0u8; BLOCK];
        let mut opad = [0u8; BLOCK];
        for (index, byte) in key_block.iter().enumerate() {
            ipad[index] = byte ^ 0x36;
            opad[index] = byte ^ 0x5c;
        }
        let mut inner = Sha256::new();
        inner.update(ipad);
        inner.update(data);
        let inner_hash = inner.finalize();
        let mut outer = Sha256::new();
        outer.update(opad);
        outer.update(inner_hash);
        hex_encode(&outer.finalize())
    }

    fn hex_encode(bytes: &[u8]) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut out = String::with_capacity(bytes.len() * 2);
        for byte in bytes {
            out.push(HEX[(byte >> 4) as usize] as char);
            out.push(HEX[(byte & 0x0f) as usize] as char);
        }
        out
    }

    fn b64url_decode(input: &str) -> Vec<u8> {
        fn value(byte: u8) -> u8 {
            match byte {
                b'A'..=b'Z' => byte - b'A',
                b'a'..=b'z' => byte - b'a' + 26,
                b'0'..=b'9' => byte - b'0' + 52,
                b'-' => 62,
                b'_' => 63,
                _ => 0,
            }
        }
        let bytes: Vec<u8> = input.bytes().filter(|byte| *byte != b'=').collect();
        let mut out = Vec::with_capacity(bytes.len().saturating_mul(3) / 4);
        let mut index = 0;
        while index + 4 <= bytes.len() {
            let chunk = (u32::from(value(bytes[index])) << 18)
                | (u32::from(value(bytes[index + 1])) << 12)
                | (u32::from(value(bytes[index + 2])) << 6)
                | u32::from(value(bytes[index + 3]));
            out.push((chunk >> 16) as u8);
            out.push((chunk >> 8) as u8);
            out.push(chunk as u8);
            index += 4;
        }
        let rest = bytes.len() - index;
        if rest == 2 {
            let chunk =
                (u32::from(value(bytes[index])) << 18) | (u32::from(value(bytes[index + 1])) << 12);
            out.push((chunk >> 16) as u8);
        } else if rest == 3 {
            let chunk = (u32::from(value(bytes[index])) << 18)
                | (u32::from(value(bytes[index + 1])) << 12)
                | (u32::from(value(bytes[index + 2])) << 6);
            out.push((chunk >> 16) as u8);
            out.push((chunk >> 8) as u8);
        }
        out
    }

    fn uuid_param(id: &str) -> uuid::Uuid {
        uuid::Uuid::parse_str(id).expect("fixture id is a uuid")
    }

    fn local_machine_uuid() -> uuid::Uuid {
        uuid_param(&gobby_core::machine::read_local_machine_id().expect("read local machine id"))
    }

    fn isolated_home(root: &Path) -> PathBuf {
        let home = root.join(".no-daemon-home");
        std::fs::create_dir_all(&home).expect("create isolated Gobby home");
        std::fs::write(home.join("machine_id"), local_machine_uuid().to_string())
            .expect("write isolated machine id");
        std::fs::write(home.join("local_cli_token"), CLI_TOKEN).expect("write cli token");
        home
    }

    fn seed_pending_file(conn: &mut Client, project_id: &str, file_path: &str) {
        let project_uuid = uuid_param(project_id);
        if file_path == FILE_PATHS[0] {
            conn.execute(
                "INSERT INTO code_indexed_projects (id) VALUES ($1)",
                &[&project_uuid],
            )
            .expect("insert indexed project");
            conn.execute(
                "INSERT INTO code_indexed_project_states
                    (machine_id, project_id, root_path, total_files, total_symbols,
                     last_indexed_at, index_duration_ms)
                 VALUES ($1, $2, $3, 4, 0, NOW(), 0)",
                &[
                    &local_machine_uuid(),
                    &project_uuid,
                    &"/tmp/concurrent-vector-grant",
                ],
            )
            .expect("insert indexed project state");
        }
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
                 graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 0, 18, false, false, NULL, NOW())",
            &[&row_uuid(project_id, file_path), &project_uuid, &file_path],
        )
        .expect("insert pending indexed file");
        conn.execute(
            "INSERT INTO code_indexed_file_states
                (machine_id, project_id, file_path, content_hash)
             VALUES ($1, $2, $3, 'hash-1')",
            &[&local_machine_uuid(), &project_uuid, &file_path],
        )
        .expect("insert indexed file state");
    }

    fn row_uuid(project_id: &str, label: &str) -> uuid::Uuid {
        uuid::Uuid::new_v5(
            &CODE_INDEX_UUID_NAMESPACE,
            format!("{project_id}:{label}").as_bytes(),
        )
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = Client::connect(&self.database_url, NoTls) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }

    fn cleanup_project(conn: &mut Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = uuid_param(project_id);
        conn.execute(
            "DELETE FROM code_indexed_file_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_project_states WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_symbols WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?;
        Ok(())
    }
}
