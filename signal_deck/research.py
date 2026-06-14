from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import state
from .config import signal_dir, load_config, load_sources
from .render import render_dashboards
from .scoring import discovery_score
from .util import iso_now, sha256_text, stable_float, strip_html
from .vault import Idea, ensure_vault, scan_ideas


@dataclass
class Candidate:
    source_type: str
    title: str
    url: str
    summary: str
    published_at: str | None = None
    channel: str | None = None
    citations: list[dict[str, str]] = field(default_factory=list)
    source_id: str | None = None
    image_url: str = ""


def run_refresh(vault: Path, kind: str = "manual", idea_filter: str | None = None) -> dict[str, Any]:
    ensure_vault(vault)
    cfg = load_config(vault)
    sources = load_sources(vault)
    conn = state.connect(vault)
    run_id = state.record_run_start(conn, kind)
    try:
        ideas = scan_ideas(vault, cfg)
        if idea_filter:
            needle = idea_filter.lower()
            ideas = [
                idea
                for idea in ideas
                if needle in idea.id.lower() or needle in idea.rel_path.lower() or needle in idea.title.lower()
            ]
        for idea in ideas:
            state.upsert_idea(conn, idea)
        conn.commit()
        all_ideas = list(ideas)
        ideas = _research_batch(conn, ideas, cfg, kind) if not idea_filter else ideas
        seed_urls = _youtube_seed_urls(sources)
        state.delete_discoveries_by_urls(conn, seed_urls)
        seen_urls = state.discovery_urls(conn) | set(seed_urls)
        candidates = collect_candidates(vault, cfg, sources, ideas, seen_urls, all_ideas)
        state.delete_obsolete_discoveries(conn)
        _prune_generic_media(conn, cfg, ideas)
        inserted = attach_candidates(conn, cfg, ideas, candidates)
        message = f"{len(ideas)} ideas scanned, {inserted} discoveries updated"
        state.record_run_finish(conn, run_id, "ok", message)
        render_dashboards(vault)
        return {"status": "ok", "ideas": len(ideas), "discoveries": inserted, "message": message}
    except Exception as exc:  # pragma: no cover - defensive boundary for server/scheduler
        state.record_run_finish(conn, run_id, "error", str(exc))
        raise
    finally:
        conn.close()


def run_codex_agent_test(vault: Path, idea_filter: str | None = None) -> dict[str, Any]:
    ensure_vault(vault)
    cfg = load_config(vault)
    cfg.setdefault("providers", {})["mode"] = "codex"
    conn = state.connect(vault)
    run_id = state.record_run_start(conn, "codex-agent-test")
    try:
        ideas = scan_ideas(vault, cfg)
        if idea_filter:
            needle = idea_filter.lower()
            ideas = [
                idea
                for idea in ideas
                if needle in idea.id.lower() or needle in idea.rel_path.lower() or needle in idea.title.lower()
            ]
        if not ideas:
            message = "No matching ideas found"
            state.record_run_finish(conn, run_id, "error", message)
            return {"status": "error", "message": message, "discoveries": 0}
        for idea in scan_ideas(vault, cfg):
            state.upsert_idea(conn, idea)
        conn.commit()
        idea = sorted(ideas, key=lambda item: item.modified_at, reverse=True)[0]
        candidates = fetch_codex_agent_research(vault, cfg, idea)
        inserted = attach_candidates(conn, cfg, [idea], candidates)
        message = f"Codex agent tested on {idea.title}: {inserted} discoveries updated"
        state.record_run_finish(conn, run_id, "ok", message)
        render_dashboards(vault)
        return {
            "status": "ok",
            "idea": idea.title,
            "idea_id": idea.id,
            "discoveries": inserted,
            "message": message,
        }
    except Exception as exc:  # pragma: no cover - defensive CLI/server boundary
        state.record_run_finish(conn, run_id, "error", str(exc))
        raise
    finally:
        conn.close()


def collect_candidates(
    vault: Path,
    cfg: dict[str, Any],
    sources: dict[str, Any],
    ideas: list[Idea],
    seen_urls: set[str] | None = None,
    all_ideas: list[Idea] | None = None,
) -> list[Candidate]:
    research_cfg = cfg.get("research", {})
    if not research_cfg.get("enabled", True):
        return []
    limit = int(research_cfg.get("max_candidates_per_source", 8))
    candidates: list[Candidate] = []
    seen_urls = seen_urls or set()
    all_ideas = all_ideas or ideas
    if research_cfg.get("local_ideas", True):
        candidates.extend(collect_local_idea_candidates(all_ideas))
    if research_cfg.get("rss", True):
        for url in sources.get("rss", []) or []:
            candidates.extend(fetch_rss(str(url), limit))
    if research_cfg.get("arxiv", True):
        for query in sources.get("arxiv_queries", []) or []:
            candidates.extend(fetch_arxiv(str(query), limit))
    if research_cfg.get("youtube", True):
        for idea in ideas:
            candidates.extend(fetch_youtube_metadata(cfg, sources, limit, [idea], seen_urls))
    if should_use_codex(cfg):
        top_ideas = sorted(ideas, key=lambda idea: idea.modified_at, reverse=True)[
            : int(cfg.get("providers", {}).get("codex", {}).get("max_ideas_per_run", 2))
        ]
        for idea in top_ideas:
            candidates.extend(fetch_codex_agent_research(vault, cfg, idea))
    if should_use_ollama(cfg):
        top_ideas = sorted(ideas, key=lambda idea: idea.modified_at, reverse=True)[
            : int(cfg.get("providers", {}).get("ollama", {}).get("max_ideas_per_run", 4))
        ]
        for idea in top_ideas:
            candidates.extend(fetch_ollama_reflection(cfg, idea))
    if should_use_openai(cfg):
        top_ideas = sorted(ideas, key=lambda idea: idea.modified_at, reverse=True)[
            : int(cfg.get("providers", {}).get("openai", {}).get("max_ideas_per_run", 3))
        ]
        for idea in top_ideas:
            candidates.extend(fetch_openai_research(cfg, idea))
    return dedupe_candidates(candidates)


def _research_batch(conn: sqlite3.Connection, ideas: list[Idea], cfg: dict[str, Any], kind: str) -> list[Idea]:
    if kind not in {"nightly", "reactive"}:
        return ideas
    limit_key = "nightly_ideas_per_run" if kind == "nightly" else "reactive_ideas_per_run"
    limit = int(cfg.get("research", {}).get(limit_key, 12 if kind == "nightly" else 6))
    if limit <= 0 or len(ideas) <= limit:
        return ideas
    counts = {
        str(row["idea_id"]): (int(row["count"] or 0), str(row["latest"] or ""))
        for row in conn.execute(
            """
            SELECT idea_id, COUNT(*) AS count, MAX(updated_at) AS latest
            FROM discoveries
            GROUP BY idea_id
            """
        )
    }

    def key(idea: Idea) -> tuple[int, float, float]:
        count, latest = counts.get(idea.id, (0, ""))
        has_no_research = 1 if count == 0 else 0
        try:
            latest_score = -datetime.fromisoformat(latest).timestamp() if latest else 0.0
        except ValueError:
            latest_score = 0.0
        return (has_no_research, latest_score, idea.modified_at)

    return sorted(ideas, key=key, reverse=True)[:limit]


def _prune_generic_media(conn: sqlite3.Connection, cfg: dict[str, Any], ideas: list[Idea]) -> int:
    threshold = float(cfg.get("research", {}).get("media_min_specificity", 0.34))
    deleted: list[int] = []
    for idea in ideas:
        for row in state.list_discoveries(conn, idea.id):
            if not _discovery_is_visual_media(row):
                continue
            candidate = Candidate(
                str(row["source_type"]),
                str(row["title"]),
                str(row["url"]),
                str(row["summary"]),
                image_url=str(row["image_url"] or ""),
            )
            specificity, _why = _media_specificity(idea, candidate)
            if specificity < threshold:
                deleted.append(int(row["id"]))
    state.delete_discoveries_by_ids(conn, deleted)
    return len(deleted)


def _discovery_is_visual_media(row: sqlite3.Row) -> bool:
    source = str(row["source_type"]).lower()
    url = str(row["url"]).lower()
    title = str(row["title"]).lower()
    image_url = str(row["image_url"] or "")
    if source == "manual" and url.startswith("ideas/") and url.endswith(".md"):
        return False
    return (
        source == "youtube"
        or "youtube.com/watch" in url
        or "youtu.be/" in url
        or bool(image_url)
        or source == "arxiv"
        or "arxiv.org" in url
        or url.endswith(".pdf")
        or "paper" in title
    )


def attach_candidates(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    ideas: list[Idea],
    candidates: list[Candidate],
) -> int:
    if not ideas or not candidates:
        return 0
    research_cfg = cfg.get("research", {})
    source_weights = cfg.get("source_weights", {})
    focus_terms = [str(term) for term in cfg.get("focus_terms", []) or []]
    min_relevance = float(research_cfg.get("min_relevance", 0.08))
    max_items = int(research_cfg.get("max_items_per_idea", 4))
    wildcard_rate = float(cfg.get("ranking", {}).get("wildcard_rate", 0.12))
    inserted = 0
    for idea in ideas:
        scored: list[tuple[float, float, str, Candidate, bool]] = []
        for candidate in candidates:
            if candidate.source_id == idea.id:
                continue
            score, novelty, why = discovery_score(idea.user_text, candidate, source_weights, focus_terms)
            direct_agent = candidate.source_id in {f"codex:{idea.id}", f"ollama:{idea.id}"}
            target_idea = _candidate_target_idea(candidate)
            if target_idea and target_idea != idea.id:
                continue
            visual_media = candidate.source_type == "youtube" or bool(candidate.image_url)
            if direct_agent:
                score = max(score, 0.55)
                why = "Generated directly for this idea by the configured agent."
            if visual_media:
                specificity, specificity_why = _media_specificity(idea, candidate)
                media_threshold = float(research_cfg.get("media_min_specificity", 0.34))
                if specificity < media_threshold:
                    continue
                score = _media_boosted_score(candidate, score, specificity)
                if candidate.source_type == "youtube":
                    why = specificity_why
            wildcard = stable_float(idea.id, candidate.url or candidate.title, iso_now()[:10]) < wildcard_rate
            if direct_agent or visual_media or score >= min_relevance or (wildcard and not visual_media):
                scored.append((score, novelty, why, candidate, wildcard and score < min_relevance))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for score, novelty, why, candidate, wildcard in scored[:max_items]:
            url = candidate.url or f"signal://{sha256_text(candidate.title + candidate.summary)[:16]}"
            discovery = {
                "idea_id": idea.id,
                "source_type": candidate.source_type,
                "title": candidate.title,
                "url": url,
                "summary": candidate.summary,
                "why": "Wildcard spark: weak direct overlap, strong novelty." if wildcard else why,
                "score": score,
                "novelty": novelty,
                "is_wildcard": wildcard,
                "image_url": candidate.image_url,
                "citations": candidate.citations,
            }
            state.add_discovery(conn, discovery)
            inserted += 1
    return inserted


def collect_local_idea_candidates(ideas: list[Idea]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for idea in ideas:
        summary = _idea_summary(idea.user_text)
        candidates.append(
            Candidate(
                "manual",
                f"Related idea: {idea.title}",
                idea.rel_path,
                f"{summary} Note: {idea.rel_path}",
                source_id=idea.id,
            )
        )
    return candidates


def _idea_summary(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
        if len(" ".join(lines)) > 520:
            break
    return " ".join(lines)[:700]


def fetch_rss(url: str, limit: int) -> list[Candidate]:
    try:
        root = ET.fromstring(_http_get(url, timeout=10))
    except Exception:
        return []
    candidates: list[Candidate] = []
    for item in root.findall(".//item")[:limit]:
        title = _child_text(item, "title")
        link = _child_text(item, "link")
        summary = _child_text(item, "description") or _child_text(item, "summary")
        if title:
            candidates.append(Candidate("rss", title, link, strip_html(summary)[:900], image_url=_preview_image_url(link)))
    if candidates:
        return candidates
    for entry in _children_by_local(root, "entry")[:limit]:
        title = _child_text(entry, "title")
        link = _entry_link(entry)
        summary = _child_text(entry, "summary") or _child_text(entry, "content")
        published = _child_text(entry, "published") or _child_text(entry, "updated")
        if title:
            candidates.append(Candidate("rss", title, link, strip_html(summary)[:900], published, image_url=_preview_image_url(link)))
    return candidates


def fetch_arxiv(query: str, limit: int) -> list[Candidate]:
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": min(limit, 12),
        }
    )
    url = f"https://export.arxiv.org/api/query?{params}"
    try:
        root = ET.fromstring(_http_get(url, timeout=12))
    except Exception:
        return []
    candidates: list[Candidate] = []
    for entry in _children_by_local(root, "entry")[:limit]:
        title = " ".join((_child_text(entry, "title") or "").split())
        summary = " ".join((_child_text(entry, "summary") or "").split())
        link = _entry_link(entry)
        published = _child_text(entry, "published")
        if title:
            candidates.append(Candidate("arxiv", title, link, summary[:1200], published, image_url=_image_from_text(summary)))
    return candidates


def fetch_youtube_metadata(
    cfg: dict[str, Any],
    sources: dict[str, Any],
    limit: int,
    ideas: list[Idea] | None = None,
    seen_urls: set[str] | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen_urls = seen_urls or set()
    seed_urls = set(_youtube_seed_urls(sources))
    excluded_urls = seen_urls | seed_urls
    api_key_env = cfg.get("research", {}).get("youtube_api_key_env", "YOUTUBE_API_KEY")
    api_key = os.environ.get(str(api_key_env)) or os.environ.get("YOUTUBE_API_KEY")
    if api_key:
        for query in sources.get("youtube_queries", []) or []:
            candidates.extend(_exclude_urls(_youtube_search(str(query), api_key, limit), excluded_urls))
        for idea_id, query in _seed_video_queries(cfg, sources, ideas or []):
            candidates.extend(
                _exclude_urls(_youtube_search(query, api_key, min(limit, 5), f"youtube-query:{idea_id}"), excluded_urls)
            )
    else:
        for idea_id, query in _seed_video_queries(cfg, sources, ideas or []):
            candidates.extend(_youtube_search_web(query, min(limit, 5), excluded_urls, f"youtube-query:{idea_id}"))
    for channel_id in sources.get("youtube_channel_ids", []) or []:
        feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(str(channel_id))}"
        candidates.extend(_exclude_urls(_youtube_channel_feed(feed, limit), excluded_urls))
    return candidates


def _youtube_search(query: str, api_key: str, limit: int, source_id: str = "") -> list[Candidate]:
    params = urllib.parse.urlencode(
        {
            "part": "snippet",
            "type": "video",
            "maxResults": min(limit, 10),
            "q": query,
            "key": api_key,
        }
    )
    url = f"https://www.googleapis.com/youtube/v3/search?{params}"
    try:
        payload = json.loads(_http_get(url, timeout=10))
    except Exception:
        return []
    candidates: list[Candidate] = []
    for item in payload.get("items", [])[:limit]:
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId")
        if not video_id:
            continue
        title = snippet.get("title", "")
        description = strip_html(snippet.get("description", ""))
        channel = snippet.get("channelTitle", "")
        published = snippet.get("publishedAt")
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        summary = f"{description} Channel: {channel}".strip()
        thumb = _youtube_thumbnail_url(video_id)
        candidates.append(
            Candidate("youtube", title, video_url, summary[:900], published, channel, source_id=source_id, image_url=thumb)
        )
    return candidates


def _youtube_search_web(query: str, limit: int, excluded_urls: set[str], source_id: str = "") -> list[Candidate]:
    params = urllib.parse.urlencode({"search_query": query, "sp": "EgIQAQ%253D%253D"})
    url = f"https://www.youtube.com/results?{params}"
    try:
        html_text = _http_get(url, timeout=12)
    except Exception:
        return []
    payload = _youtube_initial_data(html_text)
    if not payload:
        return []
    candidates: list[Candidate] = []
    seen_ids: set[str] = set()
    for renderer in _walk_video_renderers(payload):
        video_id = str(renderer.get("videoId") or "")
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        if video_url in excluded_urls:
            continue
        title = _yt_text(renderer.get("title")) or "YouTube video"
        channel = _yt_text(renderer.get("ownerText")) or _yt_text(renderer.get("shortBylineText"))
        published = _yt_text(renderer.get("publishedTimeText"))
        summary_bits = [f"Found by nightly video search: {query}."]
        if channel:
            summary_bits.append(f"Channel: {channel}.")
        if published:
            summary_bits.append(f"Published: {published}.")
        candidates.append(
            Candidate(
                "youtube",
                title[:220],
                video_url,
                " ".join(summary_bits)[:900],
                published,
                channel,
                source_id=source_id,
                image_url=_youtube_renderer_thumbnail(renderer, video_id),
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def _youtube_initial_data(text: str) -> dict[str, Any] | None:
    match = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script", text, flags=re.DOTALL)
    if not match:
        match = re.search(r"ytInitialData\s*=\s*(\{.*?\});", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _walk_video_renderers(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            renderer = current.get("videoRenderer")
            if isinstance(renderer, dict):
                found.append(renderer)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _yt_text(value: Any) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return str(value["simpleText"])
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text") or "") for run in runs if isinstance(run, dict)).strip()
    return ""


def _youtube_renderer_thumbnail(renderer: dict[str, Any], video_id: str) -> str:
    thumbnails = renderer.get("thumbnail", {}).get("thumbnails", [])
    if isinstance(thumbnails, list) and thumbnails:
        url = str(thumbnails[-1].get("url") or "")
        if url.startswith("http"):
            return url.split("?", 1)[0]
    return _youtube_thumbnail_url(video_id)


def _youtube_channel_feed(url: str, limit: int) -> list[Candidate]:
    try:
        root = ET.fromstring(_http_get(url, timeout=10))
    except Exception:
        return []
    candidates: list[Candidate] = []
    for entry in _children_by_local(root, "entry")[:limit]:
        title = _child_text(entry, "title")
        video_id = _child_text(entry, "videoId")
        published = _child_text(entry, "published")
        author = _child_text(entry, "name")
        link = _entry_link(entry) or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
        if title:
            candidates.append(
                Candidate("youtube", title, link, f"Channel: {author}", published, author, image_url=_youtube_thumbnail_url(video_id))
            )
    return candidates


def _youtube_url_candidate(url: str) -> Candidate | None:
    video_id = _youtube_video_id(url)
    if not video_id:
        return None
    normalized = f"https://www.youtube.com/watch?v={video_id}"
    title = "YouTube video"
    summary = "Seed video added to prioritize visual research examples."
    published = None
    channel = None
    oembed = f"https://www.youtube.com/oembed?{urllib.parse.urlencode({'url': normalized, 'format': 'json'})}"
    try:
        payload = json.loads(_http_get(oembed, timeout=8))
        title = str(payload.get("title") or title)
        channel = str(payload.get("author_name") or "")
        summary = f"Video seed from {channel}.".strip()
    except Exception:
        pass
    return Candidate(
        "youtube",
        title,
        normalized,
        summary,
        published,
        channel,
        source_id=f"youtube:{video_id}",
        image_url=_youtube_thumbnail_url(video_id),
    )


def _youtube_seed_urls(sources: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for url in sources.get("youtube_urls", []) or []:
        video_id = _youtube_video_id(str(url))
        if video_id:
            urls.append(f"https://www.youtube.com/watch?v={video_id}")
    return urls


def _seed_video_queries(cfg: dict[str, Any], sources: dict[str, Any], ideas: list[Idea]) -> list[tuple[str, str]]:
    max_searches = int(cfg.get("research", {}).get("max_video_searches", 6))
    seed_candidates = [_youtube_url_candidate(str(url)) for url in sources.get("youtube_urls", []) or []]
    seed_candidates = [candidate for candidate in seed_candidates if candidate]
    queries: list[tuple[str, str]] = []
    top_ideas = sorted(ideas, key=lambda idea: idea.modified_at, reverse=True)[: max(3, max_searches)]
    for idea in top_ideas:
        idea_text = getattr(idea, "user_text", idea.title)
        idea_id = getattr(idea, "id", "")
        for phrase in [
            f"{idea.title} {_specific_query_terms(idea_text)} prototype",
            f"{idea.title} {_specific_query_terms(idea_text)} build test",
        ]:
            _append_query(queries, idea_id, phrase, max_searches)
    for seed in seed_candidates:
        seed_terms = _video_seed_terms(seed)
        for phrase in [
            f"{seed_terms} similar mechanism build",
            f"{seed_terms} prototype test",
            f"{seed_terms} 3d printed gearbox mechanism",
        ]:
            _append_query(queries, "", phrase, max_searches)
        for idea in top_ideas[:3]:
            idea_text = getattr(idea, "user_text", idea.title)
            idea_id = getattr(idea, "id", "")
            _append_query(queries, idea_id, f"{idea.title} {_specific_query_terms(idea_text)} {seed_terms}", max_searches)
    for query in sources.get("youtube_queries", []) or []:
        _append_query(queries, "", str(query), max_searches)
    return queries


def _video_seed_terms(candidate: Candidate) -> str:
    text = f"{candidate.title} {candidate.channel or ''}"
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    stop = {"the", "and", "with", "from", "this", "that", "tested", "printed", "high"}
    useful = [word for word in words if len(word) > 2 and word not in stop]
    return " ".join(useful[:8]) or candidate.title


def _append_query(queries: list[tuple[str, str]], idea_id: str, query: str, max_items: int) -> None:
    clean = " ".join(query.split()).strip()
    if clean and (idea_id, clean) not in queries and len(queries) < max_items:
        queries.append((idea_id, clean))


def _exclude_urls(candidates: list[Candidate], excluded_urls: set[str]) -> list[Candidate]:
    return [candidate for candidate in candidates if candidate.url not in excluded_urls]


def _youtube_video_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if "youtube.com" in parsed.netloc:
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("v"):
            return query["v"][0]
        match = re.search(r"/(?:shorts|embed)/([^/?#]+)", parsed.path)
        if match:
            return match.group(1)
    return None


def _youtube_thumbnail_url(video_id: str | None) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def _image_from_text(text: str) -> str:
    match = re.search(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]+)", text)
    if match:
        return _youtube_thumbnail_url(match.group(1))
    return ""


def _preview_image_url(url: str) -> str:
    if not url.startswith("http"):
        return ""
    try:
        data = _http_get(url, timeout=5)[:200000]
    except Exception:
        return ""
    for pattern in [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]:
        match = re.search(pattern, data, flags=re.IGNORECASE)
        if match:
            return urllib.parse.urljoin(url, html_unescape(match.group(1)))
    return ""


def html_unescape(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#x27;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def should_use_openai(cfg: dict[str, Any]) -> bool:
    providers = cfg.get("providers", {})
    if providers.get("mode") != "openai":
        return False
    if not cfg.get("research", {}).get("openai_web", True):
        return False
    key_env = providers.get("openai", {}).get("api_key_env", "OPENAI_API_KEY")
    return bool(os.environ.get(str(key_env)))


def should_use_codex(cfg: dict[str, Any]) -> bool:
    providers = cfg.get("providers", {})
    if providers.get("mode") != "codex":
        return False
    if not cfg.get("research", {}).get("codex_agent", True):
        return False
    return bool(providers.get("codex", {}).get("enabled", True))


def fetch_codex_agent_research(vault: Path, cfg: dict[str, Any], idea: Idea) -> list[Candidate]:
    codex_cfg = cfg.get("providers", {}).get("codex", {})
    run_dir = signal_dir(vault) / "codex_runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    schema_path = run_dir / "signal_deck_codex_schema.json"
    output_path = run_dir / f"{sha256_text(idea.id)[:16]}.json"
    schema_path.write_text(json.dumps(_codex_schema(), indent=2), encoding="utf-8")

    command = _resolve_codex_command(str(codex_cfg.get("command", "codex")))
    args = [command]
    if codex_cfg.get("search", True):
        args.append("--search")
    args.extend(
        [
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        str(codex_cfg.get("sandbox", "read-only")),
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "-C",
        str(vault),
        ]
    )
    if codex_cfg.get("ephemeral", True):
        args.append("--ephemeral")
    model = str(codex_cfg.get("model", "") or "").strip()
    if model:
        args.extend(["--model", model])
    args.append(_codex_prompt(idea, load_sources(vault)))

    try:
        completed = subprocess.run(
            args,
            cwd=str(vault),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(codex_cfg.get("timeout_seconds", 180)),
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return []
    if completed.returncode != 0:
        _write_codex_error(run_dir, idea, completed)
        return []
    payload = _load_codex_payload(output_path, completed.stdout)
    items = payload.get("items", []) if isinstance(payload, dict) else []
    candidates: list[Candidate] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Codex research signal").strip()
        summary = str(item.get("summary") or "").strip()
        why = str(item.get("why") or "").strip()
        url = str(item.get("url") or "").strip()
        image_url = str(item.get("image_url") or "").strip()
        if not summary and not why:
            continue
        if not url:
            url = f"signal://codex/{sha256_text(idea.id + title + summary)[:16]}"
        candidates.append(
            Candidate(
                "codex",
                title[:220],
                url[:1000],
                f"{summary}\n\nWhy: {why}".strip()[:1400],
                citations=[{"url": url, "title": title}] if url.startswith("http") else [],
                source_id=f"codex:{idea.id}",
                image_url=image_url[:1000],
            )
        )
    return candidates


def _codex_prompt(idea: Idea, sources: dict[str, Any] | None = None) -> str:
    seed_context = _codex_seed_context(sources or {})
    return (
        "You are the Signal Deck research agent for a private Obsidian invention vault. "
        "Do not edit files. Do not run commands unless necessary. Use web search if useful. "
        "For the idea below, produce 3 to 5 high-signal research/reflection items. "
        "Prioritize new useful videos, build logs with photos, papers with figures, patents, concrete mechanisms, terms to search, and surprising adjacent domains. "
        "Avoid returning any seed video verbatim; seeds are taste examples only. "
        "When a result has a YouTube thumbnail, paper figure, website Open Graph image, or other representative image URL, include it as image_url. "
        "Each item must help the inventor test or connect the idea. Be concise.\n\n"
        f"{seed_context}"
        f"Idea title: {idea.title}\n"
        f"Idea note path: {idea.rel_path}\n\n"
        f"Idea text:\n{idea.user_text[:3500]}"
    )


def _codex_seed_context(sources: dict[str, Any]) -> str:
    seeds = []
    for url in sources.get("youtube_urls", []) or []:
        candidate = _youtube_url_candidate(str(url))
        if candidate:
            seeds.append(f"- {candidate.title} ({candidate.url})")
    if not seeds:
        return ""
    return (
        "Taste-example videos already seen by the user. Find new material with this kind of concrete build/test energy, "
        "but do not return these URLs:\n"
        + "\n".join(seeds[:5])
        + "\n\n"
    )


def _resolve_codex_command(command: str) -> str:
    if os.name == "nt" and command == "codex":
        resolved = shutil.which("codex.cmd")
        if resolved:
            return resolved
    return command


def _codex_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "summary": {"type": "string"},
                        "why": {"type": "string"},
                        "image_url": {"type": "string"},
                    },
                    "required": ["title", "url", "summary", "why", "image_url"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _load_codex_payload(output_path: Path, stdout: str) -> dict[str, Any]:
    candidates = []
    if output_path.exists():
        candidates.append(output_path.read_text(encoding="utf-8", errors="replace"))
    candidates.append(stdout)
    for text in candidates:
        parsed = _parse_json_object(text)
        if parsed:
            return parsed
    return {}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _write_codex_error(run_dir: Path, idea: Idea, completed: subprocess.CompletedProcess[str]) -> None:
    path = run_dir / f"{sha256_text(idea.id)[:16]}.error.txt"
    path.write_text(
        f"returncode={completed.returncode}\n\nSTDOUT\n{completed.stdout}\n\nSTDERR\n{completed.stderr}",
        encoding="utf-8",
    )


def should_use_ollama(cfg: dict[str, Any]) -> bool:
    providers = cfg.get("providers", {})
    if providers.get("mode") != "local":
        return False
    if not cfg.get("research", {}).get("ollama_reflections", True):
        return False
    return bool(providers.get("ollama", {}).get("enabled", True))


def fetch_ollama_reflection(cfg: dict[str, Any], idea: Idea) -> list[Candidate]:
    ollama_cfg = cfg.get("providers", {}).get("ollama", {})
    base_url = str(ollama_cfg.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
    model = str(ollama_cfg.get("model", "llama3.1"))
    timeout = int(ollama_cfg.get("timeout_seconds", 8))
    prompt = (
        "You are helping a mechanical inventor connect ideas. "
        "Give one compact research direction, one weird adjacent domain, and one next experiment. "
        "No hype. No long paragraphs.\n\n"
        f"Idea: {idea.title}\n\n{idea.user_text[:2200]}"
    )
    payload = {"model": model, "prompt": prompt, "stream": False}
    request = urllib.request.Request(
        f"{base_url}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    text = str(data.get("response") or "").strip()
    if not text:
        return []
    return [
        Candidate(
            "ollama",
            f"Local reflection: {idea.title}",
            f"signal://ollama/{sha256_text(idea.id + text)[:16]}",
            text[:1400],
            source_id=f"ollama:{idea.id}",
        )
    ]


def fetch_openai_research(cfg: dict[str, Any], idea: Idea) -> list[Candidate]:
    providers = cfg.get("providers", {})
    openai_cfg = providers.get("openai", {})
    api_key = os.environ.get(str(openai_cfg.get("api_key_env", "OPENAI_API_KEY")))
    if not api_key:
        return []
    model = str(openai_cfg.get("model", "gpt-5.5"))
    prompt = (
        "Find 3 high-signal, non-obvious web sources for this creative invention idea. "
        "Prefer primary docs, papers, build logs, or niche technical writing. "
        "Return terse bullets with why each source matters.\n\n"
        f"Idea title: {idea.title}\n\nIdea text:\n{idea.user_text[:3000]}"
    )
    payload = {
        "model": model,
        "tools": [{"type": "web_search"}],
        "input": prompt,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    text, citations = parse_openai_response(data)
    if not text:
        return []
    url = citations[0]["url"] if citations else f"signal://openai/{sha256_text(idea.id + text)[:16]}"
    return [
        Candidate(
            "openai",
            f"Research pack: {idea.title}",
            url,
            text[:1800],
            citations=citations,
        )
    ]


def parse_openai_response(data: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    texts: list[str] = []
    citations: list[dict[str, str]] = []
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            text = content.get("text") or content.get("output_text") or ""
            if text:
                texts.append(text)
            for annotation in content.get("annotations", []) or []:
                citation = annotation.get("url_citation") if "url_citation" in annotation else annotation
                url = citation.get("url") if isinstance(citation, dict) else None
                title = citation.get("title") if isinstance(citation, dict) else None
                if url:
                    citations.append({"url": str(url), "title": str(title or url)})
    return "\n\n".join(texts).strip(), citations


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    output: list[Candidate] = []
    for candidate in candidates:
        key = candidate.url or candidate.title
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(candidate)
    return output


def _http_get(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "SignalDeck/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


_MEDIA_STOPWORDS = {
    "about",
    "added",
    "and",
    "are",
    "being",
    "build",
    "channel",
    "concept",
    "design",
    "example",
    "found",
    "from",
    "gear",
    "gearbox",
    "idea",
    "ideas",
    "into",
    "machine",
    "mechanical",
    "mechanism",
    "model",
    "nightly",
    "note",
    "need",
    "needs",
    "part",
    "printed",
    "printing",
    "project",
    "prototype",
    "research",
    "search",
    "source",
    "somehow",
    "something",
    "test",
    "that",
    "the",
    "this",
    "through",
    "together",
    "understand",
    "understanding",
    "using",
    "what",
    "why",
    "video",
    "walking",
    "with",
}

_BODY_MEDIA_STOPWORDS = _MEDIA_STOPWORDS | {
    "animation",
    "cad",
    "change",
    "driven",
    "engineering",
    "leg",
    "length",
    "motion",
    "moving",
    "part",
    "parts",
    "robot",
    "robots",
    "stride",
}


def _candidate_target_idea(candidate: Candidate) -> str:
    prefix = "youtube-query:"
    source_id = candidate.source_id or ""
    if source_id.startswith(prefix):
        return source_id[len(prefix) :]
    return ""


def _specific_query_terms(text: str) -> str:
    terms = _specific_terms(text)
    return " ".join(terms[:6])


def _media_specificity(idea: Idea, candidate: Candidate) -> tuple[float, str]:
    candidate_text = _candidate_specificity_text(candidate)
    candidate_terms = set(_specific_terms(candidate_text))
    title_terms = _specific_terms(idea.title)
    body_terms = _specific_terms(idea.user_text, _BODY_MEDIA_STOPWORDS)
    title_hits = [term for term in title_terms if term in candidate_terms]
    body_hits = [term for term in body_terms if term in candidate_terms and term not in title_hits]
    title_phrase_hits = _phrase_hits(idea.title, candidate_text)
    body_phrase_hits = _phrase_hits(idea.user_text, candidate_text, _BODY_MEDIA_STOPWORDS)
    target_bonus = 0.12 if _candidate_target_idea(candidate) == idea.id else 0.0
    specificity = min(
        1.0,
        target_bonus
        + 0.16 * min(len(title_hits), 4)
        + 0.08 * min(len(body_hits), 4)
        + 0.22 * min(len(title_phrase_hits), 2)
        + 0.10 * min(len(body_phrase_hits), 2),
    )
    title_term_count = len(title_terms)
    strong_title_match = bool(title_phrase_hits) or len(title_hits) >= min(2, title_term_count)
    if title_term_count >= 2 and not strong_title_match:
        specificity = min(specificity, 0.30)
    matched = title_hits[:4] + body_hits[:3] + title_phrase_hits[:2] + body_phrase_hits[:1]
    if matched:
        return specificity, "Media kept for this idea because it matches: " + ", ".join(matched) + "."
    return specificity, "Media kept for this idea by a specific search target."


def _candidate_specificity_text(candidate: Candidate) -> str:
    summary = candidate.summary or ""
    if summary.lower().startswith("found by nightly video search:"):
        summary = ""
    return f"{candidate.title}\n{summary}\n{candidate.channel or ''}".lower()


def _specific_terms(text: str, stopwords: set[str] | None = None) -> list[str]:
    stopwords = stopwords or _MEDIA_STOPWORDS
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9\-']{2,}", text.lower()):
        if word in stopwords:
            continue
        if word.isdigit() and len(word) < 3:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def _phrase_hits(text: str, candidate_text: str, stopwords: set[str] | None = None) -> list[str]:
    terms = _specific_terms(text, stopwords)
    hits: list[str] = []
    for size in (3, 2):
        for index in range(0, max(0, len(terms) - size + 1)):
            phrase = " ".join(terms[index : index + size])
            if phrase in candidate_text and phrase not in hits:
                hits.append(phrase)
            if len(hits) >= 2:
                return hits
    return hits


def _media_boosted_score(candidate: Candidate, score: float, specificity: float) -> float:
    floor = 0.42 + 0.34 * specificity
    if candidate.source_type == "youtube":
        return max(score, floor)
    if candidate.image_url and candidate.source_type in {"codex", "openai", "rss"}:
        return max(score, floor - 0.08)
    return score


def _child_text(element: ET.Element, local_name: str) -> str:
    for child in element.iter():
        if _local(child.tag) == local_name:
            return (child.text or "").strip()
    return ""


def _children_by_local(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local(child.tag) == local_name]


def _entry_link(element: ET.Element) -> str:
    for child in element.iter():
        if _local(child.tag) == "link":
            href = child.attrib.get("href")
            if href:
                return href
            if child.text:
                return child.text.strip()
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
