from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import load_config
from .util import relpath, sha256_text, slugify
from .vault import is_agent_owned_path, update_agent_block


def sync_media_notes(vault: Path, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    cfg = cfg or load_config(vault)
    obsidian_cfg = cfg.get("obsidian", {})
    if not obsidian_cfg.get("media_notes", True):
        return {"written": 0}
    media_root = _media_root(vault, cfg)
    media_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(vault / ".signal" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                """
                SELECT d.*, i.title AS idea_title, i.path AS idea_path, m.note AS user_media_note
                FROM discoveries d
                JOIN ideas i ON i.id = d.idea_id
                LEFT JOIN media_notes m ON m.discovery_id = d.id AND m.idea_id = d.idea_id
                LEFT JOIN discovery_status s ON s.idea_id = d.idea_id AND s.url = d.url
                WHERE COALESCE(s.status, '') NOT IN ('bad', 'used')
                ORDER BY d.url, d.score DESC, d.updated_at DESC
                """
            )
        )
    finally:
        conn.close()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if _should_materialize(row):
            grouped.setdefault(str(row["url"]), []).append(row)
    written = 0
    for url, discoveries in grouped.items():
        path = media_note_path(vault, discoveries[0], cfg)
        if not is_agent_owned_path(vault, path, cfg):
            raise ValueError(f"Refusing to write non-agent-owned media note: {path}")
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        text = _render_media_note(vault, url, discoveries, cfg, existing)
        if not path.exists() or path.read_text(encoding="utf-8", errors="replace") != text:
            path.write_text(text, encoding="utf-8")
            written += 1
    return {"written": written}


def sync_related_idea_blocks(vault: Path, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    cfg = cfg or load_config(vault)
    if not cfg.get("obsidian", {}).get("related_idea_blocks", True):
        return {"written": 0}
    ideas_root = (vault / str(cfg.get("ideas_dir", "Ideas"))).resolve()
    conn = sqlite3.connect(vault / ".signal" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                """
                SELECT d.idea_id, d.url AS related_idea_id, d.title, d.why, d.score,
                       source.title AS idea_title, target.title AS related_idea_title
                FROM discoveries d
                JOIN ideas source ON source.id = d.idea_id
                JOIN ideas target ON target.id = d.url
                WHERE d.source_type='manual'
                  AND d.url LIKE 'Ideas/%.md'
                ORDER BY d.idea_id, d.score DESC, d.updated_at DESC
                """
            )
        )
    finally:
        conn.close()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["idea_id"]), []).append(row)
    written = 0
    attached = _attached_media_by_idea(vault)
    all_idea_ids = sorted(set(grouped) | set(attached))
    for idea_id in all_idea_ids:
        note_path = (vault / idea_id).resolve()
        try:
            note_path.relative_to(ideas_root)
        except ValueError as exc:
            raise ValueError("Refusing to write relationship block outside Ideas.") from exc
        if not note_path.exists() or note_path.suffix.lower() != ".md":
            continue
        before = note_path.read_text(encoding="utf-8", errors="replace")
        block = _render_related_idea_block(grouped.get(idea_id, []), attached.get(idea_id, []), vault, cfg)
        update_agent_block(note_path, block, cfg)
        after = note_path.read_text(encoding="utf-8", errors="replace")
        if after != before:
            written += 1
    return {"written": written}


def media_note_path(vault: Path, discovery: Any, cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config(vault)
    media_root = _media_root(vault, cfg)
    url = str(discovery["url"])
    digest = sha256_text(url)[:12]
    existing = sorted(media_root.glob(f"signal-{digest}-*.md"))
    if existing:
        return existing[0]
    title = slugify(str(discovery["title"]))[:72].strip("-") or "media"
    return media_root / f"signal-{digest}-{title}.md"


def media_note_rel_path(vault: Path, discovery: Any, cfg: dict[str, Any] | None = None) -> str:
    return relpath(media_note_path(vault, discovery, cfg), vault)


def media_note_wikilink(vault: Path, discovery: Any, label: str | None = None, cfg: dict[str, Any] | None = None) -> str:
    target = media_note_rel_path(vault, discovery, cfg).removesuffix(".md")
    if label:
        return f"[[{target}|{label}]]"
    return f"[[{target}]]"


def _should_materialize(row: Any) -> bool:
    source = str(row["source_type"]).lower()
    url = str(row["url"]).lower()
    image_url = str(row["image_url"] or "")
    if source == "manual" or url.startswith("signal://"):
        return False
    return (
        source == "youtube"
        or "youtube.com/watch" in url
        or "youtu.be/" in url
        or source == "arxiv"
        or "arxiv.org" in url
        or url.endswith(".pdf")
        or bool(image_url)
    )


def _render_media_note(vault: Path, url: str, discoveries: list[sqlite3.Row], cfg: dict[str, Any], existing: str = "") -> str:
    primary = discoveries[0]
    title = str(primary["title"]).strip() or "Media signal"
    source = str(primary["source_type"])
    image_url = str(primary["image_url"] or "")
    citations = _citations(primary)
    ideas = _unique_ideas(discoveries)
    lines = [
        "---",
        "type: signal-media",
        f"source: {_yaml_scalar(source)}",
        f"url: {_yaml_scalar(url)}",
        f"signal_id: {_yaml_scalar(sha256_text(url)[:12])}",
        f"updated: {_yaml_scalar(str(primary['updated_at']))}",
        "---",
        "",
        "<!-- signal-media:start -->",
        f"# {title}",
        "",
    ]
    if image_url.startswith("http"):
        lines.extend([f"![thumbnail]({image_url})", ""])
    if url.startswith("http"):
        lines.extend([f"Source: [{url}]({url})", ""])
    summary = _clean(str(primary["summary"] or ""))
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    why_blocks = _why_blocks(discoveries)
    if why_blocks:
        lines.extend(["## Why it matched", ""])
        lines.extend(why_blocks)
        lines.append("")
    lines.extend(["## Connected ideas", ""])
    for idea_id, idea_title in ideas:
        lines.append(f"- {media_note_idea_link(idea_id, idea_title)}")
    lines.append("")
    if citations:
        lines.extend(["## Citations", ""])
        for citation in citations:
            citation_url = citation.get("url", "")
            citation_title = citation.get("title") or citation_url
            if citation_url.startswith("http"):
                lines.append(f"- [{citation_title}]({citation_url})")
        lines.append("")
    user_notes = _user_notes(discoveries)
    if user_notes:
        lines.extend(["## Notes", ""])
        lines.extend(user_notes)
        lines.append("")
    lines.append("<!-- signal-media:end -->")
    personal = _personal_notes(existing)
    lines.extend(["", "## Personal notes", ""])
    if personal:
        lines.append(personal.rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def media_note_idea_link(idea_id: str, idea_title: str) -> str:
    target = idea_id.removesuffix(".md")
    return f"[[{target}|{idea_title}]]"


def _render_related_idea_block(
    rows: list[sqlite3.Row],
    attached_media: list[sqlite3.Row],
    vault: Path,
    cfg: dict[str, Any],
) -> str:
    lines = ["## Related ideas", ""]
    seen: set[str] = set()
    for row in rows:
        related_id = str(row["related_idea_id"])
        if related_id in seen:
            continue
        seen.add(related_id)
        title = str(row["related_idea_title"] or "").strip() or str(row["title"]).replace("Related idea: ", "")
        score = int(float(row["score"] or 0) * 100)
        lines.append(f"- {media_note_idea_link(related_id, title)} `{score}`")
    if not seen:
        lines.append("No related ideas found yet.")
    if attached_media:
        lines.extend(["", "## Attached media", ""])
        for row in attached_media:
            target = media_note_rel_path(vault, row, cfg).removesuffix(".md")
            title = str(row["title"] or "Media")
            lines.append(f"- [[{target}|{title}]]")
    return "\n".join(lines)


def _attached_media_by_idea(vault: Path) -> dict[str, list[sqlite3.Row]]:
    conn = sqlite3.connect(vault / ".signal" / "state.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                """
                SELECT d.*
                FROM discoveries d
                JOIN discovery_status s ON s.idea_id=d.idea_id AND s.url=d.url
                WHERE s.status='attached'
                ORDER BY d.idea_id, s.updated_at DESC
                """
            )
        )
    finally:
        conn.close()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["idea_id"]), []).append(row)
    return grouped


def _unique_ideas(discoveries: list[sqlite3.Row]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for row in discoveries:
        idea_id = str(row["idea_id"])
        if idea_id in seen:
            continue
        seen.add(idea_id)
        output.append((idea_id, str(row["idea_title"])))
    return output


def _why_blocks(discoveries: list[sqlite3.Row]) -> list[str]:
    blocks = []
    for row in discoveries[:8]:
        idea = media_note_idea_link(str(row["idea_id"]), str(row["idea_title"]))
        why = _clean(str(row["why"] or ""))
        if why:
            blocks.append(f"- {idea}: {why}")
    return blocks


def _user_notes(discoveries: list[sqlite3.Row]) -> list[str]:
    notes = []
    for row in discoveries[:8]:
        note = _clean(str(row["user_media_note"] or ""))
        if note:
            idea = media_note_idea_link(str(row["idea_id"]), str(row["idea_title"]))
            notes.append(f"- {idea}: {note}")
    return notes


def _citations(row: sqlite3.Row) -> list[dict[str, str]]:
    try:
        loaded = json.loads(str(row["citations_json"] or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    citations = []
    for item in loaded:
        if isinstance(item, dict):
            citations.append({"url": str(item.get("url") or ""), "title": str(item.get("title") or "")})
    return citations


def _clean(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value.strip())


def _yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _personal_notes(existing: str) -> str:
    marker = "## Personal notes"
    if marker not in existing:
        return ""
    return existing.split(marker, 1)[1].strip()


def _media_root(vault: Path, cfg: dict[str, Any]) -> Path:
    raw = str(cfg.get("obsidian", {}).get("media_dir", "Media")).strip() or "Media"
    root = vault.resolve()
    media_root = (vault / raw).resolve()
    media_root.relative_to(root)
    rel = media_root.relative_to(root).as_posix()
    ideas_dir = str(cfg.get("ideas_dir", "Ideas")).strip().strip("/\\")
    if rel in {"", ".", ".signal"} or rel.startswith(".signal/"):
        raise ValueError("Obsidian media notes need a dedicated generated folder outside .signal.")
    if ideas_dir and (rel == ideas_dir or rel.startswith(f"{ideas_dir}/")):
        raise ValueError("Obsidian media notes cannot be generated inside the Ideas directory.")
    return media_root
