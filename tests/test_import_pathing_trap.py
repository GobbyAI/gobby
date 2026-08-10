import os
from pathlib import Path

import pytest

import gobby.runner_maintenance

pytestmark = pytest.mark.unit


def test_fixture_redirects_gobby_home(protect_production_resources: None) -> None:
    """Fixture should keep daemon-path helpers out of ~/.gobby."""
    safe_home = Path(os.environ["GOBBY_HOME"]).resolve()
    real_home = (Path.home() / ".gobby").resolve()

    assert safe_home != real_home
    assert gobby.runner_maintenance.get_gobby_home().resolve() == safe_home
