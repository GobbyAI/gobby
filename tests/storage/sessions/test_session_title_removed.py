"""Sessions carry no stored title (#25485).

Display labels derive from the session ref, the agent definition, the claimed task and
the status line, so the session model has no title or title-source field to persist.
"""

from dataclasses import fields

from gobby.storage.session_models import Session


def test_session_model_carries_no_stored_title_fields() -> None:
    names = {field.name for field in fields(Session)}

    assert "title" not in names
    assert "title_source" not in names
