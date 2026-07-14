"""Voice test guards.

No voice test may initialize real torch/MPS: an agent-run voice pytest with
real torch touching the Metal allocator is one of the native-crash classes
from incident #18196. An autouse fixture stubs ``torch`` in ``sys.modules``;
tests that need different torch behavior override the entry themselves.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest


def make_fake_torch() -> ModuleType:
    fake = ModuleType("torch")
    fake.mps = SimpleNamespace(  # type: ignore[attr-defined]
        empty_cache=lambda: None,
        set_per_process_memory_fraction=lambda fraction: None,
        recommended_max_memory=lambda: 24 * 1024**3,
    )
    fake.backends = SimpleNamespace(  # type: ignore[attr-defined]
        mps=SimpleNamespace(is_available=lambda: False)
    )
    fake.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    return fake


@pytest.fixture(autouse=True)
def stub_torch(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    fake = make_fake_torch()
    monkeypatch.setitem(sys.modules, "torch", fake)
    return fake
