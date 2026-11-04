"""Subcommands that answer a literal provider help request, by provider.

A word maps to its child subcommands, and an empty mapping is a leaf. Only a listed
word is a subcommand: a word missing from its node ends the path, so a prompt or an
option never reads as help. A trailing `help` answers a group's help, but it is a
prompt on a leaf and on a path listed in POSITIONAL_GROUPS.
"""

type _Subcommands = dict[str, _Subcommands]

_MCP: _Subcommands = {"add": {}, "get": {}, "list": {}, "remove": {}}
_EXEC: _Subcommands = {"fork": {}, "resume": {}, "review": {}}

SUBCOMMANDS: dict[str, _Subcommands] = {
    "codex": {
        "agents": {},
        "exec": _EXEC,
        "e": _EXEC,
        "review": {},
        "login": {"status": {}},
        "logout": {},
        "mcp": {**_MCP, "login": {}, "logout": {}},
        "plugin": {
            "add": {},
            "list": {},
            "marketplace": {"add": {}, "list": {}, "remove": {}, "upgrade": {}},
            "remove": {},
        },
        "app-server": {
            "daemon": {
                "bootstrap": {},
                "disable-remote-control": {},
                "enable-remote-control": {},
                "restart": {},
                "start": {},
                "stop": {},
                "update": {},
                "version": {},
            },
            "generate-json-schema": {},
            "generate-ts": {},
            "proxy": {},
        },
        "remote-control": {"pair": {}, "start": {}, "stop": {}},
        "app": {},
        "completion": {},
        "update": {},
        "doctor": {},
        "sandbox": {"uninstall": {}},
        "debug": {
            "app-server": {"send-message-v2": {}},
            "models": {},
            "prompt-input": {},
            "trace-reduce": {},
        },
        "apply": {},
        "a": {},
        "resume": {},
        "queue": {},
        "archive": {},
        "delete": {},
        "migrate-rollouts": {},
        "migrate-nfs-db": {},
        "unarchive": {},
        "fork": {},
        "cloud": {"apply": {}, "diff": {}, "exec": {}, "list": {}, "status": {}},
        "exec-server": {"forward": {}},
        "features": {"disable": {}, "enable": {}, "list": {}},
    },
    "claude": {
        "auth": {"login": {}, "logout": {}, "status": {}},
        "mcp": {
            **_MCP,
            "add-from-claude-desktop": {},
            "add-json": {},
            "reset-project-choices": {},
            "serve": {},
        },
        "plugin": {
            "disable": {},
            "enable": {},
            "install": {},
            "list": {},
            "marketplace": {"add": {}, "list": {}, "remove": {}, "update": {}},
            "uninstall": {},
            "update": {},
            "validate": {},
        },
        "doctor": {},
        "install": {},
        "update": {},
    },
    "droid": {
        "exec": {},
        "daemon": {},
        "search": {},
        "update": {},
        "mcp": _MCP,
        "plugin": {},
        "computer": {},
    },
    "grok": {
        "agent": {},
        "login": {},
        "logout": {},
        "mcp": _MCP,
        "models": {},
        "sessions": {},
        "doctor": {},
        "version": {},
    },
    "agy": {
        "agent": {},
        "agents": {},
        "mcp": _MCP,
        "models": {},
        "plugin": {},
        "plugins": {},
        "update": {},
    },
}

# Paths that also take a positional prompt or command (codex exec, its alias e, and
# sandbox). A trailing `help` after one of these is that prompt, not a help request.
POSITIONAL_GROUPS: frozenset[tuple[str, ...]] = frozenset(
    {("codex", "exec"), ("codex", "e"), ("codex", "sandbox")}
)
