from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from signal_deck.chat_config import apply_chat_config
from signal_deck.config import load_config, load_sources, save_config
from signal_deck.diagnostics import doctor, export_json, status
from signal_deck.importer import import_markdown_zip
from signal_deck.render import render_dashboards
from signal_deck.research import (
    Candidate,
    attach_candidates,
    fetch_codex_agent_research,
    parse_openai_response,
    run_codex_agent_test,
    run_refresh,
    should_use_codex,
)
from signal_deck.scheduler import AgentLoop, parse_nightly_time
from signal_deck.server import SignalDeckServer
from signal_deck.scoring import rank_ideas
from signal_deck.state import (
    add_discovery,
    add_feedback,
    connect,
    feedback_totals,
    latest_successful_run_date,
    list_discoveries,
    list_ideas,
    record_run_finish,
    record_run_start,
    upsert_idea,
)
from signal_deck.util import now_utc
from signal_deck.vault import ensure_vault, scan_ideas, update_agent_block


class SignalDeckTests(unittest.TestCase):
    def test_vault_scan_finds_ideas_and_ignores_agent_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            idea = vault / "Ideas" / "robot.md"
            idea.write_text(
                "# Pneumatic leg\n\nUser idea.\n\n"
                "<!-- signal-agent:start -->\nAgent text.\n<!-- signal-agent:end -->\n",
                encoding="utf-8",
            )
            ideas = scan_ideas(vault)
            self.assertEqual(len(ideas), 1)
            self.assertEqual(ideas[0].title, "Pneumatic leg")
            self.assertIn("User idea", ideas[0].user_text)
            self.assertNotIn("Agent text", ideas[0].user_text)

    def test_agent_block_update_preserves_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            cfg = load_config(vault)
            note = vault / "Ideas" / "safe.md"
            note.write_text("# Safe idea\n\nDo not change this.\n", encoding="utf-8")
            update_agent_block(note, "Rank: 88", cfg)
            first = note.read_text(encoding="utf-8")
            self.assertIn("Do not change this.", first)
            update_agent_block(note, "Rank: 91", cfg)
            second = note.read_text(encoding="utf-8")
            self.assertIn("Do not change this.", second)
            self.assertNotIn("Rank: 88", second)
            self.assertIn("Rank: 91", second)

    def test_import_zip_splits_numbered_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            zip_path = Path(tmp) / "ideas.zip"
            markdown = (
                "# Idea note\n\n### Relation summary (one-line topics)\n\n"
                "- **2** (Beta) <-> **1** (Alpha)\n\n---\n\n### Ideas\n\n"
                "1. Alpha robot\n    - soft actuator\n"
                "2. Beta gearbox\n    - compact ring gear\n"
            )
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("Idea note.md", markdown)
            imported = import_markdown_zip(zip_path, vault)
            self.assertEqual(len(imported), 2)
            self.assertTrue((vault / ".signal" / "imports.jsonl").exists())
            ideas = scan_ideas(vault)
            self.assertEqual({idea.title for idea in ideas}, {"Alpha robot", "Beta gearbox"})
            self.assertIn("Relations", imported[1].read_text(encoding="utf-8"))

    def test_feedback_changes_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            old = vault / "Ideas" / "old.md"
            new = vault / "Ideas" / "new.md"
            old.write_text("# Old idea\n\nmagnetic suspension\n", encoding="utf-8")
            new.write_text("# New idea\n\npneumatic actuator\n", encoding="utf-8")
            old_time = time.time() - 86400 * 30
            os.utime(old, (old_time, old_time))
            conn = connect(vault)
            try:
                for idea in scan_ideas(vault):
                    upsert_idea(conn, idea)
                conn.commit()
                add_feedback(conn, vault, "Ideas/old.md", None, "spark", 5)
                rows = list_ideas(conn)
                ranked = rank_ideas(rows, {}, feedback_totals(conn), load_config(vault))
                self.assertEqual(ranked[0]["id"], "Ideas/old.md")
            finally:
                conn.close()

    def test_render_includes_youtube_metadata_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            note = vault / "Ideas" / "video.md"
            note.write_text("# Walking robot\n\nrotating helix walking robot\n", encoding="utf-8")
            conn = connect(vault)
            try:
                idea = scan_ideas(vault)[0]
                upsert_idea(conn, idea)
                add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "youtube",
                        "title": "Helix robot build",
                        "url": "https://www.youtube.com/watch?v=test",
                        "summary": "Metadata summary. Channel: Lab",
                        "why": "Matches walking robot.",
                        "score": 0.8,
                        "novelty": 0.7,
                        "citations": [],
                    },
                )
            finally:
                conn.close()
            paths = render_dashboards(vault)
            html = paths["html"].read_text(encoding="utf-8").lower()
            self.assertIn("helix robot build", html)
            self.assertNotIn("transcript", html)

    def test_offline_local_refresh_works_without_openai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            cfg = load_config(vault)
            cfg["research"]["rss"] = False
            cfg["research"]["arxiv"] = False
            cfg["research"]["youtube"] = False
            cfg["providers"]["mode"] = "local"
            save_config(vault, cfg)
            (vault / "Ideas" / "offline.md").write_text("# Offline idea\n\nlocal ranking only\n", encoding="utf-8")
            result = run_refresh(vault)
            self.assertEqual(result["status"], "ok")
            self.assertTrue((vault / "Signal Deck.html").exists())

    def test_openai_response_citations_parse_and_render_clickable(self) -> None:
        sample = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "text": "Use source A.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/a",
                                    "title": "Source A",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        text, citations = parse_openai_response(sample)
        self.assertIn("Use source", text)
        self.assertEqual(citations[0]["url"], "https://example.com/a")

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            note = vault / "Ideas" / "citation.md"
            note.write_text("# Citation idea\n\nsource citation test\n", encoding="utf-8")
            idea = scan_ideas(vault)[0]
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "openai",
                        "title": "Research pack",
                        "url": "https://example.com/a",
                        "summary": text,
                        "why": "Cited source.",
                        "score": 0.9,
                        "novelty": 0.8,
                        "citations": citations,
                    },
                )
            finally:
                conn.close()
            html = render_dashboards(vault)["html"].read_text(encoding="utf-8")
            self.assertIn('href="https://example.com/a"', html)
            self.assertIn("Source A", html)

    def test_scheduler_time_and_chat_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            result = apply_chat_config(vault, "focus more on robotics and run at 1:30 am")
            self.assertEqual(result["applied"]["nightly_time"], "01:30")
            self.assertIn("robotics", load_config(vault)["focus_terms"])
            source_result = apply_chat_config(vault, "add rss https://example.com/feed.xml")
            self.assertIn("sources", source_result["applied"])
            self.assertIn("https://example.com/feed.xml", load_sources(vault)["rss"])
            self.assertEqual(parse_nightly_time("25:99"), (2, 20))
            loop = AgentLoop(vault)
            cfg = load_config(vault)
            self.assertFalse(loop.should_run_nightly(cfg, datetime(2026, 1, 1, 1, 0)))
            self.assertTrue(loop.should_run_nightly(cfg, datetime(2026, 1, 1, 2, 0)))

    def test_scheduler_uses_persisted_nightly_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            conn = connect(vault)
            try:
                run_id = record_run_start(conn, "nightly")
                record_run_finish(conn, run_id, "ok", "done")
                today = latest_successful_run_date(conn, "nightly")
            finally:
                conn.close()
            self.assertEqual(today, now_utc().date().isoformat())
            loop = AgentLoop(vault)
            self.assertFalse(loop.should_run_nightly(load_config(vault), datetime.now().replace(hour=23, minute=59)))

    def test_reactive_scheduler_triggers_after_quiet_edit_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            note = vault / "Ideas" / "reactive.md"
            note.write_text("# Reactive idea\n\nfirst\n", encoding="utf-8")
            loop = AgentLoop(vault)
            loop.last_mtimes = loop._current_mtimes()
            cfg = load_config(vault)
            cfg["schedule"]["nightly_time"] = "23:59"
            cfg["schedule"]["reactive_seconds"] = 0
            note.write_text("# Reactive idea\n\nsecond\n", encoding="utf-8")
            os.utime(note, None)
            loop._tick(cfg)
            self.assertIsNotNone(loop.pending_change_at)
            with patch("signal_deck.scheduler.run_refresh") as mocked_refresh:
                loop._tick(cfg)
                mocked_refresh.assert_called_once_with(vault, "reactive")
            self.assertIsNone(loop.pending_change_at)

    def test_attach_candidates_adds_relevant_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "pneumatic.md").write_text(
                "# Pneumatic joint\n\nsoft pneumatic actuator high angle rib shell\n",
                encoding="utf-8",
            )
            idea = scan_ideas(vault)[0]
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                added = attach_candidates(
                    conn,
                    load_config(vault),
                    [idea],
                    [Candidate("rss", "Soft pneumatic actuator", "https://example.com", "ribbed high angle shell")],
                )
                self.assertGreaterEqual(added, 1)
                self.assertEqual(len(list_discoveries(conn, idea.id)), 1)
            finally:
                conn.close()

    def test_local_refresh_links_related_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "a.md").write_text("# Pneumatic robot\n\nrib shell actuator\n", encoding="utf-8")
            (vault / "Ideas" / "b.md").write_text("# Rib actuator\n\npneumatic robot shell\n", encoding="utf-8")
            cfg = load_config(vault)
            cfg["research"]["rss"] = False
            cfg["research"]["arxiv"] = False
            cfg["research"]["youtube"] = False
            cfg["research"]["ollama_reflections"] = False
            save_config(vault, cfg)
            run_refresh(vault)
            html = (vault / "Signal Deck.html").read_text(encoding="utf-8")
            self.assertIn('href="Ideas/', html)
            self.assertIn('id="filter"', html)

    def test_status_doctor_export_and_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "status.md").write_text("# Status idea\n\nlocal status\n", encoding="utf-8")
            run_refresh(vault)
            current = status(vault)
            self.assertEqual(current["stats"]["ideas"], 1)
            check = doctor(vault)
            self.assertTrue(check["dashboard_renderable"])
            exported = export_json(vault)
            self.assertTrue(exported.exists())
            payload = json.loads(exported.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"]["stats"]["ideas"], 1)

            server = SignalDeckServer(("127.0.0.1", 0), vault)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", "/api/status")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                body = json.loads(response.read().decode("utf-8"))
                self.assertEqual(body["status"], "ok")
                conn.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_codex_provider_parses_structured_agent_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            note = vault / "Ideas" / "codex.md"
            note.write_text("# Codex idea\n\nsoft robot actuator\n", encoding="utf-8")
            idea = scan_ideas(vault)[0]
            cfg = load_config(vault)
            cfg["providers"]["mode"] = "codex"
            cfg["providers"]["codex"]["timeout_seconds"] = 3
            self.assertTrue(should_use_codex(cfg))

            def fake_run(args, cwd, capture_output, text, encoding, errors, timeout):
                output_path = Path(args[args.index("-o") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "items": [
                                {
                                    "title": "Soft Robotics Toolkit",
                                    "url": "https://example.com/soft",
                                    "summary": "A concrete reference for pneumatic actuator design.",
                                    "why": "It gives geometry and fabrication terms to search next.",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess_completed(0)

            with patch("signal_deck.research.subprocess.run", side_effect=fake_run):
                candidates = fetch_codex_agent_research(vault, cfg, idea)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].source_type, "codex")
            self.assertIn("pneumatic", candidates[0].summary)

    def test_codex_agent_test_attaches_direct_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "direct.md").write_text("# Direct agent\n\nobscure mechanism\n", encoding="utf-8")
            fake_candidate = Candidate(
                "codex",
                "Codex direct result",
                "signal://codex/test",
                "A useful direct agent result.",
                source_id="codex:Ideas/direct.md",
            )
            with patch("signal_deck.research.fetch_codex_agent_research", return_value=[fake_candidate]):
                result = run_codex_agent_test(vault, "direct")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["discoveries"], 1)
            self.assertIn("Codex direct result", (vault / "Signal Deck.html").read_text(encoding="utf-8"))


def subprocess_completed(returncode: int):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


if __name__ == "__main__":
    unittest.main()
