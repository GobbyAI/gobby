"""Shared helpers for task CRUD CLI commands."""

import click

from gobby.storage.tasks import TASK_TYPE_CHOICES

TASK_TYPE_CHOICE = click.Choice(TASK_TYPE_CHOICES)
