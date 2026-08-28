//! gterm host binary.

fn main() {
    let command = std::env::args().nth(1);
    match command.as_deref() {
        Some("host") => {
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .expect("tokio runtime");
            if let Err(error) = runtime.block_on(gobby_terminal::host::run()) {
                eprintln!("gterm host failed: {error}");
                std::process::exit(1);
            }
        }
        _ => {
            eprintln!("usage: gterm host [--socket-dir PATH]");
            std::process::exit(2);
        }
    }
}
