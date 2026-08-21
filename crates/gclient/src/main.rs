fn main() {
    if let Err(err) = gobby_client::startup::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
