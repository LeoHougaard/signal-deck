from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import load_config, load_sources, save_config, save_sources
from .state import connect, record_config_event


def apply_chat_config(vault: Path, text: str) -> dict[str, Any]:
    cfg = load_config(vault)
    sources = load_sources(vault)
    lower = text.lower()
    applied: dict[str, Any] = {}

    time_value = _extract_time(lower)
    if time_value:
        cfg.setdefault("schedule", {})["nightly_time"] = time_value
        applied["nightly_time"] = time_value

    focus_match = re.search(r"focus (?:more )?(?:on|around)\s+(.+)", lower)
    if focus_match:
        focus = _clean_focus(focus_match.group(1))
        if focus:
            terms = [str(term) for term in cfg.get("focus_terms", [])]
            if focus not in terms:
                terms.append(focus)
            cfg["focus_terms"] = terms
            applied["focus_added"] = focus

    if "less youtube" in lower or "reduce youtube" in lower:
        cfg.setdefault("source_weights", {})["youtube"] = max(
            0.1, float(cfg.get("source_weights", {}).get("youtube", 0.75)) * 0.5
        )
        applied["youtube_weight"] = cfg["source_weights"]["youtube"]

    if "more youtube" in lower:
        cfg.setdefault("source_weights", {})["youtube"] = min(
            1.5, float(cfg.get("source_weights", {}).get("youtube", 0.75)) + 0.2
        )
        applied["youtube_weight"] = cfg["source_weights"]["youtube"]

    if "disable youtube" in lower or "stop youtube" in lower:
        cfg.setdefault("research", {})["youtube"] = False
        applied["youtube_enabled"] = False

    if "enable youtube" in lower:
        cfg.setdefault("research", {})["youtube"] = True
        applied["youtube_enabled"] = True

    if "use openai" in lower or "openai mode" in lower:
        cfg.setdefault("providers", {})["mode"] = "openai"
        applied["provider_mode"] = "openai"

    if "use codex" in lower or "codex mode" in lower or "codex plus" in lower:
        cfg.setdefault("providers", {})["mode"] = "codex"
        applied["provider_mode"] = "codex"

    if "use local" in lower or "local mode" in lower or "ollama" in lower:
        cfg.setdefault("providers", {})["mode"] = "local"
        applied["provider_mode"] = "local"

    if "run after edits" in lower or "after i do stuff" in lower or "after changes" in lower:
        cfg.setdefault("schedule", {})["run_after_edits"] = True
        applied["run_after_edits"] = True

    if "do not run after edits" in lower or "don't run after edits" in lower:
        cfg.setdefault("schedule", {})["run_after_edits"] = False
        applied["run_after_edits"] = False

    if "cheap" in lower:
        cfg.setdefault("research", {})["max_candidates_per_source"] = 5
        cfg.setdefault("providers", {}).setdefault("openai", {})["max_ideas_per_run"] = 1
        cfg.setdefault("providers", {}).setdefault("codex", {})["max_ideas_per_run"] = 1
        applied["cost_mode"] = "cheap"

    if "quality" in lower or "deep" in lower:
        cfg.setdefault("research", {})["max_candidates_per_source"] = 12
        cfg.setdefault("providers", {}).setdefault("openai", {})["max_ideas_per_run"] = 5
        cfg.setdefault("providers", {}).setdefault("codex", {})["max_ideas_per_run"] = 3
        applied["cost_mode"] = "quality"

    source_changes = _apply_source_changes(sources, text)
    if source_changes:
        applied["sources"] = source_changes
        save_sources(vault, sources)

    save_config(vault, cfg)
    conn = connect(vault)
    try:
        record_config_event(conn, text, applied)
    finally:
        conn.close()
    return {"applied": applied, "should_refresh": "refresh" in lower or "run now" in lower}


def _extract_time(text: str) -> str | None:
    if "run later" in text:
        return "03:30"
    match = re.search(r"\b(?:at|run at|nightly at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    ampm = match.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _clean_focus(value: str) -> str:
    value = re.split(r"\b(?:and|but|,|\.|;)\b", value)[0]
    return value.strip(" .,:;")[:80]


def _apply_source_changes(sources: dict[str, Any], text: str) -> list[str]:
    changes: list[str] = []
    urls = re.findall(r"https?://[^\s,;]+", text)
    lowered = text.lower()
    for url in urls:
        clean_url = url.rstrip(").]")
        if "rss" in lowered or clean_url.endswith((".xml", ".rss")):
            if _append_unique(sources, "rss", clean_url):
                changes.append(f"rss:{clean_url}")
        elif "youtube" in lowered and "channel" in lowered:
            channel_id = _youtube_channel_id(clean_url)
            if channel_id and _append_unique(sources, "youtube_channel_ids", channel_id):
                changes.append(f"youtube_channel:{channel_id}")
        elif _youtube_video_id(clean_url):
            if _append_unique(sources, "youtube_urls", clean_url):
                changes.append(f"youtube_video:{clean_url}")
        elif _append_unique(sources, "web_seeds", clean_url):
            changes.append(f"web_seed:{clean_url}")
    arxiv_match = re.search(r"(?:add|use)\s+arxiv\s+(.+)", text, flags=re.I)
    if arxiv_match:
        query = arxiv_match.group(1).strip(" .,:;")
        if query and not query.startswith("http") and _append_unique(sources, "arxiv_queries", query):
            changes.append(f"arxiv:{query}")
    return changes


def _append_unique(data: dict[str, Any], key: str, value: str) -> bool:
    values = data.setdefault(key, [])
    if not isinstance(values, list):
        data[key] = values = []
    if value in values:
        return False
    values.append(value)
    return True


def _youtube_channel_id(url: str) -> str | None:
    match = re.search(r"/channel/([^/?#]+)", url)
    if match:
        return match.group(1)
    return None


def _youtube_video_id(url: str) -> str | None:
    if "youtu.be/" in url:
        match = re.search(r"youtu\.be/([^/?#]+)", url)
        return match.group(1) if match else None
    if "youtube.com" in url:
        match = re.search(r"[?&]v=([^&#]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"/(?:shorts|embed)/([^/?#]+)", url)
        if match:
            return match.group(1)
    return None
