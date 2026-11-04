"""Expansion templates render inside a Jinja sandbox."""

import pytest
from jinja2.exceptions import SecurityError

from gobby.tasks.expansion._common import _render_template

pytestmark = pytest.mark.unit


def test_render_template_refuses_attribute_escape() -> None:
    with pytest.raises(SecurityError):
        _render_template("{{ ''.__class__.__mro__ }}", {})


def test_render_template_still_renders_plain_context() -> None:
    assert _render_template("Task: {{ title }}", {"title": "Sandboxed"}) == "Task: Sandboxed"
