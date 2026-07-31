"""Hub backup package: stop-consistent datastore backup with verified restore."""

from gobby.cli.hub_backup._manifest import (
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    GateDecision,
    HubBackupManifest,
    SourceIdentity,
    check_manifest_gate,
    load_manifest,
)

__all__ = [
    "MANIFEST_FORMAT",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "GateDecision",
    "HubBackupManifest",
    "SourceIdentity",
    "check_manifest_gate",
    "load_manifest",
]
