from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_yaml(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return dict(default or {})
    try:
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else dict(default or {})
    except json.JSONDecodeError:
        pass
    loaded = _parse_simple_yaml(text)
    if not isinstance(loaded, dict):
        return dict(default or {})
    return loaded


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(data), encoding="utf-8")


def dump_yaml(data: Any, indent: int = 0) -> str:
    lines = _dump_value(data, indent)
    return "\n".join(lines).rstrip() + "\n"


def merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dump_value(value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, child in value.items():
            if isinstance(child, dict):
                lines.append(f"{pad}{key}:")
                lines.extend(_dump_value(child, indent + 2))
            elif isinstance(child, list):
                lines.append(f"{pad}{key}:")
                if child:
                    lines.extend(_dump_value(child, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_format_scalar(child)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(_dump_value(item, indent + 2))
            else:
                lines.append(f"{pad}- {_format_scalar(item)}")
        return lines
    return [f"{pad}{_format_scalar(value)}"]


def _format_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or text.strip() != text or text.lower() in {"true", "false", "null"}:
        return json.dumps(text)
    if any(char in text for char in [": ", "#", "[", "]", "{", "}", "\n"]):
        return json.dumps(text)
    return text


def _parse_simple_yaml(text: str) -> Any:
    tokens: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()
        tokens.append((indent, content))
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value


def _parse_block(tokens: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(tokens):
        return {}, index
    is_list = tokens[index][0] == indent and tokens[index][1].startswith("-")
    if is_list:
        result: list[Any] = []
        while index < len(tokens):
            current_indent, content = tokens[index]
            if current_indent != indent or not content.startswith("-"):
                break
            item = content[1:].strip()
            index += 1
            if item:
                result.append(_parse_scalar(item))
            elif index < len(tokens) and tokens[index][0] > current_indent:
                child, index = _parse_block(tokens, index, tokens[index][0])
                result.append(child)
            else:
                result.append(None)
        return result, index

    result: dict[str, Any] = {}
    while index < len(tokens):
        current_indent, content = tokens[index]
        if current_indent != indent or content.startswith("-"):
            break
        key, sep, rest = content.partition(":")
        if not sep:
            index += 1
            continue
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _parse_scalar(rest)
        elif index < len(tokens) and tokens[index][0] > current_indent:
            child, index = _parse_block(tokens, index, tokens[index][0])
            result[key] = child
        else:
            result[key] = {}
    return result, index


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith('"') or value.startswith("'"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip("'\"")
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value
