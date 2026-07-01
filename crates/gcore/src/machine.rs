//! Local machine identity helpers shared by standalone Gobby binaries.

use std::io::ErrorKind;
use std::path::Path;

#[derive(Debug, thiserror::Error)]
pub enum MachineIdError {
    #[error("cannot determine Gobby home")]
    HomeUnavailable,
    #[error("machine_id file is missing")]
    Missing,
    #[error("machine_id file is empty")]
    Empty,
    #[error("failed to read machine_id file")]
    Read(#[source] std::io::Error),
}

impl MachineIdError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::HomeUnavailable => "machine_id_home_unavailable",
            Self::Missing => "machine_id_missing",
            Self::Empty => "machine_id_empty",
            Self::Read(_) => "machine_id_read_failed",
        }
    }
}

pub fn read_local_machine_id() -> Result<String, MachineIdError> {
    let gobby_home = crate::gobby_home().map_err(|_| MachineIdError::HomeUnavailable)?;
    read_machine_id_from_home(&gobby_home)
}

pub fn read_machine_id_from_home(gobby_home: &Path) -> Result<String, MachineIdError> {
    let machine_id_path = gobby_home.join("machine_id");
    let machine_id = std::fs::read_to_string(&machine_id_path).map_err(|error| {
        if error.kind() == ErrorKind::NotFound {
            MachineIdError::Missing
        } else {
            MachineIdError::Read(error)
        }
    })?;
    let machine_id = machine_id.trim().to_string();
    if machine_id.is_empty() {
        return Err(MachineIdError::Empty);
    }
    Ok(machine_id)
}

pub fn local_os_name() -> &'static str {
    std::env::consts::OS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_machine_id_trims_file_content() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), " machine-client \n").unwrap();

        assert_eq!(
            read_machine_id_from_home(dir.path()).unwrap(),
            "machine-client"
        );
    }

    #[test]
    fn read_machine_id_reports_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let err = read_machine_id_from_home(dir.path()).unwrap_err();

        assert_eq!(err.code(), "machine_id_missing");
    }

    #[test]
    fn read_machine_id_reports_empty_file() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("machine_id"), " \n").unwrap();
        let err = read_machine_id_from_home(dir.path()).unwrap_err();

        assert_eq!(err.code(), "machine_id_empty");
    }
}
