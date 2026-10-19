//! `gmcp`: serves Gobby's MCP tool surface over stdio.

use std::process::ExitCode;
use std::sync::Arc;

use clap::Parser;
use gobby_mcp::daemon::Dialer;
use gobby_mcp::server::WrapperServer;
use gobby_mcp::transport::CompleteResults;
use rmcp::RoleServer;
use rmcp::serve_server;
use rmcp::transport::async_rw::AsyncRwTransport;
use rmcp::transport::io::stdio;
use tokio::sync::Notify;

#[derive(Parser)]
#[command(name = "gmcp", version, about)]
struct Cli {}

fn main() -> ExitCode {
    // `--help` and `--version` print and exit inside `parse`; any other argument is a usage error.
    Cli::parse();
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
