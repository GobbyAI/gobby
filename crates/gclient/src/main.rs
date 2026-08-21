fn main() {
    if let Err(err) = gobby_client::views::run() {
        eprintln!("{err}");
        std::process::exit(1);
    }
}
