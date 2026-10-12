"""Literal sed option and operand parsing, independent of file access."""

from dataclasses import dataclass

from gobby.hooks._normalization_bindings import _raw_shell_word_is_literal
from gobby.hooks._normalization_shell import _contains_unexpanded_shell_reference

_LONG_OPTIONS = (
    "--expression",
    "--file",
    "--in-place",
    "--line-length",
    "--quiet",
    "--silent",
    "--debug",
    "--follow-symlinks",
    "--help",
    "--posix",
    "--regexp-extended",
    "--sandbox",
    "--separate",
    "--unbuffered",
    "--version",
)


@dataclass(frozen=True)
class _SedOperands:
    inputs: tuple[str, ...]
    script_files: tuple[str, ...]
    programs: tuple[str, ...]
    unresolved: bool
    inplace: bool


def _sed_file_operands(
    parts: list[str], raw_parts: tuple[str, ...] | None, *, bsd_inplace_suffix: bool = False
) -> _SedOperands:
    """Separate sed's program and script files from its input file operands."""
    positional: list[str] = []
    positional_indices: list[int] = []
    script_files: list[str] = []
    programs: list[str] = []
    explicit_script = False
    unresolved_script = False
    inplace = False
    after_options = False
    index = 1

    def unresolved(value: str, source_index: int) -> bool:
        return _contains_unexpanded_shell_reference(value) and not (
            raw_parts is not None and _raw_shell_word_is_literal(raw_parts[source_index])
        )

    while index < len(parts):
        source_index = index
        word = parts[index]
        index += 1
        if not after_options and word == "--":
            after_options = True
            continue
        if not after_options and word.startswith("--"):
            option, equals, value = word.partition("=")
            matches = [candidate for candidate in _LONG_OPTIONS if candidate.startswith(option)]
            if len(matches) == 1:
                option = matches[0]
            inplace |= option == "--in-place"
            if option in {"--expression", "--file", "--line-length"}:
                if not equals and index < len(parts):
                    source_index = index
                    value = parts[index]
                    index += 1
                if option != "--line-length":
                    explicit_script = True
                    unresolved_script |= unresolved(value, source_index)
                    if option == "--file" and value:
                        script_files.append(value)
                    elif option == "--expression":
                        programs.append(value)
            continue
        if not after_options and word.startswith("-") and word != "-":
            for offset, option in enumerate(word[1:], start=1):
                if option == "l":
                    value = word[offset + 1 :]
                    if value.isdigit():
                        break
                    if not value and index < len(parts) and parts[index].isdigit():
                        index += 1
                        break
                    continue
                if option in "ef":
                    value = word[offset + 1 :]
                    if not value and index < len(parts):
                        source_index = index
                        value = parts[index]
                        index += 1
                    explicit_script = True
                    unresolved_script |= unresolved(value, source_index)
                    if option == "f" and value:
                        script_files.append(value)
                    elif option == "e":
                        programs.append(value)
                    break
                if option in "iI":
                    inplace = True
                    # GNU -i has an attached optional suffix; BSD consumes the next word.
                    if (
                        offset == len(word) - 1
                        and index < len(parts)
                        and (
                            bsd_inplace_suffix
                            or option == "I"
                            or parts[index] == ""
                            or parts[index].startswith(".")
                        )
                    ):
                        index += 1
                    break
            continue
        positional.append(word)
        positional_indices.append(source_index)
    inputs = positional if explicit_script else positional[1:]
    if not explicit_script and positional:
        unresolved_script |= unresolved(positional[0], positional_indices[0])
        programs.append(positional[0])
    return _SedOperands(
        tuple(inputs), tuple(script_files), tuple(programs), unresolved_script, inplace
    )
