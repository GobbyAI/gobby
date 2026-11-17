use super::*;
use std::collections::BTreeMap;
use std::ffi::OsString;

#[test]
fn host_command_keeps_the_daemon_environment() {
    let root = tempfile::tempdir().expect("host fixture");
    for with_home in [false, true] {
        let options = HostOptions {
            socket_dir: root.path().join("sockets"),
            binary_path: root.path().join("gterm-fixture"),
            gobby_home: with_home.then(|| root.path().join("home")),
            spawn_args: vec!["fixture-extra".to_owned()],
            ..HostOptions::default()
        };
        let command = host_command(
            &options,
            tempfile::tempfile().expect("stdout"),
            tempfile::tempfile().expect("stderr"),
        );
        let environment: BTreeMap<_, _> = command
            .as_std()
            .get_envs()
            .map(|(key, value)| (key.to_owned(), value.map(|value| value.to_owned())))
            .collect();
        let mut expected = BTreeMap::from([(
            OsString::from("GTERM_LOG_FILE"),
            Some(
                options
                    .socket_dir
                    .join("logs")
                    .join("gterm.log")
                    .into_os_string(),
            ),
        )]);
        if let Some(home) = &options.gobby_home {
            expected.insert(
                OsString::from("GOBBY_HOME"),
                Some(home.as_os_str().to_owned()),
            );
        }
        assert_eq!(
            environment, expected,
            "only host configuration overrides are added"
        );
        assert_eq!(
            command.as_std().get_program(),
            options.binary_path.as_os_str()
        );
        let args: Vec<_> = command
            .as_std()
            .get_args()
            .map(|arg| arg.to_owned())
            .collect();
        assert_eq!(
            args,
            vec![
                OsString::from("host"),
                OsString::from("--socket-dir"),
                options.socket_dir.as_os_str().to_owned(),
                OsString::from("fixture-extra"),
            ]
        );
    }
}
