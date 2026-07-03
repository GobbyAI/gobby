from __future__ import annotations

import logging


def log_pool_timeout_drop(
    logger: logging.Logger,
    *,
    span_count: int,
    error: BaseException,
) -> None:
    logger.warning(
        "Dropping %d telemetry spans because hub pool acquisition timed out: %s",
        span_count,
        error,
    )
