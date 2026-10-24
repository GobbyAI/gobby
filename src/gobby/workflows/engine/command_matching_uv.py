"""Program argv for ``uv run``, ``uvx`` and Python interpreter stages."""

import re

from gobby.hooks._shell_execution_words import _unwrap, option_word_count
from gobby.hooks.code_navigation import shell_command_name
from gobby.hooks.provider_launch_guard import _python_name

# uv 0.12 global options, accepted before the subcommand and after it.
_UV_GLOBAL_VALUE_OPTIONS = frozenset(
    {
        "--cache-dir",
        "--color",
        "--allow-insecure-host",
        "--directory",
        "--project",
        "--config-file",
        "--python-preference",
        "--preview-features",
        "--python-fetch",
    }
)
# Every value-taking option `uv run --help` lists.
_UV_RUN_VALUE_OPTIONS = _UV_GLOBAL_VALUE_OPTIONS | frozenset(
    {
        "--extra",
        "--no-extra",
        "--group",
        "--no-group",
        "--only-group",
        "--no-editable-package",
        "--env-file",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--package",
        "--python-platform",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    }
)
# Every value-taking option `uvx --help` (alias `uv tool run`) lists.
_UV_TOOL_RUN_VALUE_OPTIONS = _UV_GLOBAL_VALUE_OPTIONS | frozenset(
    {
        "--from",
        "-w",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-c",
        "--constraints",
        "-b",
        "--build-constraints",
        "--overrides",
        "--env-file",
        "--python-platform",
        "--torch-backend",
        "--index",
        "--default-index",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--index-strategy",
        "--keyring-provider",
        "-P",
        "--upgrade-package",
        "--upgrade-group",
        "--resolution",
        "--prerelease",
        "--prerelease-package",
        "--fork-strategy",
        "--exclude-newer",
        "--exclude-newer-package",
        "--no-sources-package",
        "--reinstall-package",
        "--link-mode",
        "-C",
        "--config-setting",
        "--config-settings-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-binary-package",
        "--refresh-package",
        "-p",
        "--python",
    }
)
# A tool command may carry its package's version or extras (`ruff@0.6`, `ruff==0.6`).
_TOOL_SPEC_SUFFIX = re.compile(r"[@\[<>=!~].*", re.DOTALL)


def _uv_run_argvs(name: str, args: list[str]) -> list[list[str]] | None:
    """The argvs ``uv run``, ``uv tool run`` or ``uvx`` executes; None for other commands.

    ``uv run -m X args`` runs module X as ``python -m X args`` would, so it yields
    both readings: rules match the module's own command (``pip install``) or the
    ``python -m`` form.
    """
    if name == "uvx":
        return [_tool_argv(_after_options(args, _UV_TOOL_RUN_VALUE_OPTIONS))]
    if name != "uv":
        return None
    subcommand = _after_options(args, _UV_GLOBAL_VALUE_OPTIONS)
    if subcommand[:1] == ["run"]:
        argv = _after_options(subcommand[1:], _UV_RUN_VALUE_OPTIONS)
        options = subcommand[1 : len(subcommand) - len(argv)]
        if argv and any(_selects_module(option) for option in options):
            return [argv, ["python", "-m", *argv]]
        return [argv]
    if subcommand[:1] == ["tool"]:
        tool = _after_options(subcommand[1:], _UV_GLOBAL_VALUE_OPTIONS)
        if tool[:1] in (["run"], ["uvx"]):
            return [_tool_argv(_after_options(tool[1:], _UV_TOOL_RUN_VALUE_OPTIONS))]
    return None


def _selects_module(option: str) -> bool:
    """Whether a ``uv run`` option is ``-m``, alone or in a short cluster (``-qm``)."""
    if option == "--module":
        return True
    if option.startswith("--") or not option.startswith("-"):
        return False
    for letter in option[1:]:
        if letter == "m":
            return True
        # A value option takes the rest of the cluster as its value.
        if "-" + letter in _UV_RUN_VALUE_OPTIONS:
            return False
    return False


def _tool_argv(words: list[str]) -> list[str]:
    """Drop a package spec suffix from the tool command uv runs."""
    if not words:
        return words
    return [_TOOL_SPEC_SUFFIX.sub("", words[0]), *words[1:]]


def _after_options(
    words: list[str], value_options: frozenset[str], *, infer_long_options: bool = False
) -> list[str]:
    """Drop leading options (and a value option's operand) through ``--``."""
    index = 0
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "--":
            return words[index + 1 :]
        index += option_word_count(
            words[index], value_options, infer_long_options=infer_long_options
        )
    return words[index:]


def _python_stage(words: list[str]) -> bool:
    """Whether a stage's program is a Python interpreter, looking through ``uv run``."""
    unwrapped = _unwrap(words)
    argvs = _uv_run_argvs(shell_command_name(unwrapped[0]), unwrapped[1:]) if unwrapped else None
    return any(_python_name(argv[0]) for argv in (argvs or [unwrapped]) if argv)
