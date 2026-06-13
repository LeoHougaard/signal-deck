from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from .config import imports_path, load_config
from .util import iso_now, sha256_text, slugify
from .vault import ensure_vault


def import_markdown_zip(zip_path: Path, vault: Path, split_numbered: bool = True) -> list[Path]:
    ensure_vault(vault)
    cfg = load_config(vault)
    ideas_dir = vault / str(cfg.get("ideas_dir", "Ideas"))
    ideas_dir.mkdir(parents=True, exist_ok=True)
    imported: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".md"):
                continue
            raw = archive.read(info).decode("utf-8", errors="replace")
            if split_numbered:
                imported.extend(_write_split_numbered(raw, ideas_dir, info.filename))
            else:
                imported.append(_write_single(raw, ideas_dir, Path(info.filename).stem))
    _record_import(vault, zip_path, imported, split_numbered)
    return imported


def _write_single(markdown: str, ideas_dir: Path, title: str) -> Path:
    clean_title = title.strip() or "Imported idea note"
    path = _unique_path(ideas_dir / f"{slugify(clean_title)}.md")
    body = markdown if markdown.lstrip().startswith("#") else f"# {clean_title}\n\n{markdown}"
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return path


def _write_split_numbered(markdown: str, ideas_dir: Path, fallback_name: str) -> list[Path]:
    relation_summary = _extract_relation_summary(markdown)
    ideas_section = _extract_ideas_section(markdown) or markdown
    entries = _parse_numbered_entries(ideas_section)
    if not entries:
        return [_write_single(markdown, ideas_dir, Path(fallback_name).stem)]
    imported: list[Path] = []
    for number, title, body in entries:
        clean_title = _display_title(title, body)
        filename = f"{number:03d}-{slugify(clean_title)[:70]}.md"
        path = _unique_path(ideas_dir / filename)
        related = _related_lines(number, relation_summary)
        parts = [f"# {clean_title}", "", f"Imported idea number: {number}"]
        if body.strip():
            parts.extend(["", body.rstrip()])
        if related:
            parts.extend(["", "## Relations", "", *related])
        path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
        imported.append(path)
    return imported


def _extract_relation_summary(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    output: list[str] = []
    in_summary = False
    for line in lines:
        if re.match(r"^###\s+Relation summary", line, flags=re.I):
            in_summary = True
            continue
        if in_summary and re.match(r"^---\s*$", line):
            break
        if in_summary and line.strip().startswith("-"):
            output.append(line.strip())
    return output


def _extract_ideas_section(markdown: str) -> str | None:
    match = re.search(r"^###\s+Ideas\s*$", markdown, flags=re.I | re.M)
    if not match:
        return None
    return markdown[match.end() :].strip()


def _parse_numbered_entries(markdown: str) -> list[tuple[int, str, str]]:
    lines = markdown.splitlines()
    entries: list[tuple[int, str, list[str]]] = []
    current: tuple[int, str, list[str]] | None = None
    for line in lines:
        match = re.match(r"^(\d+)\.\s+(.+?)\s*$", line)
        if match:
            if current:
                entries.append(current)
            current = (int(match.group(1)), match.group(2).strip(), [])
        elif current:
            current[2].append(line)
    if current:
        entries.append(current)
    return [(number, title, "\n".join(body).strip()) for number, title, body in entries]


def _display_title(title: str, body: str) -> str:
    clean_title = title.strip().rstrip(".")
    if not clean_title.lower().startswith("relate "):
        return clean_title
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and not stripped.lower().startswith("- related:"):
            return stripped[2:].strip().rstrip(".") or clean_title
    return clean_title


def _related_lines(number: int, relation_summary: list[str]) -> list[str]:
    needle = str(number)
    return [line for line in relation_summary if re.search(rf"\b{re.escape(needle)}\b", line)]


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free filename for {path}")


def _record_import(vault: Path, zip_path: Path, imported: list[Path], split_numbered: bool) -> None:
    try:
        source_hash = sha256_text(zip_path.read_bytes().hex())
    except OSError:
        source_hash = ""
    record = {
        "source": str(zip_path),
        "source_sha256": source_hash,
        "split_numbered": split_numbered,
        "imported_count": len(imported),
        "imported": [path.relative_to(vault).as_posix() for path in imported],
        "created_at": iso_now(),
    }
    with imports_path(vault).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
