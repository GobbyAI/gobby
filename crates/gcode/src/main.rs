mod cli;
mod dispatch;

fn main() -> std::process::ExitCode {
    dispatch::run_with_exit_code()
}
