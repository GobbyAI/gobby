from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_srt_wrap_is_single_chokepoint() -> None:
    wrap_hits: list[str] = []
    for path in (
        ROOT / "src/gobby/agents/spawn_executor.py",
        ROOT / "src/gobby/agents/spawn_executor_providers.py",
        ROOT / "src/gobby/agents/resume_executor.py",
        ROOT / "src/gobby/agents/srt_runtime.py",
    ):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "launch.wrap(" in line or ".wrap(cmd)" in line or ".wrap(command)" in line:
                wrap_hits.append(f"{path.relative_to(ROOT)}:{lineno}:{line.strip()}")
    allowed = {
        "src/gobby/agents/spawn_executor.py",
        "src/gobby/agents/srt_runtime.py",
    }
    unexpected = [hit for hit in wrap_hits if hit.split(":")[0] not in allowed]
    assert unexpected == [], f"wrap sites outside the chokepoint: {unexpected}"
    assert any(hit.startswith("src/gobby/agents/spawn_executor.py") for hit in wrap_hits)
    assert any("_preflight_srt" in hit or "preflight" in hit for hit in wrap_hits)
