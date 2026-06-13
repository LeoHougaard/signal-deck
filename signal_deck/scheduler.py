from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .research import run_refresh
from .state import connect, latest_successful_run_date
from .vault import ensure_vault, scan_ideas


class AgentLoop:
    def __init__(self, vault: Path):
        self.vault = vault
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_mtimes: dict[str, float] = {}
        self.pending_change_at: float | None = None
        self.last_nightly_date: str | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name="signal-deck-agent", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self) -> None:
        ensure_vault(self.vault)
        self.last_mtimes = self._current_mtimes()
        while not self.stop_event.is_set():
            cfg = load_config(self.vault)
            poll_seconds = int(cfg.get("schedule", {}).get("poll_seconds", 20))
            try:
                self._tick(cfg)
            except Exception:
                pass
            self.stop_event.wait(max(2, poll_seconds))

    def _tick(self, cfg: dict) -> None:
        if self.should_run_nightly(cfg):
            run_refresh(self.vault, "nightly")
            self.last_nightly_date = datetime.now().date().isoformat()
            self.last_mtimes = self._current_mtimes()
            self.pending_change_at = None
            return
        if not cfg.get("schedule", {}).get("run_after_edits", True):
            return
        current = self._current_mtimes()
        if current != self.last_mtimes:
            self.last_mtimes = current
            self.pending_change_at = time.time()
            return
        if self.pending_change_at is None:
            return
        quiet_for = time.time() - self.pending_change_at
        reactive_seconds = int(cfg.get("schedule", {}).get("reactive_seconds", 90))
        if quiet_for >= reactive_seconds:
            run_refresh(self.vault, "reactive")
            self.pending_change_at = None

    def should_run_nightly(self, cfg: dict, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        today_local = now.date().isoformat()
        today_utc = now.astimezone(timezone.utc).date().isoformat() if now.tzinfo else datetime.now(timezone.utc).date().isoformat()
        if self.last_nightly_date in {today_local, today_utc} or self._last_persisted_nightly_date() in {
            today_local,
            today_utc,
        }:
            return False
        hour, minute = parse_nightly_time(str(cfg.get("schedule", {}).get("nightly_time", "02:20")))
        return (now.hour, now.minute) >= (hour, minute)

    def _last_persisted_nightly_date(self) -> str | None:
        conn = connect(self.vault)
        try:
            return latest_successful_run_date(conn, "nightly")
        finally:
            conn.close()

    def _current_mtimes(self) -> dict[str, float]:
        cfg = load_config(self.vault)
        return {idea.id: idea.modified_at for idea in scan_ideas(self.vault, cfg)}


def parse_nightly_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, TypeError):
        return 2, 20
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 2, 20
    return hour, minute
