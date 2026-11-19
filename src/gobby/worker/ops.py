"""Allow-listed domain-invocation op table (R0 design 5.9, 5.10).

R0 registers no op (R11). A later chunk adds one ``DomainOp(...)`` line to
``_REGISTRATIONS`` for each op it serves.

Caller ownership (R19, design 5.20). A worker op binds no caller and no daemon-owned
execution. ``require_machine_owned`` returns at once because no caller scope is bound
(``gobby.hub.caller_scope``), and ``is_daemon_execution`` reads False
(``gobby.utils.session_context``). A native route that exposes a worker op checks machine
ownership itself before it calls the op. The ``daemon_execution`` recipient exemption
cannot fire from a worker op.

Resolution is lazy. Importing this module imports no module that an op names, so a
registration names its function as a ``"module:qualname"`` string.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from gobby.worker.decode import Spec


class OpRegistrationError(Exception):
    """An op registration names a function that cannot serve it."""


@dataclass(frozen=True)
class DomainOp:
    """One allow-listed domain op. ``func`` is resolved on first use."""

    name: str
    func: str
    params: Mapping[str, Spec]


def _build(ops: tuple[DomainOp, ...]) -> Mapping[str, DomainOp]:
    table: dict[str, DomainOp] = {}
    for op in ops:
        if op.name in table:
            raise OpRegistrationError(f"duplicate op name: {op.name}")
        table[op.name] = op
    return types.MappingProxyType(table)


_REGISTRATIONS: tuple[DomainOp, ...] = (
    # One DomainOp(name=..., func="module:qualname", params={...}) line per op. R0 has none.
)

OPS: Mapping[str, DomainOp] = _build(_REGISTRATIONS)


def resolve(op: DomainOp) -> Callable[..., object]:
    """Return the op's function, importing it on first use and checking its signature."""
    return _resolve_path(op.func, frozenset(op.params))


@functools.cache
def _resolve_path(path: str, declared: frozenset[str]) -> Callable[..., object]:
    module_name, separator, qualname = path.partition(":")
    if not separator or not module_name or not qualname:
        raise OpRegistrationError(f"op path must be 'module:qualname': {path!r}")
    try:
        found: object = importlib.import_module(module_name)
    except Exception as exc:
        # A module that fails on import is a registration fault of any class, not only a
        # missing module (R53). The text names the module and the failure.
        raise OpRegistrationError(f"op module does not import: {module_name}: {exc}") from exc
    for part in qualname.split("."):
        found = getattr(found, part, None)
    if not callable(found):
        raise OpRegistrationError(f"op path does not name a callable: {path!r}")
    _check_signature(path, found, declared)
    return found


def _check_signature(path: str, fn: Callable[..., object], declared: frozenset[str]) -> None:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise OpRegistrationError(f"op has no inspectable signature: {path!r}") from exc
    named: dict[str, inspect.Parameter] = {}
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise OpRegistrationError(f"op takes a positional-only parameter: {path!r}")
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        named[parameter.name] = parameter
    unknown = sorted(declared - named.keys())
    if unknown:
        raise OpRegistrationError(f"declared parameters the op does not take: {unknown}")
    missing = sorted(
        name
        for name, parameter in named.items()
        if parameter.default is inspect.Parameter.empty and name not in declared
    )
    if missing:
        raise OpRegistrationError(f"required parameters the op does not declare: {missing}")
