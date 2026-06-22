from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .taxonomy import categorize_text
from .util import slugify


def ensure_inbox(vault: Path, cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config(vault)
    inbox_cfg = cfg.get("inbox", {})
    if not inbox_cfg.get("enabled", True):
        return
    (vault / str(inbox_cfg.get("dir", "Idea Inbox"))).mkdir(parents=True, exist_ok=True)


def process_inbox(vault: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config(vault)
    inbox_cfg = cfg.get("inbox", {})
    if not inbox_cfg.get("enabled", True):
        return {"processed": 0, "created": []}
    inbox_root = (vault / str(inbox_cfg.get("dir", "Idea Inbox"))).resolve()
    archive_root = (vault / str(inbox_cfg.get("archive_dir", "Idea Inbox/Processed"))).resolve()
    ideas_root = (vault / str(cfg.get("ideas_dir", "Ideas"))).resolve()
    inbox_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    ideas_root.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    for path in sorted(inbox_root.glob("*.md")):
        if archive_root in path.resolve().parents:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        if not raw.strip():
            continue
        title = _extract_inbox_title(raw, path)
        idea_path = _next_idea_path(ideas_root, title)
        idea_path.write_text(_format_imported_note(title, raw, path, cfg), encoding="utf-8")
        archive_path = _unique_archive_path(archive_root / f"{datetime.now().date().isoformat()}-{path.name}")
        shutil.move(str(path), str(archive_path))
        created.append(idea_path.relative_to(vault).as_posix())
    return {"processed": len(created), "created": created}


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


def _format_imported_note(title: str, raw: str, source_path: Path, cfg: dict[str, Any]) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    categories = categorize_text(f"{title}\n{raw}")
    frontmatter = [
        "---",
        "type: idea",
        "source: idea-inbox",
        f"captured: {json.dumps(now)}",
        f"inbox_note: {json.dumps(source_path.name)}",
        "categories:",
        *[f"  - {category}" for category in categories],
        "---",
        "",
    ]
    body = raw.rstrip()
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
