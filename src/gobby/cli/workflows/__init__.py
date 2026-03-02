"""CLI commands for managing Gobby workflows."""

import click

from .check import audit_workflow, check_workflow
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
workflows.add_command(import_workflow)
workflows.add_command(reload_workflows)
workflows.add_command(reinstall_workflows)
workflows.add_command(audit_workflow)
workflows.add_command(set_variable)
workflows.add_command(get_variable)
