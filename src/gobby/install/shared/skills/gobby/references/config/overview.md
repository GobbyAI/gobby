# Configuration

Load when inspecting or changing daemon configuration. Discover the public
registry through `gobby-config:get_config_schema`, then read one coherent
desired/active snapshot with `get_config_values`. Fetch each tool schema first.

- `$gobby config references schema` — key discovery, metadata and visibility.
- `$gobby config references values` — defaults, desired/active state and scope.
- `$gobby config references updates` — revision-checked writes and recovery.
- `$gobby config references models` — profiles, endpoints and embeddings.
- `$gobby config references restarts` — activation and restart boundaries.

Load the relevant operation topic before mutation. Configuration guidance does
not authorize a settings change, secret rotation, endpoint probe or restart.
Menus do not perform operations or load their topics. Tool schemas are authoritative.

The database owns desired runtime overrides. Bootstrap YAML owns pre-database
wiring; project JSON owns repository settings. An unexpected active value needs
comparison with desired state and activation diagnostics before a write.
See the [configuration guide](../../../../../../../../docs/guides/configuration.md).
