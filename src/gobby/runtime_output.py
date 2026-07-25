"""Raw process-output helpers for the daemon runtime stream."""

import sys

_DAEMON_EFFECTIVE_CONFIG_TRANSPORT_PREFIX = (
    "daemon effective config request failed: daemon could not be reached"
)


def forward_subprocess_stderr(stderr: bytes | str) -> str:
    """Forward captured subprocess stderr to the daemon stderr stream once."""
    decoded = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    text = decoded.strip()
    if text:
        sys.stderr.write(f"{text}\n")
        sys.stderr.flush()
    return text


def is_daemon_effective_config_transport_error(stderr: str) -> bool:
    """Whether stderr reports a sanitized daemon effective-config transport failure."""
    return _DAEMON_EFFECTIVE_CONFIG_TRANSPORT_PREFIX in stderr.lower()
