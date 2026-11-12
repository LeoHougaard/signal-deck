from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from signal_deck.config import load_config, save_config
from signal_deck.inbox import process_inbox
from signal_deck.render import render_dashboards
from signal_deck.research import run_refresh
from signal_deck.vault import ensure_vault, strip_agent_blocks, update_agent_block, update_idea


class NoteLifecycleTests(unittest.TestCase):
    def test_vault_alias_import_and_canonical_recovery_share_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = (Path(tmp) / "Long vault directory name").resolve()
            ensure_vault(vault)
            alias = vault / ".." / vault.name
            if os.name == "nt":
                import ctypes
                short_path = ctypes.create_unicode_buffer(32768)
                length = ctypes.windll.kernel32.GetShortPathNameW(str(vault), short_path, len(short_path))
                if 0 < length < len(short_path) and Path(short_path.value) != vault:
                    alias = Path(short_path.value)
            # Exercise real Windows short names where available; the parent
            # alias exercises the same lexical/resolved mismatch everywhere.
            self.assertNotEqual(alias, vault)
            self.assertEqual(alias.resolve(), vault)
            capture = vault / "Idea Inbox" / "quick.md"
            raw = b"Pneumatic rib robot\r\n\r\nKeep my text.  \r\n"
            capture.write_bytes(raw)
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    process_inbox(alias)
            note = next((vault / "Ideas").glob("*.md"))
            edited = note.read_bytes() + b"\nHuman edit during recovery.\n"
            note.write_bytes(edited)
            result = process_inbox(vault)
            self.assertEqual(result["created"], [note.relative_to(vault).as_posix()])
            self.assertEqual(note.read_bytes(), edited)
            self.assertEqual(next((vault / "Idea Inbox" / "Processed").glob("*.md")).read_bytes(), raw)
            self.assertFalse(list((vault / ".signal" / "inbox_pending").glob("*.json")))
            capture.write_bytes(b"Second capture through canonical path")
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    process_inbox(vault)
            self.assertEqual(process_inbox(alias)["processed"], 1)
            self.assertEqual(process_inbox(alias)["processed"], 0)
            self.assertEqual(len(list((vault / "Ideas").glob("*.md"))), 2)

    def test_recovery_paths_cannot_escape_configured_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = (Path(tmp) / "vault").resolve()
            ensure_vault(vault)
            capture = vault / "Idea Inbox" / "quick.md"
            capture.write_bytes(b"Keep capture")
            outside = vault.parent / "outside.md"
            outside.write_bytes(b"Protected outside text")
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    process_inbox(vault)
            receipt = next((vault / ".signal" / "inbox_pending").glob("*.json"))
            record = json.loads(receipt.read_text(encoding="utf-8"))
            note = vault / record["idea"]
            before = note.read_bytes()
            for field in ("idea", "archive"):
                for escape in ("../outside.md", str(outside), "Other/note.md"):
                    with self.subTest(field=field, escape=escape):
                        receipt.write_text(json.dumps({**record, field: escape}), encoding="utf-8")
                        with self.assertRaisesRegex(ValueError, "must stay inside"):
                            process_inbox(vault)
                        self.assertEqual(outside.read_bytes(), b"Protected outside text")
                        self.assertEqual(note.read_bytes(), before)
                        self.assertEqual(capture.read_bytes(), b"Keep capture")
                        self.assertFalse((vault / "Other").exists())
            receipt.write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(process_inbox(vault)["processed"], 1)

    def test_pending_import_survives_restore_to_another_vault_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "original"
            ensure_vault(vault)
            (vault / "Idea Inbox" / "quick.md").write_text("Pneumatic rib robot", encoding="utf-8")
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    process_inbox(vault)
            restored = Path(tmp) / "restored"
            shutil.copytree(vault, restored)
            self.assertEqual(process_inbox(restored)["processed"], 1)
            self.assertEqual(len(list((restored / "Ideas").glob("*.md"))), 1)
            self.assertFalse(list((restored / ".signal" / "inbox_pending").glob("*.json")))

    def test_failed_publication_keeps_capture_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            capture = vault / "Idea Inbox" / "quick.md"
            capture.write_text("Pneumatic rib robot", encoding="utf-8")
            from signal_deck.inbox import _atomic_write

            def publish(path, text):
                if path.parent.name == "Ideas":
                    raise OSError("Simulated publication failure")
                _atomic_write(path, text)

            with patch("signal_deck.inbox._atomic_write", side_effect=publish):
                with self.assertRaises(OSError):
                    process_inbox(vault)
            self.assertTrue(capture.exists())
            self.assertFalse(list((vault / "Ideas").glob("*.md")))
            # Another note occupying the reserved path must never be mistaken
            # for the imported capture after an interrupted publication.
            import json
            receipt = next((vault / ".signal" / "inbox_pending").glob("*.json"))
            target = vault / json.loads(receipt.read_text(encoding="utf-8"))["idea"]
            target.write_text("Unrelated human note", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "another note"):
                process_inbox(vault)
            self.assertEqual(target.read_text(encoding="utf-8"), "Unrelated human note")
            self.assertTrue(capture.exists())
            target.rename(vault / "Ideas" / "unrelated.md")
            self.assertEqual(process_inbox(vault)["processed"], 1)

    def test_archive_failure_retry_keeps_edited_idea_and_one_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            capture = vault / "Idea Inbox" / "quick.md"
            raw = "# Rib shell\r\n\r\nPneumatic robot café.  \r\n\r\n"
            capture.write_bytes(raw.encode("utf-8"))
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError("locked archive")):
                with self.assertRaises(PermissionError):
                    process_inbox(vault)
            note = next((vault / "Ideas").glob("*.md"))
            self.assertIn(raw.encode("utf-8"), note.read_bytes())
            edited = note.read_bytes() + b"\nHuman edit after interruption.\n"
            note.write_bytes(edited)
            self.assertEqual(process_inbox(vault)["created"], [note.relative_to(vault).as_posix()])
            self.assertEqual(note.read_bytes(), edited)
            self.assertEqual(len(list((vault / "Ideas").glob("*.md"))), 1)
            archive = next((vault / "Idea Inbox" / "Processed").glob("*.md"))
            self.assertEqual(archive.read_bytes(), raw.encode("utf-8"))
            self.assertEqual(process_inbox(vault)["processed"], 0)
            # A later, intentional capture with identical text is still new.
            capture.write_bytes(raw.encode("utf-8"))
            self.assertEqual(process_inbox(vault)["processed"], 1)
            self.assertEqual(len(list((vault / "Ideas").glob("*.md"))), 2)

    def test_changed_capture_during_recovery_fails_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            capture = vault / "Idea Inbox" / "quick.md"
            capture.write_text("Original capture", encoding="utf-8")
            with patch("signal_deck.inbox.shutil.move", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    process_inbox(vault)
            note = next((vault / "Ideas").glob("*.md"))
            before = note.read_bytes()
            capture.write_text("Changed capture", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed during recovery"):
                process_inbox(vault)
            self.assertEqual(note.read_bytes(), before)
            self.assertEqual(capture.read_text(encoding="utf-8"), "Changed capture")

    def test_agent_block_literal_backslashes_and_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            cfg = load_config(vault)
            note = vault / "Ideas" / "safe.md"
            note.write_text("# Safe\n\nHuman text.\n", encoding="utf-8")
            update_agent_block(note, "Initial", cfg)
            block = r"Path C:\notes\robot and regex \1"
            update_agent_block(note, block, cfg)
            self.assertIn(block, note.read_text(encoding="utf-8"))
            before = note.stat().st_mtime_ns
            update_agent_block(note, block, cfg)
            self.assertEqual(note.stat().st_mtime_ns, before)

    def test_agent_refresh_preserves_whitespace_and_refuses_broken_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            ensure_vault(vault)
            cfg = load_config(vault)
            note = vault / "Ideas" / "safe.md"
            authored = b"# Safe\r\n\r\nKeep markdown line break.  \r\n\r\n"
            note.write_bytes(authored)
            update_agent_block(note, "First", cfg)
            update_agent_block(note, "Second", cfg)
            self.assertTrue(note.read_bytes().startswith(authored))
            for malformed in (
                "<!-- signal-agent:start -->\nHuman text after broken marker.\n",
                "<!-- signal-agent:end -->\nHuman text.\n<!-- signal-agent:start -->",
            ):
                note.write_text(malformed, encoding="utf-8")
                before = note.read_bytes()
                with self.assertRaisesRegex(ValueError, "malformed"):
                    update_agent_block(note, "New", cfg)
                self.assertEqual(note.read_bytes(), before)

    def test_capture_process_edit_refresh_restore_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            ensure_vault(vault)
            cfg = load_config(vault)
            for key in ("rss", "arxiv", "youtube", "ollama_reflections", "codex_agent", "openai_web"):
                cfg["research"][key] = False
            save_config(vault, cfg)
            (vault / "Ideas" / "base.md").write_text("# Pneumatic robot\n\nrib shell actuator\n", encoding="utf-8")
            raw = "rib shell pneumatic actuator variant\n\nMy original café sketch.\n"
            (vault / "Idea Inbox" / "capture.md").write_text(raw, encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("offline fixture")):
                self.assertEqual(run_refresh(vault)["status"], "ok")
                note = next(path for path in (vault / "Ideas").glob("*.md") if path.name != "base.md")
                original = note.read_text(encoding="utf-8")
                self.assertIn(raw, original)
                self.assertIn("[[Ideas/base|Pneumatic robot]]", original)
                block = original.split(cfg["agent_block"]["start"], 1)[1].split(cfg["agent_block"]["end"], 1)[0]
                body = strip_agent_blocks(original, cfg).split("\n", 1)[1] + "\nHuman revision: use a removable rib.\n"
                update_idea(vault, note.relative_to(vault).as_posix(), "Edited rib robot", body, cfg)
                self.assertIn(block, note.read_text(encoding="utf-8"))
                authored = strip_agent_blocks(note.read_text(encoding="utf-8"), cfg)
                for _ in range(2):
                    self.assertEqual(run_refresh(vault)["status"], "ok")
                    self.assertEqual(strip_agent_blocks(note.read_text(encoding="utf-8"), cfg), authored)
                # A whole-vault snapshot preserves edits and local feedback/state.
                backup = Path(tmp) / "backup"
                shutil.copytree(vault, backup)
                restored = Path(tmp) / "restored"
                shutil.copytree(backup, restored)
                restored_note = restored / note.relative_to(vault)
                self.assertEqual(restored_note.read_bytes(), note.read_bytes())
                render_dashboards(restored)
                self.assertEqual(run_refresh(restored)["status"], "ok")
                self.assertEqual(strip_agent_blocks(restored_note.read_text(encoding="utf-8"), cfg), authored)
                self.assertEqual(len(list((restored / "Ideas").glob("*.md"))), 2)
                self.assertIn("Edited rib robot", (restored / "Signal Deck.html").read_text(encoding="utf-8"))
                for path in list((restored / "Ideas").glob("*.md")) + list((restored / "Media").glob("*.md")) + [restored / "Signal Deck.md", restored / "Signal Deck.html"]:
                    self.assertNotRegex(path.name + path.read_text(encoding="utf-8"), r"signal[-_][0-9a-f]{8,}[-_]")


if __name__ == "__main__":
    unittest.main()
