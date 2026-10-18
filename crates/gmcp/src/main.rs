//! `gmcp`: serves Gobby's MCP tool surface over stdio.

use std::ffi::OsString;
use std::process::ExitCode;
use std::sync::Arc;

use gobby_mcp::daemon::Dialer;
use gobby_mcp::server::WrapperServer;
use gobby_mcp::transport::CompleteResults;
use rmcp::RoleServer;
use rmcp::serve_server;
use rmcp::transport::async_rw::AsyncRwTransport;
use rmcp::transport::io::stdio;
use tokio::sync::Notify;

fn main() -> ExitCode {
    let arguments: Vec<OsString> = std::env::args_os().skip(1).collect();
    match arguments.as_slice() {
        [] => {}
        [argument] if argument.to_str() == Some("--version") => {
            println!("gmcp {}", env!("CARGO_PKG_VERSION"));
            return ExitCode::SUCCESS;
        }
        _ => {
            eprintln!("usage: gmcp");
            return ExitCode::from(2);
        }
    }
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("gmcp: {error}");
            return ExitCode::FAILURE;
        }
    };
    let served = runtime.block_on(async {
        let dialer = Dialer::new()?;
        let (read, write) = stdio();
        let ended = Arc::new(Notify::new());
        let transport = CompleteResults::new(
            AsyncRwTransport::<RoleServer, _, _>::new_server(read, write),
            Arc::clone(&ended),
        );
        let service = serve_server(WrapperServer::new(dialer), transport).await?;
        // rmcp waits up to 5 s for in-flight calls once input ends; exit as soon as input ends.
        tokio::select! {
            result = service.waiting() => {
                result?;
            }
            () = ended.notified() => {}
        }
        anyhow::Ok(())
    });
    runtime.shutdown_background();
    match served {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("gmcp: {error:#}");
            ExitCode::FAILURE
        }
    }
}
