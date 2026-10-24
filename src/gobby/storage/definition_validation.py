"""Configuration-aware validation before definition registry writes."""

import json
from collections.abc import Mapping

from gobby.config.app import DaemonConfig
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.regex_validation import iter_definition_regexes, validate_definition_regexes


def validate_registry_regexes(db: HubDatabase, name: str, definition: object) -> None:
    """Validate regex inputs against the current DB-backed daemon configuration.

    Plain definitions need no config read. Regex-bearing definitions read the
    authoritative snapshot without resolving secrets, including inside an ambient
    registry transaction. Sync, imports, and API writes share this boundary.
    """
    data = json.loads(definition) if isinstance(definition, str) else definition
    if not isinstance(data, Mapping) or next(iter_definition_regexes(data), None) is None:
        return
    snapshot = ConfigRepository(db).read(resolve_secrets=False)
    config = DaemonConfig(
        workflow={
            "definition_regex_max_length": snapshot.values["workflow.definition_regex_max_length"]
        }
    )
    validate_definition_regexes(name, data, config)
