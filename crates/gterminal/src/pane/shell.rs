//! Shell launch policy for a pane PTY.

use std::io;
use std::path::Path;

use portable_pty::CommandBuilder;

const PANE_TERM: &str = "xterm-256color";
const PANE_COLORTERM: &str = "truecolor";

pub(crate) fn apply_pane_terminal_env(cmd: &mut CommandBuilder) {
    cmd.env("TERM", PANE_TERM);
    cmd.env("COLORTERM", PANE_COLORTERM);
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PaneLaunchEnv {
    extra: Vec<(String, String)>,
}

impl PaneLaunchEnv {
    pub fn from_extra(extra: Vec<(String, String)>) -> Self {
        Self { extra }
    }
}

pub(crate) fn apply_pane_launch_env(cmd: &mut CommandBuilder, launch_env: &PaneLaunchEnv) {
    cmd.env_remove("CODEX_THREAD_ID");
    for (key, value) in &launch_env.extra {
        cmd.env(key, value);
    }
    cmd.env(crate::GTERM_ENV_VAR, crate::GTERM_ENV_VALUE);
    crate::platform::apply_pane_runtime_marker(cmd);
}

fn pane_shell(configured_shell: &str) -> String {
    pane_shell_from(configured_shell, std::env::var("SHELL").ok())
}

fn pane_shell_from(configured_shell: &str, env_shell: Option<String>) -> String {
    let configured_shell = configured_shell.trim();
    if !configured_shell.is_empty() {
        return configured_shell.to_string();
    }

    #[cfg(windows)]
    {
        let _ = env_shell;
        default_pane_shell()
    }

    #[cfg(not(windows))]
    env_shell
        .map(|shell| shell.trim().to_string())
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(default_pane_shell)
}

#[cfg(windows)]
fn default_pane_shell() -> String {
    "powershell.exe".into()
}

#[cfg(not(windows))]
fn default_pane_shell() -> String {
    "/bin/sh".into()
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ShellMode {
    #[default]
    Auto,
    Login,
    NonLogin,
}

#[derive(Clone, Copy)]
pub struct PaneShellConfig<'a> {
    pub default_shell: &'a str,
    pub mode: ShellMode,
}

impl<'a> PaneShellConfig<'a> {
    pub fn new(default_shell: &'a str, mode: ShellMode) -> Self {
        Self {
            default_shell,
            mode,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum ShellLaunchTarget {
    Windows,
    Macos,
    OtherUnix,
}

impl ShellLaunchTarget {
    fn current() -> Self {
        if cfg!(windows) {
            Self::Windows
        } else if cfg!(target_os = "macos") {
            Self::Macos
        } else {
            Self::OtherUnix
        }
    }
}

fn shell_mode_uses_login_shell(mode: ShellMode, target: ShellLaunchTarget) -> bool {
    match mode {
        ShellMode::Auto => target == ShellLaunchTarget::Macos,
        ShellMode::Login => true,
        ShellMode::NonLogin => false,
    }
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = path.metadata() else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn resolve_shell_for_login_mode(shell: &str) -> io::Result<String> {
    if shell.contains(std::path::MAIN_SEPARATOR) {
        let path = Path::new(shell);
        return is_executable_file(path)
            .then(|| shell.to_string())
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("login shell {shell:?} is not executable"),
                )
            });
    }

    std::env::var_os("PATH")
        .and_then(|path| {
            std::env::split_paths(&path)
                .map(|dir| dir.join(shell))
                .find(|candidate| is_executable_file(candidate))
        })
        .and_then(|path| path.into_os_string().into_string().ok())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("login shell {shell:?} was not found on PATH"),
            )
        })
}

pub(crate) const WINDOWS_POWERSHELL_SHELL_INTEGRATION_COMMAND: &str = r"if ($null -eq $global:__GtermOriginalPrompt) { $global:__GtermOriginalPrompt = $function:prompt; function global:prompt { $out = @(& $global:__GtermOriginalPrompt) -join ' '; $loc = $ExecutionContext.SessionState.Path.CurrentLocation; if ($loc.Provider.Name -eq 'FileSystem') { $esc = [string][char]27; $out += $esc + ']9;9;' + $loc.ProviderPath + $esc + '\' }; $out } }";

fn pane_shell_command_builder_for_target(
    shell_config: PaneShellConfig<'_>,
    target: ShellLaunchTarget,
) -> io::Result<CommandBuilder> {
    let shell = pane_shell(shell_config.default_shell);
    if shell_mode_uses_login_shell(shell_config.mode, target) {
        let mut cmd = CommandBuilder::new_default_prog();
        cmd.env("SHELL", resolve_shell_for_login_mode(&shell)?);
        Ok(cmd)
    } else {
        let mut cmd = CommandBuilder::new(&shell);
        if uses_windows_powershell_pane_shell_for_target(shell_config, target) {
            cmd.args([
                "-NoExit",
                "-Command",
                WINDOWS_POWERSHELL_SHELL_INTEGRATION_COMMAND,
            ]);
        }
        Ok(cmd)
    }
}

pub(crate) fn pane_shell_command_builder(
    shell_config: PaneShellConfig<'_>,
) -> io::Result<CommandBuilder> {
    pane_shell_command_builder_for_target(shell_config, ShellLaunchTarget::current())
}

pub(crate) fn uses_windows_powershell_pane_shell(shell_config: PaneShellConfig<'_>) -> bool {
    uses_windows_powershell_pane_shell_for_target(shell_config, ShellLaunchTarget::current())
}

fn uses_windows_powershell_pane_shell_for_target(
    shell_config: PaneShellConfig<'_>,
    target: ShellLaunchTarget,
) -> bool {
    target == ShellLaunchTarget::Windows
        && !shell_mode_uses_login_shell(shell_config.mode, target)
        && is_powershell_shell(&pane_shell(shell_config.default_shell))
}

fn is_powershell_shell(shell: &str) -> bool {
    let name = shell
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(shell)
        .to_ascii_lowercase();
    matches!(
        name.as_str(),
        "powershell" | "powershell.exe" | "pwsh" | "pwsh.exe"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pane_shell_prefers_configured_shell() {
        assert_eq!(
            pane_shell_from("/bin/zsh", Some("/bin/bash".into())),
            "/bin/zsh"
        );
    }

    #[test]
    fn pane_shell_falls_back_to_shell_env() {
        #[cfg(not(windows))]
        assert_eq!(pane_shell_from("", Some("/bin/zsh".into())), "/bin/zsh");
        #[cfg(windows)]
        assert_eq!(
            pane_shell_from("", Some("/bin/zsh".into())),
            "powershell.exe"
        );
    }

    #[test]
    fn pane_shell_ignores_empty_values() {
        assert_eq!(
            pane_shell_from("  ", Some("  ".into())),
            default_pane_shell()
        );
    }

    #[test]
    fn shell_mode_auto_uses_login_shell_only_on_macos() {
        assert!(shell_mode_uses_login_shell(
            ShellMode::Auto,
            ShellLaunchTarget::Macos
        ));
        assert!(!shell_mode_uses_login_shell(
            ShellMode::Auto,
            ShellLaunchTarget::OtherUnix
        ));
        assert!(!shell_mode_uses_login_shell(
            ShellMode::Auto,
            ShellLaunchTarget::Windows
        ));
        assert!(shell_mode_uses_login_shell(
            ShellMode::Login,
            ShellLaunchTarget::OtherUnix
        ));
        assert!(!shell_mode_uses_login_shell(
            ShellMode::NonLogin,
            ShellLaunchTarget::Macos
        ));
    }
}
