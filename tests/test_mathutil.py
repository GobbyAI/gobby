import pytest

from mathutil import add


@pytest.mark.unit
def test_add() -> None:
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0
