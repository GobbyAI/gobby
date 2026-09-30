fn main() {
    // Durable attribution: a panic must leave a reason in `gclient.log` even
    // though the terminal's alternate screen hides stderr (#23076). The exit
    // line for a clean or error return is recorded on the runtime path inside
    // `startup::run`, after logging is initialized and past the `--version`
    // and `--help` short-circuits that must not touch the log directory.
    gobby_client::logging::install_panic_hook();
    if let Err(err) = gobby_client::startup::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
