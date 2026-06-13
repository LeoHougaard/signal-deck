from __future__ import annotations

import argparse
import json
from pathlib import Path

from .chat_config import apply_chat_config
from .diagnostics import doctor, export_json, status
from .importer import import_markdown_zip
from .render import render_dashboards
from .research import run_codex_agent_test, run_refresh
from .scheduler import AgentLoop
from .server import SignalDeckServer
from .vault import ensure_vault, resolve_vault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-deck", description="Obsidian idea radar agent")
    parser.add_argument("--vault", default=".", help="Path to Obsidian vault")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Create .signal config and Ideas folder")
    init_parser.add_argument("--demo", action="store_true", help="Create one starter idea")

    import_parser = sub.add_parser("import-zip", help="Import Markdown ideas from a ZIP")
    import_parser.add_argument("zip_path", help="ZIP containing Markdown")
    import_parser.add_argument("--single-note", action="store_true", help="Do not split numbered idea lists")

    refresh_parser = sub.add_parser("refresh", help="Run research/ranking now")
    refresh_parser.add_argument("--idea", help="Idea id, path, or title filter")

    agent_parser = sub.add_parser("agent-test", help="Run Codex agent on one idea and attach results")
    agent_parser.add_argument("--idea", help="Idea id, path, or title filter")

    sub.add_parser("render", help="Regenerate Signal Deck.html and Signal Deck.md")

    sub.add_parser("status", help="Print Signal Deck state as JSON")
    sub.add_parser("doctor", help="Check local setup and provider availability")

    export_parser = sub.add_parser("export-json", help="Export state to .signal/export.json")
    export_parser.add_argument("--output", help="Output JSON path")

    chat_parser = sub.add_parser("chat-config", help="Change config using plain text")
    chat_parser.add_argument("text", help="Config instruction")

    serve_parser = sub.add_parser("serve", help="Start local dashboard server and background agent")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--no-agent", action="store_true", help="Serve without scheduler/reactive loop")

    args = parser.parse_args(argv)
    vault = resolve_vault(args.vault)

    if args.command == "init":
        ensure_vault(vault)
        if args.demo:
            _write_demo(vault)
        paths = render_dashboards(vault)
        print(f"Initialized {vault}")
        print(f"Dashboard: {paths['html']}")
        return 0

    if args.command == "import-zip":
        imported = import_markdown_zip(Path(args.zip_path).expanduser(), vault, split_numbered=not args.single_note)
        run_refresh(vault, "import")
        print(f"Imported {len(imported)} notes into {vault / 'Ideas'}")
        return 0

    if args.command == "refresh":
        result = run_refresh(vault, "manual", args.idea)
        print(result["message"])
        return 0

    if args.command == "agent-test":
        result = run_codex_agent_test(vault, args.idea)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0

    if args.command == "render":
        paths = render_dashboards(vault)
        print(paths["html"])
        print(paths["markdown"])
        return 0

    if args.command == "status":
        print(json.dumps(status(vault), indent=2, ensure_ascii=True))
        return 0

    if args.command == "doctor":
        print(json.dumps(doctor(vault), indent=2, ensure_ascii=True))
        return 0

    if args.command == "export-json":
        output = Path(args.output).expanduser() if args.output else None
        print(export_json(vault, output))
        return 0

    if args.command == "chat-config":
        result = apply_chat_config(vault, args.text)
        if result.get("should_refresh"):
            refresh = run_refresh(vault, "chat")
            result["refresh"] = refresh
        else:
            render_dashboards(vault)
        print(result)
        return 0

    if args.command == "serve":
        ensure_vault(vault)
        render_dashboards(vault)
        from .config import load_config

        cfg = load_config(vault)
        host = args.host or str(cfg.get("server", {}).get("host", "127.0.0.1"))
        port = int(args.port or cfg.get("server", {}).get("port", 8765))
        loop = AgentLoop(vault)
        if not args.no_agent:
            loop.start()
        server = SignalDeckServer((host, port), vault)
        print(f"Signal Deck running at http://{host}:{port}")
        try:
            server.serve_forever()
        finally:
            loop.stop()
        return 0

    parser.error("unknown command")
    return 2


def _write_demo(vault: Path) -> None:
    ideas = vault / "Ideas"
    ideas.mkdir(parents=True, exist_ok=True)
    demo = ideas / "001-signal-deck-demo.md"
    if demo.exists():
        return
    demo.write_text(
        "# Soft robot research radar\n\n"
        "A small system for finding unusual papers, build logs, and videos that connect pneumatic robotics, "
        "compliant mechanisms, and printable fabrication methods.\n",
        encoding="utf-8",
    )
