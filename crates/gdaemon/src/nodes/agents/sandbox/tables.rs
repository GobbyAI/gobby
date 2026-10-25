//! Static policy tables, mirrored from the Python oracle in the same order: grant
//! and deny lists are emitted in table order, so reordering changes the settings bytes.

/// `_PROVIDER_DOMAINS`.
pub(super) fn provider_domains(provider: &str) -> &'static [&'static str] {
    match provider {
        "claude" => &["api.anthropic.com", "*.anthropic.com"],
        "codex" => &[
            "api.openai.com",
            "*.openai.com",
            "chatgpt.com",
            "*.chatgpt.com",
        ],
        "gemini" => &["generativelanguage.googleapis.com", "oauth2.googleapis.com"],
        "droid" => &["api.factory.ai", "*.factory.ai"],
        "grok" => &["api.x.ai", "*.x.ai", "grok.com", "*.grok.com"],
        "agy" => &[
            "daily-cloudcode-pa.googleapis.com",
            "www.googleapis.com",
            "oauth2.googleapis.com",
            "accounts.google.com",
            "play.googleapis.com",
            "playwright.azureedge.net",
            "playwright-akamai.azureedge.net",
            "playwright-verizon.azureedge.net",
            "*.googleusercontent.com",
        ],
        _ => &[],
    }
}

/// `_PROVIDER_AUTH_DOMAINS`.
pub(super) fn provider_auth_domains(provider: &str) -> &'static [&'static str] {
    match provider {
        "droid" => &["api.workos.com"],
        _ => &[],
    }
}

/// `_PROVIDER_AUTH_PATHS`, in insertion order.
pub(super) const AUTH_PATHS: &[(&str, &[&str])] = &[
    (
        "claude",
        &["~/.claude", "~/Library/Caches/claude-cli-nodejs"],
    ),
    ("codex", &["~/.codex"]),
    ("gemini", &["~/.gemini", "~/.config/gemini"]),
    ("droid", &["~/.factory"]),
    ("grok", &["~/.grok"]),
    (
        "agy",
        &[
            "~/.gemini/antigravity-cli",
            "~/Library/Caches/ms-playwright-go",
        ],
    ),
];

/// `_PROVIDER_AUTH_READ_ONLY_PATHS`, in insertion order.
pub(super) const AUTH_READ_ONLY_PATHS: &[(&str, &[&str])] = &[
    (
        "claude",
        &["~/.claude.json", "~/Library/Keychains/login.keychain-db"],
    ),
    (
        "agy",
        &[
            "~/.gemini/config",
            "~/.gemini/antigravity-ide/installation_id",
            "~/Library/Keychains/login.keychain-db",
        ],
    ),
    ("droid", &["~/Library/Keychains/login.keychain-db"]),
];

/// `_PROVIDER_CREDENTIAL_ENV`. The agy row is `denied_ambient_keys("agy")`, sorted.
pub(super) fn credential_env(provider: &str) -> &'static [&'static str] {
    match provider {
        "claude" => &["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
        "codex" => &["OPENAI_API_KEY"],
        "gemini" => &["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "droid" => &["FACTORY_API_KEY"],
        "grok" => &["XAI_API_KEY"],
        "agy" => &[
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ],
        _ => &[],
    }
}

/// `_SRT_DEFAULT_WRITE_PATHS` (srt_settings.py).
pub(super) const SRT_DEFAULT_WRITE_PATHS: &[&str] = &[
    "/dev/stdout",
    "/dev/stderr",
    "/dev/null",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/autofs_nowait",
    "/tmp/claude",
    "/private/tmp/claude",
    "~/.npm/_logs",
    "~/.claude/debug",
];

/// `_TOOLCHAIN_READ_ROOTS`, filtered to existing home roots at resolve time.
pub(super) const TOOLCHAIN_READ_ROOTS: &[&str] = &[
    ".rustup",
    ".pyenv",
    ".nvm",
    ".fnm",
    ".volta",
    ".bun",
    ".deno",
    ".sdkman",
    ".jenv",
    ".konan",
    "Library/Java",
    ".rbenv",
    ".rvm",
    ".dotnet",
    ".phpenv",
    "flutter",
    ".swiftly",
    "sdk",
    ".asdf",
    ".mise",
    ".local/share/mise",
    ".local/bin",
];

/// `_TOOLCHAIN_SHARED_READ_ROOTS`, filtered to existing home roots at resolve time.
pub(super) const TOOLCHAIN_SHARED_READ_ROOTS: &[&str] = &[
    ".cargo/bin",
    ".cargo/registry",
    ".cargo/git",
    ".cargo/.package-cache",
    ".cargo/config",
    ".cargo/config.toml",
    ".cache/uv",
    "Library/Caches/uv",
    ".cache/pip",
    "Library/Caches/pip",
    ".npm/_cacache",
    ".npm/_npx",
    ".npm/_logs",
    ".cache/npm",
    ".pnpm-store",
    "Library/pnpm",
    ".yarn",
    ".cache/yarn",
    ".config/yarn",
    "go",
    ".cache/go-build",
    "Library/Caches/go-build",
    ".gradle/caches",
    ".gradle/wrapper",
    ".gradle/jdks",
    ".gradle/daemon",
    ".gradle/native",
    ".gradle/init.d",
    ".gradle/init.gradle",
    ".gradle/init.gradle.kts",
    ".m2/repository",
    ".m2/wrapper",
    ".m2/toolchains.xml",
    ".ivy2/cache",
    ".ivy2/jars",
    ".ivy2/local",
    ".sbt/boot",
    ".sbt/1.0",
    ".sbt/preloaded",
    ".sbt/repositories",
    ".cache/coursier",
    "Library/Caches/Coursier",
    ".gem/ruby",
    ".gem/specs",
    ".gem/cache",
    ".gem/extensions",
    ".bundle/cache",
    ".bundle/plugin",
    ".nuget/packages",
    ".nuget/plugins",
    ".nuget/fallbackpackages",
    ".local/share/NuGet",
    ".composer/cache",
    ".composer/vendor",
    ".composer/config.json",
    ".composer/composer.json",
    ".config/composer/cache",
    ".config/composer/vendor",
    ".config/composer/config.json",
    ".config/composer/composer.json",
    ".cache/composer",
    ".pub-cache/hosted",
    ".pub-cache/git",
    ".pub-cache/bin",
    ".hex/packages",
    ".hex/cache.ets",
    ".mix",
    ".luarocks",
    ".cache/luarocks",
    ".cache/ccache",
    "Library/Caches/ccache",
    ".cache/clangd",
    ".swiftpm",
    "Library/Caches/org.swift.swiftpm",
];

/// `_TOOLCHAIN_CREDENTIAL_PATHS`.
pub(super) const TOOLCHAIN_CREDENTIAL_PATHS: &[&str] = &[
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".npm/_auth",
    ".gradle/gradle.properties",
    ".m2/settings.xml",
    ".m2/settings-security.xml",
    ".gem/credentials",
    ".bundle/config",
    ".sbt/.credentials",
    ".ivy2/.credentials",
    ".nuget/NuGet/NuGet.Config",
    ".composer/auth.json",
    ".config/composer/auth.json",
    ".pub-cache/credentials.json",
    ".hex/hex.config",
];

/// `user_credential_read_paths` (sandbox_control_paths.py).
pub(super) const USER_CREDENTIAL_READ_PATHS: &[&str] = &[
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.netrc",
    "~/.docker/config.json",
    "~/Library/Keychains",
];

/// `GIT_DOMAINS` (sandbox_domains.py).
pub(super) const GIT_DOMAINS: &[&str] = &[
    "github.com",
    "*.github.com",
    "gitlab.com",
    "*.gitlab.com",
    "bitbucket.org",
    "*.bitbucket.org",
];

/// `PACKAGE_REGISTRY_DOMAINS` (sandbox_domains.py).
pub(super) const PACKAGE_REGISTRY_DOMAINS: &[&str] = &[
    "registry.npmjs.org",
    "*.npmjs.org",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "static.crates.io",
    "index.crates.io",
    "static.rust-lang.org",
    "proxy.golang.org",
    "sum.golang.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "rubygems.org",
    "api.nuget.org",
    "repo.packagist.org",
    "pub.dev",
    "repo.hex.pm",
    "luarocks.org",
    "deps.files.ghostty.org",
];

/// `GROK_RUNTIME_DIRECTORIES`.
pub(super) const GROK_RUNTIME_DIRECTORIES: &[&str] = &[
    "sessions",
    "memory",
    "logs",
    "crash",
    "trace-exports",
    "worktrees",
];

/// `GROK_CONTROL_FILES`.
const GROK_CONTROL_FILES: &[&str] = &[
    "hooks",
    "hooks-paths",
    "config.toml",
    "managed_config.toml",
    "requirements.toml",
    "trusted_folders.toml",
    "trusted-hook-projects",
    "trusted-plugins",
    "sandbox.toml",
    "bin",
];

/// `CLAUDE_CONTROL_FILES`.
const CLAUDE_CONTROL_FILES: &[&str] = &[
    "settings.json",
    "settings.local.json",
    "agents",
    "skills",
    "commands",
    "plugins/cache",
    "plugins/marketplaces",
    "plugins/synced",
    "plugins/installed_plugins.json",
    "plugins/known_marketplaces.json",
];

/// `_PROVIDER_CONTROLS`, in insertion order.
pub(super) const PROVIDER_CONTROLS: &[(&str, &[&str])] = &[
    (".claude", CLAUDE_CONTROL_FILES),
    (
        ".codex",
        &["hooks.json", "config.toml", "rules", "plugins/cache"],
    ),
    (
        ".factory",
        &[
            "hooks.json",
            "settings.json",
            "settings.local.json",
            "mcp.json",
            "droids",
            "commands",
            "plugins/cache",
            "plugins/marketplaces",
            "plugins/installed_plugins.json",
            "plugins/known_marketplaces.json",
        ],
    ),
    (".grok", GROK_CONTROL_FILES),
    (".cursor", &["hooks.json"]),
    (
        ".gemini",
        &[
            "settings.json",
            "trustedFolders.json",
            "config/hooks.json",
            "agents",
            "commands",
            "policies",
            "extensions",
        ],
    ),
    (".config/gemini", &["settings.json", "trustedFolders.json"]),
    (".agents/plugins", &["marketplace.json"]),
    (".claude-plugin", &["marketplace.json"]),
];

/// `_CONFIG_HOME_ENV`: the variable names a provider's config home and its control directory.
pub(super) const CONFIG_HOME_ENV: &[(&str, &str)] =
    &[("CLAUDE_CONFIG_DIR", ".claude"), ("CODEX_HOME", ".codex")];

/// Look up a provider row in one of the `(provider, paths)` tables.
pub(super) fn lookup(
    table: &'static [(&'static str, &'static [&'static str])],
    provider: &str,
) -> &'static [&'static str] {
    table
        .iter()
        .find(|(name, _)| *name == provider)
        .map_or(&[], |(_, paths)| *paths)
}
