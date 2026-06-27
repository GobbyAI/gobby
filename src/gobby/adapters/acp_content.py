"""ACP content block normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

PROMPT_CAPABILITY_KEYS = ("image", "audio", "embeddedContext")


def parse_prompt_capabilities(agent_capabilities: Any) -> dict[str, bool]:
    """Parse ACP prompt capability flags from an initialize response."""
    parsed = dict.fromkeys(PROMPT_CAPABILITY_KEYS, False)
    if not isinstance(agent_capabilities, Mapping):
        return parsed

    raw = agent_capabilities.get("promptCapabilities")
    if not isinstance(raw, Mapping):
        return parsed

    for key in PROMPT_CAPABILITY_KEYS:
        value = raw.get(key)
        parsed[key] = value is not None and value is not False
    return parsed


def normalize_prompt_blocks(
    content: str | list[dict[str, Any]],
    *,
    agent_capabilities: Any,
    prefix_text: str | None = None,
) -> list[dict[str, Any]]:
    """Build ACP prompt content blocks, gating optional block types by capability."""
    prompt_capabilities = parse_prompt_capabilities(agent_capabilities)
    blocks: list[dict[str, Any]] = []

    if prefix_text:
        blocks.append({"type": "text", "text": prefix_text})

    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
        return blocks

    for block in content:
        if not isinstance(block, Mapping):
            continue
        blocks.extend(_normalize_prompt_block(block, prompt_capabilities))

    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def normalize_acp_content_blocks(
    content: Any,
    *,
    include_text: bool = True,
) -> list[dict[str, Any]]:
    """Normalize ACP response content blocks to Gobby UI content blocks."""
    blocks: list[dict[str, Any]] = []
    for block in _iter_content_blocks(content):
        block_type = block.get("type")

        if block_type == "text":
            text = _block_text(block)
            if include_text and text:
                blocks.append({"type": "text", "content": text})
            continue

        if block_type == "content":
            blocks.extend(
                normalize_acp_content_blocks(block.get("content"), include_text=include_text)
            )
            continue

        if block_type == "resource_link":
            blocks.append(_normalize_resource_link(block))
            continue

        if block_type == "resource":
            resource = block.get("resource")
            blocks.append(
                {
                    "type": "resource",
                    "resource": dict(resource) if isinstance(resource, Mapping) else dict(block),
                }
            )
            continue

        if block_type == "image":
            blocks.append(_normalize_image_block(block))
            continue

        if block_type == "audio":
            blocks.append(_normalize_audio_block(block))
            continue

        if block_type == "diff":
            blocks.append(
                {
                    "type": "diff",
                    "path": block.get("path"),
                    "old_text": block.get("oldText"),
                    "new_text": block.get("newText"),
                }
            )
            continue

        if block_type == "terminal":
            blocks.append({"type": "terminal", "terminal_id": block.get("terminalId")})
            continue

        blocks.append(
            {
                "type": "unknown",
                "block_type": str(block_type or "unknown"),
                "raw": dict(block),
            }
        )
    return blocks


def normalize_tool_call_update(update: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize ACP tool_call and tool_call_update payloads."""
    tool_input = _first_mapping(update, "rawInput", "input")
    raw_output = update.get("rawOutput")
    content_blocks = normalize_acp_content_blocks(update.get("content"), include_text=True)
    status = _map_tool_status(update.get("status"))
    locations = _normalize_locations(update.get("locations"))
    data: dict[str, Any] = {
        "call_id": update.get("toolCallId"),
        "tool_name": update.get("title") or update.get("name"),
        "tool_input": tool_input,
        "tool_kind": update.get("kind"),
        "tool_status": status,
        "content_blocks": content_blocks,
        "locations": locations,
        "raw_output": raw_output if "rawOutput" in update else None,
    }
    return {key: value for key, value in data.items() if value not in (None, [], {})}


def extract_text(content: Any) -> str:
    """Extract text from ACP content block shapes."""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in _iter_content_blocks(content):
        if block.get("type") == "text":
            text = _block_text(block)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _normalize_prompt_block(
    block: Mapping[str, Any],
    prompt_capabilities: Mapping[str, bool],
) -> list[dict[str, Any]]:
    block_type = block.get("type")

    if block_type == "text":
        return [{"type": "text", "text": _block_text(block)}]

    if block_type == "attachment":
        link = _attachment_resource_link(block.get("attachment"))
        return [link] if link is not None else [_fallback_text("attachment", block)]

    if block_type == "resource_link":
        link = _prompt_resource_link(block)
        return [link] if link is not None else [_fallback_text("resource link", block)]

    if block_type == "image":
        if prompt_capabilities.get("image"):
            image = _prompt_image_block(block)
            if image is not None:
                return [image]
        link = _prompt_resource_link(block)
        if link is not None:
            return [link]
        return [_fallback_text("image", block)]

    if block_type == "audio":
        if prompt_capabilities.get("audio"):
            audio = _prompt_audio_block(block)
            if audio is not None:
                return [audio]
        link = _prompt_resource_link(block)
        if link is not None:
            return [link]
        return [_fallback_text("audio", block)]

    if block_type == "resource":
        if prompt_capabilities.get("embeddedContext"):
            return [dict(block)]
        return [_resource_fallback(block)]

    text = _block_text(block)
    if text:
        return [{"type": "text", "text": text}]
    return [_fallback_text(str(block_type or "unknown"), block)]


def _prompt_resource_link(block: Mapping[str, Any]) -> dict[str, Any] | None:
    uri = _string_value(block, "uri", "url", "content_url")
    if not uri:
        return None
    link: dict[str, Any] = {"type": "resource_link", "uri": uri}
    name = _string_value(block, "name", "title", "filename")
    if name:
        link["name"] = name
    description = _string_value(block, "description")
    if description:
        link["description"] = description
    mime_type = _string_value(block, "mimeType", "mime_type")
    if mime_type:
        link["mimeType"] = mime_type
    return link


def _attachment_resource_link(attachment: Any) -> dict[str, Any] | None:
    if not isinstance(attachment, Mapping):
        return None
    return _prompt_resource_link(attachment)


def _prompt_image_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    data = _string_value(block, "data")
    source = block.get("source")
    if not data and isinstance(source, Mapping):
        data = _string_value(source, "data")
    if not data:
        return None
    image: dict[str, Any] = {"type": "image", "data": data}
    mime_type = _string_value(block, "mimeType", "mime_type")
    if not mime_type and isinstance(source, Mapping):
        mime_type = _string_value(source, "mimeType", "mime_type", "media_type")
    if mime_type:
        image["mimeType"] = mime_type
    return image


def _prompt_audio_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    data = _string_value(block, "data")
    source = block.get("source")
    if not data and isinstance(source, Mapping):
        data = _string_value(source, "data")
    if not data:
        return None
    audio: dict[str, Any] = {"type": "audio", "data": data}
    mime_type = _string_value(block, "mimeType", "mime_type")
    if not mime_type and isinstance(source, Mapping):
        mime_type = _string_value(source, "mimeType", "mime_type", "media_type")
    if mime_type:
        audio["mimeType"] = mime_type
    return audio


def _resource_fallback(block: Mapping[str, Any]) -> dict[str, Any]:
    resource = block.get("resource")
    if not isinstance(resource, Mapping):
        return _fallback_text("embedded resource", block)

    uri = _string_value(resource, "uri")
    text = _block_text(resource)
    if uri and text:
        return {"type": "text", "text": f"Attached resource {uri}:\n{text}"}
    if text:
        return {"type": "text", "text": text}
    if uri:
        return {"type": "resource_link", "uri": uri, "name": _string_value(resource, "name") or uri}
    return _fallback_text("embedded resource", block)


def _fallback_text(label: str, block: Mapping[str, Any]) -> dict[str, str]:
    descriptor = _string_value(block, "name", "title", "uri", "url", "filename")
    suffix = f": {descriptor}" if descriptor else ""
    return {"type": "text", "text": f"[Unsupported {label} content omitted{suffix}]"}


def _normalize_resource_link(block: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {"type": "resource_link", "uri": _string_value(block, "uri", "url") or ""}
    for wire_key, ui_key in (
        ("name", "name"),
        ("title", "name"),
        ("description", "description"),
        ("mimeType", "mime_type"),
        ("mime_type", "mime_type"),
    ):
        value = block.get(wire_key)
        if isinstance(value, str) and value:
            normalized[ui_key] = value
    return normalized


def _normalize_image_block(block: Mapping[str, Any]) -> dict[str, Any]:
    data = _string_value(block, "data")
    mime_type = _string_value(block, "mimeType", "mime_type", "media_type")
    if data:
        source = {"type": "base64", "data": data}
        if mime_type:
            source["media_type"] = mime_type
        return {"type": "image", "source": source}
    uri = _string_value(block, "uri", "url")
    return {"type": "image", "url": uri} if uri else {"type": "image", **dict(block)}


def _normalize_audio_block(block: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {"type": "audio"}
    data = _string_value(block, "data")
    uri = _string_value(block, "uri", "url")
    mime_type = _string_value(block, "mimeType", "mime_type", "media_type")
    if data:
        normalized["data"] = data
    if uri:
        normalized["url"] = uri
    if mime_type:
        normalized["mime_type"] = mime_type
    return normalized


def _iter_content_blocks(content: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(content, Mapping):
        yield content
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, Mapping):
            yield block


def _block_text(block: Mapping[str, Any]) -> str:
    for key in ("text", "content"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return ""


def _string_value(block: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_mapping(block: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = block.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _normalize_locations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _map_tool_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {
        "pending": "pending",
        "in_progress": "calling",
        "completed": "completed",
        "failed": "error",
    }.get(value, value)
