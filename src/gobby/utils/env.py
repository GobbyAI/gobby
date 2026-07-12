"""Environment-variable expansion helpers."""

import os
import re
from collections.abc import Mapping

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env_variables(value: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` patterns in a string.

    Unset variables without a default remain unchanged so callers can surface
    the unresolved configuration at the boundary where it is consumed.
    """

    def replace_match(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)
        env_value = os.environ.get(var_name)

        if env_value:
            return env_value
        if default_value is not None:
            return default_value
        return match.group(0)

    return ENV_VAR_PATTERN.sub(replace_match, value)


def expand_env_mapping(values: Mapping[str, str] | None) -> dict[str, str] | None:
    """Return a copy with environment variables expanded in mapping values."""
    if values is None:
        return None

    return {key: expand_env_variables(value) for key, value in values.items()}
