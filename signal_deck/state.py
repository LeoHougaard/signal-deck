from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import feedback_path, state_path
from .util import iso_now
from .vault import Idea


SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    modified_at REAL NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL,
    why TEXT NOT NULL,
    score REAL NOT NULL,
    novelty REAL NOT NULL,
    is_wildcard INTEGER NOT NULL DEFAULT 0,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(idea_id, url)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id TEXT NOT NULL,
    discovery_id INTEGER,
    signal TEXT NOT NULL,
    value REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS config_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    applied_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(vault: Path) -> sqlite3.Connection:
    state_path(vault).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_path(vault))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_idea(conn: sqlite3.Connection, idea: Idea) -> None:
    conn.execute(
        """
        INSERT INTO ideas(id, path, title, body_hash, modified_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            path=excluded.path,
            title=excluded.title,
            body_hash=excluded.body_hash,
            modified_at=excluded.modified_at,
            last_seen_at=excluded.last_seen_at
        """,
        (idea.id, idea.rel_path, idea.title, idea.body_hash, idea.modified_at, iso_now()),
    )


def record_run_start(conn: sqlite3.Connection, kind: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs(kind, started_at, status) VALUES (?, ?, ?)",
        (kind, iso_now(), "running"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def record_run_finish(conn: sqlite3.Connection, run_id: int, status: str, message: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, message=? WHERE id=?",
        (iso_now(), status, message[:1000], run_id),
    )
    conn.commit()


def add_discovery(conn: sqlite3.Connection, discovery: dict[str, Any]) -> int:
    now = iso_now()
    citations_json = json.dumps(discovery.get("citations", []), ensure_ascii=True)
    cursor = conn.execute(
        """
        INSERT INTO discoveries(
            idea_id, source_type, title, url, summary, why, score, novelty,
            is_wildcard, citations_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(idea_id, url) DO UPDATE SET
            title=excluded.title,
            source_type=excluded.source_type,
            summary=excluded.summary,
            why=excluded.why,
            score=MAX(discoveries.score, excluded.score),
            novelty=MAX(discoveries.novelty, excluded.novelty),
            is_wildcard=excluded.is_wildcard,
            citations_json=excluded.citations_json,
            updated_at=excluded.updated_at
        """,
        (
            discovery["idea_id"],
            discovery["source_type"],
            discovery["title"][:300],
            discovery["url"][:1000],
            discovery["summary"][:1800],
            discovery["why"][:1200],
            float(discovery["score"]),
            float(discovery["novelty"]),
            1 if discovery.get("is_wildcard") else 0,
            citations_json,
            now,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM discoveries WHERE idea_id=? AND url=?",
        (discovery["idea_id"], discovery["url"]),
    ).fetchone()
    conn.commit()
    return int(row["id"] if row else cursor.lastrowid)


def delete_obsolete_discoveries(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM discoveries WHERE source_type='manual' AND url LIKE 'signal://idea/%'")
    conn.commit()


def add_feedback(
    conn: sqlite3.Connection,
    vault: Path,
    idea_id: str,
    discovery_id: int | None,
    signal: str,
    value: float,
    note: str = "",
) -> None:
    created_at = iso_now()
    conn.execute(
        """
        INSERT INTO feedback(idea_id, discovery_id, signal, value, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (idea_id, discovery_id, signal, float(value), note[:1000], created_at),
    )
    feedback_path(vault).parent.mkdir(parents=True, exist_ok=True)
    with feedback_path(vault).open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "idea_id": idea_id,
                    "discovery_id": discovery_id,
                    "signal": signal,
                    "value": float(value),
                    "note": note,
                    "created_at": created_at,
                },
                ensure_ascii=True,
            )
            + "\n"
        )
    conn.commit()


def record_config_event(conn: sqlite3.Connection, text: str, applied: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO config_events(text, applied_json, created_at) VALUES (?, ?, ?)",
        (text, json.dumps(applied, ensure_ascii=True), iso_now()),
    )
    conn.commit()


def list_ideas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM ideas ORDER BY modified_at DESC"))


def list_discoveries(conn: sqlite3.Connection, idea_id: str | None = None) -> list[sqlite3.Row]:
    if idea_id:
        return list(
            conn.execute(
                "SELECT * FROM discoveries WHERE idea_id=? ORDER BY score DESC, updated_at DESC",
                (idea_id,),
            )
        )
    return list(conn.execute("SELECT * FROM discoveries ORDER BY score DESC, updated_at DESC"))


def feedback_totals(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT idea_id, SUM(value) AS total FROM feedback GROUP BY idea_id")
    return {str(row["idea_id"]): float(row["total"] or 0.0) for row in rows}


def discovery_feedback_totals(conn: sqlite3.Connection) -> dict[int, float]:
    rows = conn.execute(
        "SELECT discovery_id, SUM(value) AS total FROM feedback WHERE discovery_id IS NOT NULL GROUP BY discovery_id"
    )
    return {int(row["discovery_id"]): float(row["total"] or 0.0) for row in rows}


def recent_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))


def latest_successful_run_date(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        """
        SELECT finished_at FROM runs
        WHERE kind=? AND status='ok' AND finished_at IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (kind,),
    ).fetchone()
    if not row or not row["finished_at"]:
        return None
    return str(row["finished_at"])[:10]


def dashboard_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "ideas": int(conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0]),
        "discoveries": int(conn.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]),
        "feedback": int(conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]),
        "runs": int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        "sources": {
            row["source_type"]: int(row["count"])
            for row in conn.execute("SELECT source_type, COUNT(*) AS count FROM discoveries GROUP BY source_type")
        },
    }
