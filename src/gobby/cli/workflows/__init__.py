"""CLI commands for managing Gobby workflows."""

import click

from .check import audit_workflow, check_workflow
from .control import (
    clear_workflow,
    disable_workflow,
    enable_workflow,
    reset_workflow,
    set_step,
    set_workflow,
)
from .inspect import list_workflows, show_workflow, workflow_status
from .manage import import_workflow, reinstall_workflows, reload_workflows
from .variables import get_variable, set_variable


@click.group()
def workflows() -> None:
    """Manage Gobby workflows."""
    pass


workflows.add_command(list_workflows)
workflows.add_command(check_workflow)
workflows.add_command(show_workflow)
workflows.add_command(workflow_status)
workflows.add_command(set_workflow)
workflows.add_command(clear_workflow)
workflows.add_command(set_step)
workflows.add_command(reset_workflow)
workflows.add_command(disable_workflow)
workflows.add_command(enable_workflow)
workflows.add_command(import_workflow)
workflows.add_command(reload_workflows)
workflows.add_command(reinstall_workflows)
workflows.add_command(audit_workflow)
workflows.add_command(set_variable)
workflows.add_command(get_variable)
