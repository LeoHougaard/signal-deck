from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .config import config_path, load_config, sources_path
from .render import render_dashboards
from .state import connect, dashboard_stats, recent_runs
from .vault import scan_ideas


def status(vault: Path) -> dict[str, Any]:
    cfg = load_config(vault)
    conn = connect(vault)
    try:
        stats = dashboard_stats(conn)
        runs = [
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "message": row["message"],
            }
            for row in recent_runs(conn, 5)
        ]
    finally:
        conn.close()
    return {
        "vault": str(vault),
        "provider_mode": cfg.get("providers", {}).get("mode", "local"),
        "stats": stats,
        "runs": runs,
        "dashboard_html": str(vault / str(cfg.get("dashboard_html", "Signal Deck.html"))),
        "dashboard_md": str(vault / str(cfg.get("dashboard_md", "Signal Deck.md"))),
    }


def doctor(vault: Path) -> dict[str, Any]:
    cfg = load_config(vault)
    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "vault_exists": vault.exists(),
        "config_exists": config_path(vault).exists(),
        "sources_exists": sources_path(vault).exists(),
        "ideas_found": len(scan_ideas(vault, cfg)),
        "dashboard_renderable": False,
        "server_port_free": None,
        "openai_key_present": False,
        "youtube_key_present": False,
        "codex_cli_present": False,
        "ollama_reachable": False,
        "warnings": [],
    }
    try:
        render_dashboards(vault)
        result["dashboard_renderable"] = True
    except Exception as exc:
        result["warnings"].append(f"dashboard render failed: {exc}")
    host = str(cfg.get("server", {}).get("host", "127.0.0.1"))
    port = int(cfg.get("server", {}).get("port", 8765))
    result["server_port_free"] = _port_free(host, port)
    openai_env = str(cfg.get("providers", {}).get("openai", {}).get("api_key_env", "OPENAI_API_KEY"))
    youtube_env = str(cfg.get("research", {}).get("youtube_api_key_env", "YOUTUBE_API_KEY"))
    result["openai_key_present"] = bool(os.environ.get(openai_env))
    result["youtube_key_present"] = bool(os.environ.get(youtube_env))
    result["codex_cli_present"] = shutil.which(str(cfg.get("providers", {}).get("codex", {}).get("command", "codex"))) is not None
    result["ollama_reachable"] = _ollama_reachable(cfg)
    if result["ideas_found"] == 0:
        result["warnings"].append("No ideas found in Ideas/*.md")
    if cfg.get("providers", {}).get("mode") == "openai" and not result["openai_key_present"]:
        result["warnings"].append(f"OpenAI mode selected but {openai_env} is not set")
    if cfg.get("providers", {}).get("mode") == "codex" and not result["codex_cli_present"]:
        result["warnings"].append("Codex mode selected but codex CLI was not found on PATH")
    return result


def export_json(vault: Path, output: Path | None = None) -> Path:
    output = output or (vault / ".signal" / "export.json")
    conn = connect(vault)
    try:
        payload = {
            "status": status(vault),
            "ideas": [dict(row) for row in conn.execute("SELECT * FROM ideas ORDER BY title")],
            "discoveries": [dict(row) for row in conn.execute("SELECT * FROM discoveries ORDER BY idea_id, score DESC")],
            "feedback": [dict(row) for row in conn.execute("SELECT * FROM feedback ORDER BY id")],
        }
    finally:
        conn.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return output


def _port_free(host: str, port: int) -> bool:
    sock = socket.socket()
    try:
        return sock.connect_ex((host, port)) != 0
    finally:
        sock.close()


def _ollama_reachable(cfg: dict[str, Any]) -> bool:
    base_url = str(cfg.get("providers", {}).get("ollama", {}).get("base_url", "http://127.0.0.1:11434")).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=1.5) as response:
            return response.status == 200
    except Exception:
        return False
