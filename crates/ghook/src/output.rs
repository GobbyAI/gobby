use std::{fmt::Arguments, io, io::Write as _};

pub(crate) fn stdout(args: Arguments<'_>) -> io::Result<()> {
    let mut stdout = std::io::stdout().lock();
    stdout.write_fmt(args)?;
    stdout.flush()
}

pub(crate) fn stdout_bytes(bytes: &[u8]) -> io::Result<()> {
    let mut stdout = std::io::stdout().lock();
    stdout.write_all(bytes)?;
    stdout.flush()
}

pub(crate) fn stderr(args: Arguments<'_>) {
    let _ = std::io::stderr().lock().write_fmt(args);
}
