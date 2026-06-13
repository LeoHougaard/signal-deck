from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .chat_config import apply_chat_config
from .config import load_config
from .render import render_dashboards
from .research import run_refresh
from .state import add_feedback, connect
from .vault import ensure_vault


class SignalDeckServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], vault: Path):
        self.vault = vault
        super().__init__(server_address, SignalDeckHandler)


class SignalDeckHandler(BaseHTTPRequestHandler):
    server: SignalDeckServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/status", "/api/status"}:
            self._handle_status()
            return
        if parsed.path not in {"/", "/index.html"}:
            self._send_json({"error": "not found"}, status=404)
            return
        ensure_vault(self.server.vault)
        paths = render_dashboards(self.server.vault)
        data = paths["html"].read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/feedback":
                self._handle_feedback(payload)
            elif parsed.path == "/refresh":
                result = run_refresh(self.server.vault, str(payload.get("kind") or "manual"), payload.get("idea_id"))
                self._send_json(result)
            elif parsed.path == "/chat-config":
                result = apply_chat_config(self.server.vault, str(payload.get("text") or ""))
                if result.get("should_refresh"):
                    result["refresh"] = run_refresh(self.server.vault, "chat")
                else:
                    render_dashboards(self.server.vault)
                self._send_json(result)
            else:
                self._send_json({"error": "not found"}, status=404)
        except Exception as exc:  # pragma: no cover - HTTP boundary
            self._send_json({"error": str(exc)}, status=500)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_feedback(self, payload: dict[str, Any]) -> None:
        idea_id = str(payload["idea_id"])
        discovery_id = payload.get("discovery_id")
        conn = connect(self.server.vault)
        try:
            add_feedback(
                conn,
                self.server.vault,
                idea_id,
                int(discovery_id) if discovery_id is not None else None,
                str(payload.get("signal") or "feedback"),
                float(payload.get("value") or 0),
                str(payload.get("note") or ""),
            )
        finally:
            conn.close()
        render_dashboards(self.server.vault)
        self._send_json({"status": "ok"})

    def _handle_status(self) -> None:
        conn = connect(self.server.vault)
        try:
            from .state import dashboard_stats, recent_runs
            from .config import load_config

            stats = dashboard_stats(conn)
            runs = [
                {
                    "id": int(row["id"]),
                    "kind": row["kind"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "message": row["message"],
                }
                for row in recent_runs(conn, 5)
            ]
            cfg = load_config(self.server.vault)
        finally:
            conn.close()
        self._send_json(
            {
                "status": "ok",
                "vault": str(self.server.vault),
                "provider_mode": cfg.get("providers", {}).get("mode", "local"),
                "stats": stats,
                "runs": runs,
            }
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        loaded = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(loaded, dict):
            raise ValueError("Expected JSON object")
        return loaded

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(vault: Path, host: str | None = None, port: int | None = None) -> None:
    ensure_vault(vault)
    cfg = load_config(vault)
    host = host or str(cfg.get("server", {}).get("host", "127.0.0.1"))
    port = int(port or cfg.get("server", {}).get("port", 8765))
    render_dashboards(vault)
    server = SignalDeckServer((host, port), vault)
    print(f"Signal Deck running at http://{host}:{port}")
    server.serve_forever()
