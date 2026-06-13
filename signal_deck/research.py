from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
        candidates = collect_candidates(vault, cfg, sources, ideas)
        state.delete_obsolete_discoveries(conn)
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


def collect_candidates(vault: Path, cfg: dict[str, Any], sources: dict[str, Any], ideas: list[Idea]) -> list[Candidate]:
    research_cfg = cfg.get("research", {})
    if not research_cfg.get("enabled", True):
        return []
    limit = int(research_cfg.get("max_candidates_per_source", 8))
    candidates: list[Candidate] = []
    if research_cfg.get("local_ideas", True):
        candidates.extend(collect_local_idea_candidates(ideas))
    if research_cfg.get("rss", True):
        for url in sources.get("rss", []) or []:
            candidates.extend(fetch_rss(str(url), limit))
    if research_cfg.get("arxiv", True):
        for query in sources.get("arxiv_queries", []) or []:
            candidates.extend(fetch_arxiv(str(query), limit))
    if research_cfg.get("youtube", True):
        candidates.extend(fetch_youtube_metadata(cfg, sources, limit))
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
            if direct_agent:
                score = max(score, 0.55)
                why = "Generated directly for this idea by the configured agent."
            wildcard = stable_float(idea.id, candidate.url or candidate.title, iso_now()[:10]) < wildcard_rate
            if direct_agent or score >= min_relevance or wildcard:
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
            candidates.append(Candidate("rss", title, link, strip_html(summary)[:900]))
    if candidates:
        return candidates
    for entry in _children_by_local(root, "entry")[:limit]:
        title = _child_text(entry, "title")
        link = _entry_link(entry)
        summary = _child_text(entry, "summary") or _child_text(entry, "content")
        published = _child_text(entry, "published") or _child_text(entry, "updated")
        if title:
            candidates.append(Candidate("rss", title, link, strip_html(summary)[:900], published))
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
            candidates.append(Candidate("arxiv", title, link, summary[:1200], published))
    return candidates


def fetch_youtube_metadata(cfg: dict[str, Any], sources: dict[str, Any], limit: int) -> list[Candidate]:
    candidates: list[Candidate] = []
    api_key_env = cfg.get("research", {}).get("youtube_api_key_env", "YOUTUBE_API_KEY")
    api_key = os.environ.get(str(api_key_env)) or os.environ.get("YOUTUBE_API_KEY")
    if api_key:
        for query in sources.get("youtube_queries", []) or []:
            candidates.extend(_youtube_search(str(query), api_key, limit))
    for channel_id in sources.get("youtube_channel_ids", []) or []:
        feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(str(channel_id))}"
        candidates.extend(_youtube_channel_feed(feed, limit))
    return candidates


def _youtube_search(query: str, api_key: str, limit: int) -> list[Candidate]:
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
        candidates.append(Candidate("youtube", title, video_url, summary[:900], published, channel))
    return candidates


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
            candidates.append(Candidate("youtube", title, link, f"Channel: {author}", published, author))
    return candidates


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

    command = str(codex_cfg.get("command", "codex"))
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
    args.append(_codex_prompt(idea))

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
            )
        )
    return candidates


def _codex_prompt(idea: Idea) -> str:
    return (
        "You are the Signal Deck research agent for a private Obsidian invention vault. "
        "Do not edit files. Do not run commands unless necessary. Use web search if useful. "
        "For the idea below, produce 3 to 5 high-signal research/reflection items. "
        "Favor concrete mechanisms, papers, build logs, patents, videos, terms to search, and surprising adjacent domains. "
        "Each item must help the inventor test or connect the idea. Be concise.\n\n"
        f"Idea title: {idea.title}\n"
        f"Idea note path: {idea.rel_path}\n\n"
        f"Idea text:\n{idea.user_text[:3500]}"
    )


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
                    },
                    "required": ["title", "url", "summary", "why"],
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
