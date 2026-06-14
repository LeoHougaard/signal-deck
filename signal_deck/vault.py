from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import ensure_config_files, load_config
from .util import relpath, sha256_text, slugify


@dataclass(frozen=True)
class Idea:
    id: str
    path: Path
    rel_path: str
    title: str
    user_text: str
    raw_text: str
    modified_at: float
    body_hash: str


def resolve_vault(path: str | Path | None) -> Path:
    return Path(path or ".").expanduser().resolve()


def ensure_vault(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    ensure_config_files(vault)
    ideas_dir = vault / str(load_config(vault).get("ideas_dir", "Ideas"))
    ideas_dir.mkdir(parents=True, exist_ok=True)


def scan_ideas(vault: Path, cfg: dict | None = None) -> list[Idea]:
    cfg = cfg or load_config(vault)
    ideas_root = vault / str(cfg.get("ideas_dir", "Ideas"))
    if not ideas_root.exists():
        return []
    ideas: list[Idea] = []
    for path in sorted(ideas_root.rglob("*.md")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        user_text = strip_agent_blocks(raw, cfg)
        title = extract_title(user_text, path)
        rel = relpath(path, vault)
        ideas.append(
            Idea(
                id=rel,
                path=path,
                rel_path=rel,
                title=title,
                user_text=user_text,
                raw_text=raw,
                modified_at=path.stat().st_mtime,
                body_hash=sha256_text(user_text),
            )
        )
    return ideas


def create_idea(vault: Path, title: str, body: str, cfg: dict | None = None) -> Idea:
    cfg = cfg or load_config(vault)
    ideas_root = vault / str(cfg.get("ideas_dir", "Ideas"))
    ideas_root.mkdir(parents=True, exist_ok=True)
    clean_title = title.strip() or "Untitled idea"
    path = _unique_idea_path(ideas_root / f"{slugify(clean_title)}.md")
    path.write_text(_format_user_note(clean_title, body), encoding="utf-8")
    return scan_idea_path(vault, path, cfg)


def update_idea(vault: Path, idea_id: str, title: str, body: str, cfg: dict | None = None) -> Idea:
    cfg = cfg or load_config(vault)
    path = (vault / idea_id).resolve()
    ideas_root = (vault / str(cfg.get("ideas_dir", "Ideas"))).resolve()
    try:
        path.relative_to(ideas_root)
    except ValueError as exc:
        raise ValueError("Idea must be inside the configured Ideas directory.") from exc
    if path.suffix.lower() != ".md" or not path.exists():
        raise ValueError("Idea note not found.")
    original = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(_format_user_note(title.strip() or path.stem, body) + _agent_block_suffix(original, cfg), encoding="utf-8")
    return scan_idea_path(vault, path, cfg)


def scan_idea_path(vault: Path, path: Path, cfg: dict | None = None) -> Idea:
    cfg = cfg or load_config(vault)
    raw = path.read_text(encoding="utf-8", errors="replace")
    user_text = strip_agent_blocks(raw, cfg)
    rel = relpath(path, vault)
    return Idea(
        id=rel,
        path=path,
        rel_path=rel,
        title=extract_title(user_text, path),
        user_text=user_text,
        raw_text=raw,
        modified_at=path.stat().st_mtime,
        body_hash=sha256_text(user_text),
    )


def _unique_idea_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Could not create a unique idea filename.")


def _format_user_note(title: str, body: str) -> str:
    clean_title = " ".join((title or "Untitled idea").split())
    clean_body = body.strip()
    if clean_body:
        return f"# {clean_title}\n\n{clean_body}\n"
    return f"# {clean_title}\n"


def _agent_block_suffix(text: str, cfg: dict) -> str:
    start = str(cfg["agent_block"]["start"])
    end = str(cfg["agent_block"]["end"])
    match = re.search(rf"{re.escape(start)}.*?{re.escape(end)}", text, flags=re.DOTALL)
    if not match:
        return ""
    return "\n" + match.group(0).rstrip() + "\n"


def extract_title(text: str, path: Path) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1).strip()
            if title.lower().startswith("relate "):
                return _derive_relation_title(text, title)
            return title
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def _derive_relation_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and not stripped.lower().startswith("- related:"):
            return stripped[2:].strip().rstrip(".") or fallback
    return fallback


def strip_agent_blocks(text: str, cfg: dict) -> str:
    start = re.escape(str(cfg["agent_block"]["start"]))
    end = re.escape(str(cfg["agent_block"]["end"]))
    pattern = re.compile(rf"\n?{start}.*?{end}\n?", re.DOTALL)
    return pattern.sub("\n", text).rstrip() + ("\n" if text.endswith("\n") else "")


def update_agent_block(note_path: Path, block_markdown: str, cfg: dict) -> None:
    original = note_path.read_text(encoding="utf-8", errors="replace")
    start = str(cfg["agent_block"]["start"])
    end = str(cfg["agent_block"]["end"])
    block = f"{start}\n{block_markdown.rstrip()}\n{end}"
    pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", re.DOTALL)
    if pattern.search(original):
        updated = pattern.sub(block, original, count=1)
    else:
        updated = original.rstrip() + "\n\n" + block + "\n"
    assert_only_agent_block_changed(original, updated, cfg)
    note_path.write_text(updated, encoding="utf-8")


def assert_only_agent_block_changed(original: str, updated: str, cfg: dict) -> None:
    original_user = strip_agent_blocks(original, cfg).strip()
    updated_user = strip_agent_blocks(updated, cfg).strip()
    if original_user != updated_user:
        raise ValueError("Refusing to change user-authored note text outside signal-agent blocks.")


def is_agent_owned_path(vault: Path, path: Path, cfg: dict) -> bool:
    resolved = path.resolve()
    root = vault.resolve()
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        return False
    if rel.startswith(".signal/") or rel == ".signal":
        return True
    return rel in {
        str(cfg.get("dashboard_html", "Signal Deck.html")),
        str(cfg.get("dashboard_md", "Signal Deck.md")),
    }
