from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .util import clamp, parse_iso, stable_float, words


def token_overlap(left: str, right: str) -> float:
    left_words = set(words(left))
    right_words = set(words(right))
    if not left_words or not right_words:
        return 0.0
    overlap = len(left_words & right_words)
    return overlap / math.sqrt(len(left_words) * len(right_words))


def age_score(modified_at: float, half_life_days: float) -> float:
    modified = datetime.fromtimestamp(modified_at, tz=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - modified).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(half_life_days, 0.1))


def discovery_score(
    idea_text: str,
    candidate: Any,
    source_weights: dict[str, float],
    focus_terms: list[str],
) -> tuple[float, float, str]:
    source_type = getattr(candidate, "source_type", "manual")
    title = getattr(candidate, "title", "")
    summary = getattr(candidate, "summary", "")
    combined = f"{title}\n{summary}"
    relevance = token_overlap(idea_text, combined)
    focus_bonus = 0.0
    if focus_terms:
        focus_bonus = max(token_overlap(term, combined) for term in focus_terms)
    novelty = 0.25 + 0.75 * stable_float(getattr(candidate, "url", ""), title)
    source_weight = float(source_weights.get(source_type, 0.7))
    score = clamp((0.62 * relevance) + (0.16 * focus_bonus) + (0.14 * novelty) + (0.08 * source_weight))
    if score < 0.02:
        why = "Low direct overlap, kept only if selected as a wildcard."
    elif focus_bonus > 0.15:
        why = "Matches current focus terms and overlaps with the idea."
    else:
        why = "Overlaps with the idea and adds a different source angle."
    return score, novelty, why


def rank_ideas(
    idea_rows: list[sqlite3.Row],
    discoveries_by_idea: dict[str, list[sqlite3.Row]],
    feedback_by_idea: dict[str, float],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    ranking_cfg = cfg.get("ranking", {})
    half_life = float(ranking_cfg.get("activity_half_life_days", 10))
    weights = {
        "feedback": float(ranking_cfg.get("feedback_weight", 0.28)),
        "relevance": float(ranking_cfg.get("relevance_weight", 0.32)),
        "gem": float(ranking_cfg.get("gem_weight", 0.22)),
        "novelty": float(ranking_cfg.get("novelty_weight", 0.10)),
        "diversity": float(ranking_cfg.get("diversity_weight", 0.08)),
    }
    ranked: list[dict[str, Any]] = []
    for row in idea_rows:
        idea_id = str(row["id"])
        discoveries = discoveries_by_idea.get(idea_id, [])
        activity = age_score(float(row["modified_at"]), half_life)
        relevance = _avg([float(item["score"]) for item in discoveries])
        novelty = _avg([float(item["novelty"]) for item in discoveries])
        diversity = min(1.0, len({str(item["source_type"]) for item in discoveries}) / 4.0)
        feedback_raw = clamp(feedback_by_idea.get(idea_id, 0.0) / 5.0, -1.0, 1.0)
        feedback_display = clamp((feedback_raw + 1.0) / 2.0)
        gem = max([float(item["score"]) for item in discoveries], default=0.0)
        media = [_item for _item in discoveries if _is_media_discovery(_item)]
        research = [_item for _item in discoveries if _item not in media]
        media_count = min(1.0, len(media) / 4.0)
        media_gem = max([float(item["score"]) for item in media], default=0.0)
        media_recency = max([freshness_from_iso(str(item["updated_at"] or item["created_at"] or "")) for item in media], default=0.0)
        research_quality = _avg([float(item["score"]) for item in research])
        media_signal = clamp((0.35 * media_count) + (0.45 * media_gem) + (0.20 * media_recency))
        total = clamp(
            0.20 * activity
            + weights["feedback"] * feedback_raw
            + weights["relevance"] * relevance
            + weights["gem"] * gem
            + weights["novelty"] * novelty
            + weights["diversity"] * diversity
            + 0.18 * media_signal
            + 0.08 * research_quality
        )
        ranked.append(
            {
                "id": idea_id,
                "path": row["path"],
                "title": row["title"],
                "modified_at": row["modified_at"],
                "score": total,
                "activity": activity,
                "feedback": feedback_display,
                "relevance": relevance,
                "novelty": novelty,
                "diversity": diversity,
                "media_score": media_signal,
                "discoveries": discoveries,
            }
        )
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked


def freshness_from_iso(value: str | None) -> float:
    parsed = parse_iso(value)
    if not parsed:
        return 0.45
    age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    return 0.5 ** (age_days / 30.0)


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _is_media_discovery(item: sqlite3.Row) -> bool:
    source = str(item["source_type"]).lower()
    url = str(item["url"]).lower()
    title = str(item["title"]).lower()
    try:
        image_url = str(item["image_url"] or "")
    except (KeyError, IndexError):
        image_url = ""
    if source == "youtube" or "youtube.com/watch" in url or "youtu.be/" in url:
        return True
    if source == "arxiv" or "arxiv.org" in url or url.endswith(".pdf") or "paper" in title:
        return True
    return bool(image_url)
