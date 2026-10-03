"""Committed gclient mark assets match their generators and grid contract."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKS = ROOT / "crates/gclient/assets/marks"
LOGO = ROOT / "web/public/logo.png"


def generate(script: str, *args: str) -> bytes:
    result = subprocess.run(
        [sys.executable, str(MARKS / script), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout


def data_lines(path: Path, kind: str, cols: int, rows: int) -> list[str]:
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert all(line == line.rstrip() for line in content.splitlines())
    lines = content.splitlines()
    assert lines[0] == f"# {kind} {cols}x{rows}"
    data = [line for line in lines if not line.startswith("#")]
    assert len(data) == rows
    return data


def test_generators_regenerate_grids_byte_identically() -> None:
    assert (MARKS / "mask2.py").is_file()
    assert (MARKS / "wordmark.py").is_file()
    cases = [
        ("mask2.py", (str(LOGO), "48", "14", "grid"), "goblin-29x14.grid"),
        ("wordmark.py", ("braille", "grid", "lower"), "wordmark-braille-54x8.txt"),
        ("wordmark.py", ("shadow", "grid", "lower"), "wordmark-shadow-49x9.grid"),
    ]
    for script, args, name in cases:
        assert generate(script, *args) == (MARKS / name).read_bytes()


def test_generated_goblin_grid_has_the_committed_dimensions_and_roles() -> None:
    data = data_lines(MARKS / "goblin-29x14.grid", "halfblock", 29, 14)
    assert all(len(line) == 58 for line in data)
    assert set("".join(data)) <= set("aoig.")
    assert "a" in "".join(data)


def test_hand_authored_goblin_grid_wears_its_node_halo() -> None:
    # goblin-41x18.grid is Josh's hand-corrected goblin inside its node halo
    # (#23280); no generator produces it, so it is checked, never regenerated.
    data = data_lines(MARKS / "goblin-41x18.grid", "halfblock", 41, 18)
    assert all(len(line) == 82 for line in data)
    halves = "".join(data)
    assert set(halves) == set("aoigne.")
    assert halves.count("g") == 2


def test_wordmark_assets_have_the_committed_dimensions_and_roles() -> None:
    braille = data_lines(MARKS / "wordmark-braille-54x8.txt", "braille", 54, 8)
    assert all(len(line) == 54 for line in braille)
    assert all(0x2800 <= ord(char) <= 0x28FF for line in braille for char in line)
    shadow = data_lines(MARKS / "wordmark-shadow-49x9.grid", "halfblock", 49, 9)
    assert all(len(line) == 98 for line in shadow)
    assert set("".join(shadow)) == set("ad.")


def test_dark_and_light_terminal_renders_match_generators() -> None:
    for theme in ("dark", "light"):
        for source, cols, rows, name in (
            ("mask2.py", "56", "16", "goblin-33x16"),
            ("mask2.py", "48", "14", "goblin-29x14"),
        ):
            rendered = generate(source, str(LOGO), cols, rows, f"ansi-{theme}")
            assert rendered == (MARKS / "renders" / f"{name}-{theme}.ans").read_bytes()
            assert b"\x1b[" in rendered
        for variant, name in (("braille", "wordmark-braille"), ("shadow", "wordmark-shadow")):
            rendered = generate("wordmark.py", variant, f"ansi-{theme}", "lower")
            assert rendered == (MARKS / "renders" / f"{name}-{theme}.ans").read_bytes()
            assert b"\x1b[" in rendered


def test_readme_documents_roles_homes_and_regeneration() -> None:
    readme = (MARKS / "README.md").read_text(encoding="utf-8")
    assert "three homes" in readme
    assert "macOS" in readme
    for role in ("accent", "overlay1", "ink", "glint", "dim", "overlay0", "surface1"):
        assert role in readme
    for name in (
        "goblin-41x18.grid",
        "goblin-29x14.grid",
        "wordmark-braille-54x8.txt",
        "wordmark-shadow-49x9.grid",
    ):
        assert name in readme
    assert "hand-authored and is never regenerated" in readme
