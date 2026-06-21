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
from signal_deck.render import render_dashboards, render_idea_detail
from signal_deck.research import (
    Candidate,
    attach_candidates,
    fetch_codex_agent_research,
    fetch_youtube_metadata,
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
    relation_note_map,
    record_run_finish,
    record_run_start,
    upsert_idea,
    upsert_media_note,
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
            self.assertIn('/idea?idea_id=ideas%2fvideo.md', html)
            self.assertIn("related media preview", html)
            self.assertNotIn("helix robot build", html)
            detail = render_idea_detail(vault, idea.id).lower()
            self.assertIn("helix robot build", detail)
            self.assertIn("media found for this idea", detail)
            self.assertIn("obsidian note", detail)
            self.assertNotIn("transcript", html)

    def test_render_creates_obsidian_media_note_with_idea_backlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            note = vault / "Ideas" / "video.md"
            original = "# Walking robot\n\nrotating helix walking robot\n"
            note.write_text(original, encoding="utf-8")
            conn = connect(vault)
            try:
                idea = scan_ideas(vault)[0]
                upsert_idea(conn, idea)
                discovery_id = add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "youtube",
                        "title": "Helix robot build",
                        "url": "https://www.youtube.com/watch?v=helix",
                        "summary": "Metadata summary. Channel: Lab",
                        "why": "Matches walking robot.",
                        "score": 0.8,
                        "novelty": 0.7,
                        "image_url": "https://img.youtube.com/vi/helix/hqdefault.jpg",
                        "citations": [],
                    },
                )
                upsert_media_note(conn, idea.id, discovery_id, "Watch the foot contact pattern again.")
            finally:
                conn.close()

            paths = render_dashboards(vault)
            media_notes = list((vault / "Media").glob("signal-*.md"))
            self.assertEqual(len(media_notes), 1)
            media_text = media_notes[0].read_text(encoding="utf-8")
            self.assertIn("type: signal-media", media_text)
            self.assertIn("[[Ideas/video|Walking robot]]", media_text)
            self.assertIn("https://www.youtube.com/watch?v=helix", media_text)
            self.assertIn("Watch the foot contact pattern again.", media_text)
            self.assertEqual(note.read_text(encoding="utf-8"), original)
            deck_md = paths["markdown"].read_text(encoding="utf-8")
            self.assertIn("media note: `Media/signal-", deck_md)
            self.assertNotIn("[[Media/signal-", deck_md)

    def test_obsidian_media_note_regeneration_preserves_personal_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "video.md").write_text("# Walking robot\n\nhelix robot\n", encoding="utf-8")
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
                        "url": "https://www.youtube.com/watch?v=helix",
                        "summary": "Metadata summary.",
                        "why": "Matches walking robot.",
                        "score": 0.8,
                        "novelty": 0.7,
                        "image_url": "https://img.youtube.com/vi/helix/hqdefault.jpg",
                        "citations": [],
                    },
                )
            finally:
                conn.close()
            render_dashboards(vault)
            media_note = next((vault / "Media").glob("signal-*.md"))
            media_note.write_text(media_note.read_text(encoding="utf-8") + "\nHand-written Obsidian note.\n", encoding="utf-8")
            render_dashboards(vault)
            regenerated = media_note.read_text(encoding="utf-8")
            self.assertIn("Hand-written Obsidian note.", regenerated)
            self.assertEqual(len(list((vault / "Media").glob("signal-*.md"))), 1)

    def test_obsidian_media_notes_cannot_be_generated_inside_ideas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            cfg = load_config(vault)
            cfg["obsidian"]["media_dir"] = "Ideas/Media"
            save_config(vault, cfg)
            (vault / "Ideas" / "video.md").write_text("# Walking robot\n\nhelix robot\n", encoding="utf-8")
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
                        "url": "https://www.youtube.com/watch?v=helix",
                        "summary": "Metadata summary.",
                        "why": "Matches walking robot.",
                        "score": 0.8,
                        "novelty": 0.7,
                        "image_url": "https://img.youtube.com/vi/helix/hqdefault.jpg",
                        "citations": [],
                    },
                )
            finally:
                conn.close()
            with self.assertRaises(ValueError):
                render_dashboards(vault)

    def test_idea_first_dashboard_and_detail_media_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "alpha.md").write_text("# Alpha idea\n\nalpha body text\n", encoding="utf-8")
            (vault / "Ideas" / "beta.md").write_text("# Beta idea\n\nbeta body text\n", encoding="utf-8")
            conn = connect(vault)
            try:
                for idea in scan_ideas(vault):
                    upsert_idea(conn, idea)
                add_discovery(
                    conn,
                    {
                        "idea_id": "Ideas/alpha.md",
                        "source_type": "youtube",
                        "title": "Alpha video",
                        "url": "https://www.youtube.com/watch?v=alpha",
                        "summary": "Visual support for alpha.",
                        "why": "Attached to alpha.",
                        "score": 0.9,
                        "novelty": 0.8,
                        "image_url": "https://img.youtube.com/vi/alpha/hqdefault.jpg",
                        "citations": [],
                    },
                )
                add_discovery(
                    conn,
                    {
                        "idea_id": "Ideas/beta.md",
                        "source_type": "youtube",
                        "title": "Beta video",
                        "url": "https://www.youtube.com/watch?v=beta",
                        "summary": "Visual support for beta.",
                        "why": "Attached to beta.",
                        "score": 0.7,
                        "novelty": 0.6,
                        "image_url": "https://img.youtube.com/vi/beta/hqdefault.jpg",
                        "citations": [],
                    },
                )
                add_discovery(
                    conn,
                    {
                        "idea_id": "Ideas/alpha.md",
                        "source_type": "manual",
                        "title": "Related idea: Beta idea",
                        "url": "Ideas/beta.md",
                        "summary": "Beta relationship.",
                        "why": "Local related idea.",
                        "score": 0.6,
                        "novelty": 0.5,
                        "citations": [],
                    },
                )
            finally:
                conn.close()

            dashboard = render_dashboards(vault)["html"].read_text(encoding="utf-8")
            self.assertNotIn("Media and papers", dashboard)
            self.assertNotIn("media-shelf", dashboard)
            self.assertIn('class="idea-card"', dashboard)
            self.assertIn('href="/idea?idea_id=Ideas%2Falpha.md"', dashboard)
            self.assertIn("https://img.youtube.com/vi/alpha/hqdefault.jpg", dashboard)
            self.assertIn("https://img.youtube.com/vi/beta/hqdefault.jpg", dashboard)

            alpha_detail = render_idea_detail(vault, "Ideas/alpha.md")
            beta_detail = render_idea_detail(vault, "Ideas/beta.md")
            self.assertLess(alpha_detail.index("alpha body text"), alpha_detail.index("Media found for this idea"))
            self.assertIn("Alpha video", alpha_detail)
            self.assertNotIn("Beta video", alpha_detail)
            self.assertIn('id="idea-title"', alpha_detail)
            self.assertIn('id="idea-body"', alpha_detail)
            self.assertIn('id="idea-summary"', alpha_detail)
            self.assertIn('id="idea-notes"', alpha_detail)
            self.assertIn("Related ideas", alpha_detail)
            self.assertIn('href="/idea?idea_id=Ideas%2Fbeta.md"', alpha_detail)
            self.assertIn("Beta video", beta_detail)
            self.assertNotIn("Alpha video", beta_detail)

            server = SignalDeckServer(("127.0.0.1", 0), vault)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn_http = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn_http.request(
                    "POST",
                    "/ideas/relation-note",
                    body=json.dumps(
                        {
                            "idea_id": "Ideas/alpha.md",
                            "related_idea_id": "Ideas/beta.md",
                            "note": "Beta supplies the contrasting prototype.",
                        }
                    ),
                    headers={"Content-Type": "application/json"},
                )
                response = conn_http.getresponse()
                self.assertEqual(response.status, 200)
                conn_http.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            conn = connect(vault)
            try:
                self.assertEqual(
                    relation_note_map(conn, "Ideas/alpha.md")["Ideas/beta.md"],
                    "Beta supplies the contrasting prototype.",
                )
            finally:
                conn.close()

    def test_youtube_seed_drives_search_but_is_not_returned(self) -> None:
        seed = Candidate(
            "youtube",
            "High Precision Angular Gearbox, 3D Printed and Tested",
            "https://www.youtube.com/watch?v=VXcuryyRGbo",
            "Video seed from Mishin Machine.",
            channel="Mishin Machine",
            image_url="https://img.youtube.com/vi/VXcuryyRGbo/hqdefault.jpg",
        )
        found = Candidate(
            "youtube",
            "Cycloidal gearbox prototype test",
            "https://www.youtube.com/watch?v=newvideo",
            "Found by nightly video search.",
            image_url="https://img.youtube.com/vi/newvideo/hqdefault.jpg",
        )
        sources = {"youtube_urls": [seed.url], "youtube_queries": [], "youtube_channel_ids": []}
        cfg = load_config(Path(tempfile.gettempdir()))
        cfg["research"]["max_video_searches"] = 1
        idea = type("IdeaStub", (), {"title": "Compact gearbox", "modified_at": time.time()})()
        with patch("signal_deck.research._youtube_url_candidate", return_value=seed):
            with patch("signal_deck.research._youtube_search_web", return_value=[found]) as mocked_search:
                candidates = fetch_youtube_metadata(cfg, sources, 5, [idea], {seed.url})
        self.assertEqual([candidate.url for candidate in candidates], [found.url])
        self.assertIn("gearbox", mocked_search.call_args.args[0].lower())

    def test_media_requires_idea_specific_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "offset-arm.md").write_text(
                "# Dual offset arm suspension\n\nConverts to linear motion for sailing linkage.\n",
                encoding="utf-8",
            )
            idea = scan_ideas(vault)[0]
            cfg = load_config(vault)
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                generic = Candidate(
                    "youtube",
                    "Transparent 3D Printing!",
                    "https://www.youtube.com/watch?v=generic",
                    "A general clear resin printing video.",
                    image_url="https://img.youtube.com/vi/generic/hqdefault.jpg",
                    source_id=f"youtube-query:{idea.id}",
                )
                specific = Candidate(
                    "youtube",
                    "Dual offset arm suspension linear sailing linkage prototype",
                    "https://www.youtube.com/watch?v=specific",
                    "Offset arm linkage converts rotary motion to linear motion for sailing.",
                    image_url="https://img.youtube.com/vi/specific/hqdefault.jpg",
                    source_id=f"youtube-query:{idea.id}",
                )
                inserted = attach_candidates(conn, cfg, [idea], [generic, specific])
                self.assertEqual(inserted, 1)
                discoveries = list_discoveries(conn, idea.id)
                self.assertEqual(len(discoveries), 1)
                self.assertEqual(discoveries[0]["title"], specific.title)
            finally:
                conn.close()

    def test_media_items_per_idea_caps_visual_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "media-cap.md").write_text(
                "# Hydrofoil latch\n\ncambered hydrofoil latch sailing linkage prototype\n",
                encoding="utf-8",
            )
            cfg = load_config(vault)
            cfg["research"]["media_items_per_idea"] = 1
            cfg["research"]["max_items_per_idea"] = 4
            save_config(vault, cfg)
            idea = scan_ideas(vault)[0]
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                candidates = [
                    Candidate(
                        "youtube",
                        "Cambered hydrofoil latch sailing linkage prototype alpha",
                        "https://www.youtube.com/watch?v=cap1",
                        "cambered hydrofoil latch sailing linkage prototype",
                        image_url="https://img.youtube.com/vi/cap1/hqdefault.jpg",
                    ),
                    Candidate(
                        "youtube",
                        "Cambered hydrofoil latch sailing linkage prototype beta",
                        "https://www.youtube.com/watch?v=cap2",
                        "cambered hydrofoil latch sailing linkage prototype",
                        image_url="https://img.youtube.com/vi/cap2/hqdefault.jpg",
                    ),
                    Candidate(
                        "ollama",
                        "Hydrofoil latch control note",
                        "signal://ollama/control",
                        "hydrofoil latch control note",
                        source_id=f"ollama:{idea.id}",
                    ),
                ]
                inserted = attach_candidates(conn, cfg, [idea], candidates)
                self.assertEqual(inserted, 2)
                rows = list_discoveries(conn, idea.id)
                media_rows = [row for row in rows if row["source_type"] == "youtube"]
                self.assertEqual(len(media_rows), 1)
                added_later = attach_candidates(
                    conn,
                    cfg,
                    [idea],
                    [
                        Candidate(
                            "youtube",
                            "Cambered hydrofoil latch sailing linkage prototype gamma",
                            "https://www.youtube.com/watch?v=cap3",
                            "cambered hydrofoil latch sailing linkage prototype",
                            image_url="https://img.youtube.com/vi/cap3/hqdefault.jpg",
                        ),
                        Candidate(
                            "ollama",
                            "Hydrofoil latch later note",
                            "signal://ollama/later",
                            "hydrofoil latch later note",
                            source_id=f"ollama:{idea.id}",
                        ),
                    ],
                )
                self.assertEqual(added_later, 1)
                rows = list_discoveries(conn, idea.id)
                media_rows = [row for row in rows if row["source_type"] == "youtube"]
                self.assertEqual(len(media_rows), 1)
            finally:
                conn.close()

    def test_media_action_hides_used_and_attaches_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "media.md").write_text("# Walking robot\n\nwalking robot linkage\n", encoding="utf-8")
            idea = scan_ideas(vault)[0]
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                used_id = add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "youtube",
                        "title": "Used walking robot video",
                        "url": "https://www.youtube.com/watch?v=used",
                        "summary": "walking robot linkage",
                        "why": "matches",
                        "score": 0.9,
                        "novelty": 0.6,
                        "image_url": "https://img.youtube.com/vi/used/hqdefault.jpg",
                        "citations": [],
                    },
                )
                attached_id = add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "youtube",
                        "title": "Attached walking robot video",
                        "url": "https://www.youtube.com/watch?v=attached",
                        "summary": "walking robot linkage",
                        "why": "matches",
                        "score": 0.8,
                        "novelty": 0.6,
                        "image_url": "https://img.youtube.com/vi/attached/hqdefault.jpg",
                        "citations": [],
                    },
                )
            finally:
                conn.close()

            server = SignalDeckServer(("127.0.0.1", 0), vault)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for discovery_id, action in [(used_id, "used"), (attached_id, "attached")]:
                    conn_http = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                    conn_http.request(
                        "POST",
                        "/media/action",
                        body=json.dumps({"idea_id": idea.id, "discovery_id": discovery_id, "action": action}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = conn_http.getresponse()
                    self.assertEqual(response.status, 200)
                    conn_http.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

            conn = connect(vault)
            try:
                visible_titles = {str(row["title"]) for row in list_discoveries(conn, idea.id)}
                all_titles = {str(row["title"]) for row in list_discoveries(conn, idea.id, include_hidden=True)}
                self.assertNotIn("Used walking robot video", visible_titles)
                self.assertIn("Used walking robot video", all_titles)
            finally:
                conn.close()
            render_dashboards(vault)
            note_text = (vault / "Ideas" / "media.md").read_text(encoding="utf-8")
            self.assertIn("## Attached media", note_text)
            self.assertIn("Attached walking robot video", note_text)

    def test_nightly_video_research_is_scoped_to_each_idea(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "alpha.md").write_text(
                "# Alpha hydrofoil latch\n\ncambered hydrofoil latch sailing linkage\n",
                encoding="utf-8",
            )
            (vault / "Ideas" / "beta.md").write_text(
                "# Beta ceramic extruder\n\nhigh temperature ceramic paste extruder auger\n",
                encoding="utf-8",
            )
            cfg = load_config(vault)
            cfg["research"]["rss"] = False
            cfg["research"]["arxiv"] = False
            cfg["research"]["local_ideas"] = False
            cfg["research"]["ollama_reflections"] = False
            cfg["research"]["codex_agent"] = False
            cfg["research"]["openai_web"] = False
            cfg["research"]["max_video_searches"] = 2
            cfg["research"]["nightly_ideas_per_run"] = 2
            save_config(vault, cfg)

            def fake_search(query, limit, excluded_urls, source_id=""):
                return [
                    Candidate(
                        "youtube",
                        f"{query} fixture demo",
                        f"https://www.youtube.com/watch?v={abs(hash(source_id))}",
                        f"Specific result for {query}.",
                        image_url="https://img.youtube.com/vi/scoped/hqdefault.jpg",
                        source_id=source_id,
                    )
                ]

            with patch("signal_deck.research._youtube_search_web", side_effect=fake_search) as mocked:
                result = run_refresh(vault, "nightly")
            self.assertEqual(result["status"], "ok")
            source_ids = [call.args[3] for call in mocked.call_args_list]
            self.assertIn("youtube-query:Ideas/alpha.md", source_ids)
            self.assertIn("youtube-query:Ideas/beta.md", source_ids)
            conn = connect(vault)
            try:
                alpha = [dict(row) for row in list_discoveries(conn, "Ideas/alpha.md")]
                beta = [dict(row) for row in list_discoveries(conn, "Ideas/beta.md")]
            finally:
                conn.close()
            self.assertTrue(alpha)
            self.assertTrue(beta)
            self.assertTrue(all("Alpha hydrofoil latch" in row["title"] for row in alpha))
            self.assertTrue(all("Beta ceramic extruder" in row["title"] for row in beta))

    def test_refresh_prunes_existing_generic_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            (vault / "Ideas" / "arm.md").write_text(
                "# Dual offset arm suspension\n\nlinear sailing linkage\n",
                encoding="utf-8",
            )
            cfg = load_config(vault)
            cfg["research"]["rss"] = False
            cfg["research"]["arxiv"] = False
            cfg["research"]["youtube"] = False
            cfg["research"]["local_ideas"] = False
            cfg["research"]["ollama_reflections"] = False
            cfg["research"]["codex_agent"] = False
            cfg["research"]["openai_web"] = False
            save_config(vault, cfg)
            idea = scan_ideas(vault)[0]
            conn = connect(vault)
            try:
                upsert_idea(conn, idea)
                add_discovery(
                    conn,
                    {
                        "idea_id": idea.id,
                        "source_type": "youtube",
                        "title": "Transparent 3D Printing!",
                        "url": "https://www.youtube.com/watch?v=generic",
                        "summary": "A general clear resin printing video.",
                        "why": "Old generic media.",
                        "score": 0.8,
                        "novelty": 0.6,
                        "image_url": "https://img.youtube.com/vi/generic/hqdefault.jpg",
                        "citations": [],
                    },
                )
            finally:
                conn.close()
            result = run_refresh(vault, "manual")
            self.assertEqual(result["status"], "ok")
            conn = connect(vault)
            try:
                self.assertEqual(list_discoveries(conn, idea.id), [])
            finally:
                conn.close()

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
            self.assertNotIn("Research pack", html)
            detail = render_idea_detail(vault, idea.id)
            self.assertIn('href="https://example.com/a"', detail)
            self.assertIn("Source A", detail)

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
            self.assertIn('href="/idea?idea_id=Ideas%2Fa.md"', html)
            self.assertIn('id="filter"', html)
            self.assertNotIn("Media and papers", html)
            detail = render_idea_detail(vault, "Ideas/a.md")
            self.assertIn("Related ideas", detail)
            self.assertIn('href="/idea?idea_id=Ideas%2Fb.md"', detail)
            signal_deck_md = (vault / "Signal Deck.md").read_text(encoding="utf-8")
            self.assertNotIn("[Full note](Ideas/a.md)", signal_deck_md)
            self.assertNotIn("[Related idea: Rib actuator](Ideas/b.md)", signal_deck_md)
            a_note = (vault / "Ideas" / "a.md").read_text(encoding="utf-8")
            b_note = (vault / "Ideas" / "b.md").read_text(encoding="utf-8")
            self.assertIn("[[Ideas/b|Rib actuator]]", a_note)
            self.assertIn("[[Ideas/a|Pneumatic robot]]", b_note)
            self.assertIn("<!-- signal-agent:start -->", a_note)

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

                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request(
                    "POST",
                    "/ideas",
                    body=json.dumps({"title": "New dashboard idea", "body": "first editable note"}),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                created = json.loads(response.read().decode("utf-8"))
                self.assertEqual(created["status"], "ok")
                self.assertTrue((vault / created["path"]).exists())
                conn.close()

                update_agent_block(vault / created["path"], "Rank: 77", load_config(vault))
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request(
                    "POST",
                    "/ideas/update",
                    body=json.dumps(
                        {
                            "idea_id": created["idea_id"],
                            "title": "Updated dashboard idea",
                            "body": "changed note text",
                        }
                    ),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                conn.close()
                updated_note = (vault / created["path"]).read_text(encoding="utf-8")
                self.assertIn("changed note text", updated_note)
                self.assertIn("Rank: 77", updated_note)

                html = (vault / "Signal Deck.html").read_text(encoding="utf-8")
                self.assertIn('id="new-title"', html)
                self.assertNotIn("Note and changes", html)
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", f"/idea?idea_id={created['idea_id']}")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                detail_html = response.read().decode("utf-8")
                conn.close()
                self.assertIn("Notes and changes", detail_html)
                self.assertIn('id="idea-title"', detail_html)
                self.assertIn('id="idea-body"', detail_html)
                self.assertIn('id="idea-summary"', detail_html)
                self.assertIn('id="idea-notes"', detail_html)
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
            self.assertNotIn("Codex direct result", (vault / "Signal Deck.html").read_text(encoding="utf-8"))
            self.assertIn("Codex direct result", render_idea_detail(vault, "Ideas/direct.md"))


def subprocess_completed(returncode: int):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


if __name__ == "__main__":
    unittest.main()
