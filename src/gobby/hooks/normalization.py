"""Compatibility facade for hook field normalization.

Implementation lives in private focused modules. Import from
``gobby.hooks.normalization`` unless a private module boundary is being edited.
"""

from gobby.hooks._normalization_canonical import (
    _build_canonical_tool_metadata as _build_canonical_tool_metadata,
)
from gobby.hooks._normalization_canonical import (
    _normalize_shell_tool_metadata as _normalize_shell_tool_metadata,
)
from gobby.hooks._normalization_canonical import (
    _set_canonical_tool_metadata as _set_canonical_tool_metadata,
)
from gobby.hooks._normalization_mcp import (
    _extract_mcp_content_object as _extract_mcp_content_object,
)
from gobby.hooks._normalization_mcp import (
    _parse_json_object as _parse_json_object,
)
from gobby.hooks._normalization_mcp import (
    _unwrap_mcp_tool_output as _unwrap_mcp_tool_output,
)
from gobby.hooks._normalization_mcp import (
    normalize_mcp_fields as normalize_mcp_fields,
)
from gobby.hooks._normalization_notifications import (
    _notification_severity_from_payload as _notification_severity_from_payload,
)
from gobby.hooks._normalization_notifications import (
    normalize_notification_input as normalize_notification_input,
)
from gobby.hooks._normalization_notifications import (
    notification_message_from_payload as notification_message_from_payload,
)
from gobby.hooks._normalization_notifications import (
    notification_type_from_payload as notification_type_from_payload,
)
from gobby.hooks._normalization_paths import (
    _append_unique_path as _append_unique_path,
)
from gobby.hooks._normalization_paths import (
    _extract_apply_patch_text as _extract_apply_patch_text,
)
from gobby.hooks._normalization_paths import (
    _extract_change_path as _extract_change_path,
)
from gobby.hooks._normalization_paths import (
    _extract_tool_input_paths as _extract_tool_input_paths,
)
from gobby.hooks._normalization_paths import (
    _normalize_apply_patch_input as _normalize_apply_patch_input,
)
from gobby.hooks._normalization_paths import (
    _normalize_file_change_input as _normalize_file_change_input,
)
from gobby.hooks._normalization_paths import (
    _parse_apply_patch_paths as _parse_apply_patch_paths,
)
from gobby.hooks._normalization_paths import (
    _setdefault_tool_input_paths as _setdefault_tool_input_paths,
)
from gobby.hooks._normalization_shell import (
    _SHELL_TOOLS as _SHELL_TOOLS,
)
from gobby.hooks._normalization_shell import (
    _extract_redirection_paths as _extract_redirection_paths,
)
from gobby.hooks._normalization_shell import (
    _get_command_text as _get_command_text,
)
from gobby.hooks._normalization_shell import (
    _has_perl_inplace_option as _has_perl_inplace_option,
)
from gobby.hooks._normalization_shell import (
    _has_sed_inplace_option as _has_sed_inplace_option,
)
from gobby.hooks._normalization_shell import (
    _looks_file_like as _looks_file_like,
)
from gobby.hooks._normalization_shell import (
    _looks_path_target as _looks_path_target,
)
from gobby.hooks._normalization_shell import (
    _shell_positional_args as _shell_positional_args,
)
from gobby.hooks._normalization_shell import (
    canonicalize_shell_tool_name as canonicalize_shell_tool_name,
)
from gobby.hooks._normalization_shell import (
    is_shell_tool as is_shell_tool,
)
from gobby.hooks._normalization_tools import (
    _detect_tool_error as _detect_tool_error,
)
from gobby.hooks._normalization_tools import (
    normalize_tool_fields as normalize_tool_fields,
)

__all__ = [
    "_SHELL_TOOLS",
    "_append_unique_path",
    "_build_canonical_tool_metadata",
    "_detect_tool_error",
    "_extract_apply_patch_text",
    "_extract_change_path",
    "_extract_mcp_content_object",
    "_extract_redirection_paths",
    "_extract_tool_input_paths",
    "_get_command_text",
    "_has_perl_inplace_option",
    "_has_sed_inplace_option",
    "_looks_file_like",
    "_looks_path_target",
    "_normalize_apply_patch_input",
    "_normalize_file_change_input",
    "_normalize_shell_tool_metadata",
    "_notification_severity_from_payload",
    "_parse_apply_patch_paths",
    "_parse_json_object",
    "_set_canonical_tool_metadata",
    "_setdefault_tool_input_paths",
    "_shell_positional_args",
    "_unwrap_mcp_tool_output",
    "canonicalize_shell_tool_name",
    "is_shell_tool",
    "normalize_mcp_fields",
    "normalize_notification_input",
    "normalize_tool_fields",
    "notification_message_from_payload",
    "notification_type_from_payload",
]
