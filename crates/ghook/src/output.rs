use std::{fmt::Arguments, io::Write as _};

pub(crate) fn stdout(args: Arguments<'_>) {
    let _ = std::io::stdout().lock().write_fmt(args);
}

pub(crate) fn stderr(args: Arguments<'_>) {
    let _ = std::io::stderr().lock().write_fmt(args);
}
