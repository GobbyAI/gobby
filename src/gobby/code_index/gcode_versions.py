"""Version floors for gcode capabilities used by the daemon."""

from gobby.install.version_pins import MANAGED_BIN_VERSION_PINS

MIN_GCODE_GRAPH_VERSION = MANAGED_BIN_VERSION_PINS["gcode"]
MIN_GCODE_PRUNE_BUDGET_VERSION = MANAGED_BIN_VERSION_PINS["gcode"]
GCODE_ALLOW_MISSING_INDEXED_FILE_VERSION = "0.9.5"
