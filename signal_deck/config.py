from __future__ import annotations

from pathlib import Path
from typing import Any

from .simple_yaml import load_yaml, merge_dict, write_yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "ideas_dir": "Ideas",
    "dashboard_html": "Signal Deck.html",
    "dashboard_md": "Signal Deck.md",
    "agent_block": {
        "start": "<!-- signal-agent:start -->",
        "end": "<!-- signal-agent:end -->",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "schedule": {
        "nightly_time": "02:20",
        "poll_seconds": 20,
        "reactive_seconds": 90,
        "run_after_edits": True,
    },
    "providers": {
        "mode": "local",
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "model": "gpt-5.5",
            "max_ideas_per_run": 3,
        },
        "codex": {
            "enabled": True,
            "command": "codex",
            "model": "",
            "timeout_seconds": 180,
            "max_ideas_per_run": 2,
            "sandbox": "read-only",
            "search": True,
            "ephemeral": True,
        },
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "llama3.1",
            "timeout_seconds": 8,
            "enabled": True,
            "max_ideas_per_run": 4,
        },
    },
    "research": {
        "enabled": True,
        "rss": True,
        "arxiv": True,
        "youtube": True,
        "local_ideas": True,
        "ollama_reflections": True,
        "codex_agent": True,
        "openai_web": True,
        "youtube_api_key_env": "YOUTUBE_API_KEY",
        "max_candidates_per_source": 8,
        "max_items_per_idea": 4,
        "max_video_searches": 6,
        "nightly_ideas_per_run": 12,
        "reactive_ideas_per_run": 6,
        "min_relevance": 0.08,
        "media_min_specificity": 0.34,
    },
    "ranking": {
        "activity_half_life_days": 10,
        "feedback_weight": 0.28,
        "relevance_weight": 0.32,
        "gem_weight": 0.22,
        "novelty_weight": 0.10,
        "diversity_weight": 0.08,
        "wildcard_rate": 0.12,
    },
    "source_weights": {
        "rss": 0.85,
        "arxiv": 0.95,
        "youtube": 1.15,
        "ollama": 0.72,
        "codex": 1.05,
        "openai": 1.0,
        "manual": 0.7,
    },
    "focus_terms": [],
}


DEFAULT_SOURCES: dict[str, Any] = {
    "rss": [
        "https://hnrss.org/frontpage",
        "https://lobste.rs/rss",
    ],
    "arxiv_queries": [
        'all:"creative AI"',
        'all:"human computer interaction" OR all:"creativity"',
        'all:"collective intelligence"',
    ],
    "youtube_queries": [
        "creative technology research",
        "independent invention documentary",
        "human computer interaction ideas",
    ],
    "youtube_urls": [],
    "youtube_channel_ids": [],
    "web_seeds": [],
}


def signal_dir(vault: Path) -> Path:
    return vault / ".signal"


def config_path(vault: Path) -> Path:
    return signal_dir(vault) / "config.yml"


def sources_path(vault: Path) -> Path:
    return signal_dir(vault) / "sources.yml"


def feedback_path(vault: Path) -> Path:
    return signal_dir(vault) / "feedback.jsonl"


def imports_path(vault: Path) -> Path:
    return signal_dir(vault) / "imports.jsonl"


def state_path(vault: Path) -> Path:
    return signal_dir(vault) / "state.sqlite"


def load_config(vault: Path) -> dict[str, Any]:
    loaded = load_yaml(config_path(vault), DEFAULT_CONFIG)
    return merge_dict(DEFAULT_CONFIG, loaded)


def save_config(vault: Path, data: dict[str, Any]) -> None:
    write_yaml(config_path(vault), data)


def load_sources(vault: Path) -> dict[str, Any]:
    loaded = load_yaml(sources_path(vault), DEFAULT_SOURCES)
    return merge_dict(DEFAULT_SOURCES, loaded)


def save_sources(vault: Path, data: dict[str, Any]) -> None:
    write_yaml(sources_path(vault), data)


def ensure_config_files(vault: Path) -> None:
    signal_dir(vault).mkdir(parents=True, exist_ok=True)
    if not config_path(vault).exists():
        save_config(vault, DEFAULT_CONFIG)
    if not sources_path(vault).exists():
        save_sources(vault, DEFAULT_SOURCES)
    if not feedback_path(vault).exists():
        feedback_path(vault).write_text("", encoding="utf-8")
    if not imports_path(vault).exists():
        imports_path(vault).write_text("", encoding="utf-8")
