//! Command dispatch for the `gterm` binary.

use std::ffi::OsString;

pub fn main_entry() {
    let mut args = std::env::args_os().skip(1);
    match args.next().as_deref().and_then(std::ffi::OsStr::to_str) {
        Some("host") => run_host(),
        #[cfg(unix)]
        Some("gate") => {
            if args.next().as_deref() != Some(std::ffi::OsStr::new("--")) {
                usage();
            }
            crate::host::gate::run(args.collect::<Vec<OsString>>());
        }
        _ => usage(),
    }
}

fn run_host() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    if let Err(error) = runtime.block_on(crate::host::run()) {
        eprintln!("gterm host failed: {error}");
        std::process::exit(1);
    }
}

fn usage() -> ! {
    eprintln!("usage: gterm host [--socket-dir PATH] | gterm gate -- <argv>");
    std::process::exit(2);
}
