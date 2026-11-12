from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .taxonomy import categorize_text
from .util import sha256_text, slugify


def ensure_inbox(vault: Path, cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config(vault)
    inbox_cfg = cfg.get("inbox", {})
    if not inbox_cfg.get("enabled", True):
        return
    (vault / str(inbox_cfg.get("dir", "Idea Inbox"))).mkdir(parents=True, exist_ok=True)


def process_inbox(vault: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    # Resolve the root once, including Windows 8.3 aliases, before deriving
    # paths or recording relative destinations for interrupted imports.
    vault = vault.resolve()
    cfg = cfg or load_config(vault)
    inbox_cfg = cfg.get("inbox", {})
    if not inbox_cfg.get("enabled", True):
        return {"processed": 0, "created": []}
    inbox_root = _contained_path(vault, vault / str(inbox_cfg.get("dir", "Idea Inbox")))
    archive_root = _contained_path(vault, vault / str(inbox_cfg.get("archive_dir", "Idea Inbox/Processed")))
    ideas_root = _contained_path(vault, vault / str(cfg.get("ideas_dir", "Ideas")))
    inbox_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    ideas_root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    for path in sorted(inbox_root.glob("*.md")):
        _contained_path(inbox_root, path)
        if archive_root in path.resolve().parents:
            continue
        raw = path.read_bytes().decode("utf-8")
        if not raw.strip():
            continue
        # Keep the destination across interruptions, even if the idea is edited
        # before a failed archive move is retried.
        pending_root = _contained_path(vault, vault / ".signal" / "inbox_pending")
        pending_root.mkdir(parents=True, exist_ok=True)
        pending = pending_root / f"{sha256_text(path.relative_to(vault).as_posix())}.json"
        if pending.exists():
            record = json.loads(pending.read_text(encoding="utf-8"))
            if record["body_hash"] != sha256_text(raw):
                raise ValueError(f"Inbox capture changed during recovery: {path}. Preserve both versions before retrying.")
            idea_path = _contained_path(ideas_root, vault / record["idea"])
            archive_path = _contained_path(archive_root, vault / record["archive"])
            title = record["title"]
        else:
            title = _extract_inbox_title(raw, path)
            idea_path = _next_idea_path(ideas_root, title)
            archive_path = _unique_archive_path(archive_root / f"{datetime.now().date().isoformat()}-{path.name}")
            record = {
                "body_hash": sha256_text(raw), "title": title,
                "capture_id": uuid.uuid4().hex,
                "idea": idea_path.relative_to(vault).as_posix(),
                "archive": archive_path.relative_to(vault).as_posix(),
            }
            _atomic_write(pending, json.dumps(record))
        if not idea_path.exists():
            _atomic_write(idea_path, _format_imported_note(title, raw, path, cfg, record["capture_id"]))
        marker = f'inbox_capture_id: {json.dumps(record["capture_id"])}'
        if marker not in idea_path.read_text(encoding="utf-8"):
            raise ValueError(f"Inbox recovery destination belongs to another note: {idea_path}")
        if archive_path.exists():
            raise ValueError(f"Inbox archive destination already exists: {archive_path}")
        shutil.move(str(path), str(archive_path))
        pending.unlink()
        created.append(idea_path.relative_to(vault).as_posix())
    return {"processed": len(created), "created": created}


def _contained_path(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Inbox path must stay inside {root}: {path}") from exc
    return resolved


def _atomic_write(path: Path, text: str) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _extract_inbox_title(text: str, path: Path) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            title = " ".join(match.group(1).strip().split())[:90] or path.stem
            if title.lower().startswith("relate "):
                return _derive_relation_title(text, title)[:90]
            return title
    for line in text.splitlines():
        clean = re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
        if clean:
            return " ".join(clean.split())[:90]
    return path.stem.replace("-", " ").replace("_", " ").strip() or "Untitled idea"


def _derive_relation_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and not stripped.lower().startswith("- related:"):
            return " ".join(stripped[2:].strip().rstrip(".").split()) or fallback
    return fallback


def _next_idea_path(ideas_root: Path, title: str) -> Path:
    max_index = 0
    for existing in ideas_root.glob("*.md"):
        match = re.match(r"^(\d{3,})-", existing.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    slug = slugify(title)[:84].strip("-") or "untitled-idea"
    base = ideas_root / f"{max_index + 1:03d}-{slug}.md"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = ideas_root / f"{max_index + 1:03d}-{slug}-{index}.md"
        if not candidate.exists():
            return candidate
    raise ValueError("Could not create a unique idea filename.")


def _format_imported_note(title: str, raw: str, source_path: Path, cfg: dict[str, Any], capture_id: str) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    categories = categorize_text(f"{title}\n{raw}")
    frontmatter = [
        "---",
        "type: idea",
        "source: idea-inbox",
        f"captured: {json.dumps(now)}",
        f"inbox_note: {json.dumps(source_path.name)}",
        f"inbox_capture_id: {json.dumps(capture_id)}",
        "categories:",
        *[f"  - {category}" for category in categories],
        "---",
        "",
    ]
    body = raw
    agent_block = [
        "",
        str(cfg["agent_block"]["start"]),
        "## Categories",
        "",
        *[f"- #{category}" for category in categories],
        "",
        "## Related ideas",
        "",
        "No related ideas found yet.",
        str(cfg["agent_block"]["end"]),
        "",
    ]
    return "\n".join(frontmatter) + f"# {title}\n\n## Captured note\n\n{body}\n" + "\n".join(agent_block)


def _unique_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Could not create a unique processed inbox filename.")
