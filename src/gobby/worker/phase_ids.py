"""Init phase identifiers in declaration order (R0 design 5.4).

Stdlib only. The live runner imports this module, so it must not pull in pydantic or any
other module of the worker package.
"""

from enum import StrEnum


class InitPhase(StrEnum):
    """The five phases of ``GobbyRunner.create``, in the order ``create`` runs them."""

    STORAGE_AND_CONFIG = "storage_and_config"
    CONTENT_REFUSAL = "content_refusal"
    STARTUP_CONTENT = "startup_content"
    CONFIG_RUNTIME = "config_runtime"
    RUNTIME_SERVICES = "runtime_services"


PHASE_ORDER: tuple[InitPhase, ...] = tuple(InitPhase)


class PhaseStatus(StrEnum):
    """What one initialize call reports about its phase (R0 design 5.4, ruling R30)."""

    READY = "ready"
    REFUSED = "refused"
    FAILED = "failed"
