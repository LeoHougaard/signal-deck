from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import state
from .config import load_config
from .scoring import rank_ideas
from .vault import is_agent_owned_path, strip_agent_blocks


def render_dashboards(vault: Path) -> dict[str, Path]:
    cfg = load_config(vault)
    conn = state.connect(vault)
    try:
        idea_rows = state.list_ideas(conn)
        discoveries = state.list_discoveries(conn)
        by_idea: dict[str, list[Any]] = {}
        for discovery in discoveries:
            by_idea.setdefault(str(discovery["idea_id"]), []).append(discovery)
        feedback_rows = state.list_feedback(conn)
        feedback_by_idea: dict[str, list[Any]] = {}
        for feedback in feedback_rows:
            feedback_by_idea.setdefault(str(feedback["idea_id"]), []).append(feedback)
        metadata = state.list_idea_metadata(conn)
        ranked = rank_ideas(idea_rows, by_idea, state.feedback_totals(conn), cfg)
        _attach_note_details(vault, ranked, cfg, feedback_by_idea, metadata)
        runs = state.recent_runs(conn)
        stats = state.dashboard_stats(conn)
    finally:
        conn.close()
    html_path = vault / str(cfg.get("dashboard_html", "Signal Deck.html"))
    md_path = vault / str(cfg.get("dashboard_md", "Signal Deck.md"))
    for path in [html_path, md_path]:
        if not is_agent_owned_path(vault, path, cfg):
            raise ValueError(f"Refusing to write non-agent-owned path: {path}")
    html_path.write_text(render_html(ranked, runs, cfg, stats), encoding="utf-8")
    md_path.write_text(render_markdown(ranked, runs), encoding="utf-8")
    return {"html": html_path, "markdown": md_path}


def render_html(ranked: list[dict[str, Any]], runs: list[Any], cfg: dict[str, Any], stats: dict[str, Any] | None = None) -> str:
    rows = "\n".join(_render_idea_card(index + 1, idea) for index, idea in enumerate(ranked))
    if not rows:
        rows = '<section class="empty">No ideas found in <code>Ideas/*.md</code>.</section>'
    last_run = runs[0] if runs else None
    last_run_text = (
        f"{html.escape(str(last_run['status']))} at {html.escape(str(last_run['finished_at'] or last_run['started_at']))}"
        if last_run
        else "no runs yet"
    )
    port = int(cfg.get("server", {}).get("port", 8765))
    stats = stats or {}
    stat_text = (
        f"{int(stats.get('discoveries', 0))} signals | "
        f"{int(stats.get('feedback', 0))} feedback | "
        f"{html.escape(str(cfg.get('providers', {}).get('mode', 'local')))} mode"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Deck</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f7f4;
      --ink: #191b1f;
      --muted: #6f747b;
      --line: #dfe2e2;
      --panel: #ffffff;
      --panel-soft: #f0f5f3;
      --accent: #0f766e;
      --accent-2: #7c3aed;
      --paper: #b45309;
      --video: #b91c1c;
      --warn: #b45309;
      --shadow: 0 14px 36px rgba(24, 27, 31, 0.08);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111315;
        --ink: #f3f4f1;
        --muted: #a4abb3;
        --line: #2b3035;
        --panel: #181b1f;
        --panel-soft: #1f2927;
        --accent: #5eead4;
        --accent-2: #c4b5fd;
        --paper: #fbbf24;
        --video: #f87171;
        --warn: #e3a14c;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 4;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 92%, transparent);
      backdrop-filter: blur(14px);
    }}
    .topbar {{
      max-width: 1160px;
      margin: 0 auto;
      padding: 16px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      letter-spacing: 0;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    input {{
      width: min(38vw, 360px);
      min-width: 220px;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      padding: 0 10px;
      font: inherit;
    }}
    textarea {{
      width: 100%;
      min-height: 116px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      padding: 10px;
      font: inherit;
    }}
    button {{
      height: 36px;
      min-width: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      font: 700 14px/1 system-ui, sans-serif;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--accent); color: var(--accent); }}
    main {{
      max-width: 1160px;
      margin: 0 auto;
      padding: 18px 16px 42px;
    }}
    .overview {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .composer {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 12px;
      margin-bottom: 14px;
      display: grid;
      gap: 8px;
    }}
    .composer-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: start;
    }}
    .composer input {{
      width: 100%;
      min-width: 0;
    }}
    .stat {{
      min-height: 76px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 13px 14px;
      box-shadow: var(--shadow);
    }}
    .stat b {{
      display: block;
      font-size: 22px;
      line-height: 1.1;
    }}
    .stat span {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 12px;
    }}
    .thumb {{
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
    }}
    .kind {{
      width: fit-content;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 7px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .kind.paper {{ color: var(--paper); border-color: color-mix(in srgb, var(--paper) 55%, var(--line)); }}
    .kind.video {{ color: var(--video); border-color: color-mix(in srgb, var(--video) 55%, var(--line)); }}
    .kind.agent {{ color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 55%, var(--line)); }}
    .deck {{
      display: grid;
      gap: 12px;
    }}
    .idea-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: clip;
      color: inherit;
      text-decoration: none;
      display: block;
    }}
    .idea-card:hover {{
      border-color: var(--accent);
      text-decoration: none;
    }}
    .card-inner {{
      display: grid;
      grid-template-columns: 46px minmax(0, 1fr) auto;
      gap: 14px;
      align-items: start;
      min-height: 92px;
      padding: 14px;
    }}
    .rank {{
      width: 42px;
      height: 42px;
      border-radius: 6px;
      display: grid;
      place-items: center;
      background: var(--panel-soft);
      color: var(--accent);
      font-weight: 800;
    }}
    .title {{
      min-width: 0;
    }}
    .title strong {{
      display: block;
      font-size: 17px;
      overflow-wrap: anywhere;
    }}
    .summary-line {{
      margin-top: 6px;
      max-width: 72ch;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .path {{
      display: inline-block;
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .summary-media {{
      margin-top: 10px;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 112px));
      gap: 8px;
    }}
    .summary-media img {{
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-soft);
    }}
    .summary-media .media-count {{
      min-height: 63px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--muted);
      font-size: 12px;
    }}
    .score {{
      display: grid;
      grid-template-columns: repeat(3, 48px);
      gap: 6px;
      align-items: end;
    }}
    .metric {{
      height: 48px;
      display: grid;
      align-content: center;
      justify-items: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      font-size: 11px;
      color: var(--muted);
    }}
    .metric b {{
      display: block;
      font-size: 13px;
      color: var(--ink);
    }}
    .body {{
      border-top: 1px solid var(--line);
      padding: 12px 14px 14px 74px;
      background: color-mix(in srgb, var(--panel) 85%, var(--panel-soft));
    }}
    .note-panel {{
      margin-bottom: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      overflow: hidden;
    }}
    .note-panel > summary {{
      min-height: 0;
      padding: 10px 12px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      font-weight: 800;
    }}
    .note-editor {{
      border-top: 1px solid var(--line);
      padding: 12px;
      display: grid;
      gap: 8px;
    }}
    .note-editor input {{
      width: 100%;
      min-width: 0;
    }}
    .feedback-notes {{
      display: grid;
      gap: 6px;
      margin-top: 2px;
    }}
    .feedback-note {{
      border-left: 3px solid var(--accent);
      padding: 6px 8px;
      background: var(--panel-soft);
      color: var(--muted);
      font-size: 12px;
    }}
    .media-strip {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .media-item {{
      min-height: 76px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 9px 10px;
    }}
    .media-item .thumb {{
      margin-bottom: 8px;
    }}
    .media-item strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 13px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .media-kind {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .signals {{
      display: grid;
      gap: 0;
    }}
    .section-label {{
      margin: 2px 0 8px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }}
    .signal {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      padding: 9px 0;
      border-bottom: 1px solid var(--line);
    }}
    .signal:last-child {{ border-bottom: 0; }}
    .signal h3 {{
      margin: 0 0 4px;
      font-size: 14px;
      letter-spacing: 0;
    }}
    .signal p {{
      margin: 0 0 5px;
      color: var(--muted);
      max-width: 78ch;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
    }}
    .badge {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 7px;
      font-size: 11px;
      color: var(--muted);
    }}
    .badge.wild {{ color: var(--warn); border-color: color-mix(in srgb, var(--warn) 50%, var(--line)); }}
    .actions {{
      display: flex;
      gap: 6px;
      align-items: start;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{ text-decoration: underline; }}
    .empty {{
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 18px;
      color: var(--muted);
    }}
    .hidden {{ display: none; }}
    @media (max-width: 760px) {{
      .topbar {{ grid-template-columns: 1fr; }}
      .toolbar {{ align-items: stretch; }}
      input {{ width: 100%; min-width: 0; }}
      .card-inner {{ grid-template-columns: 42px 1fr; }}
      .score {{ grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(42px, 1fr)); }}
      .overview, .media-strip {{ grid-template-columns: 1fr; }}
      .summary-media {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .composer-row {{ grid-template-columns: 1fr; }}
      .signal {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Signal Deck</h1>
        <div class="meta">{len(ranked)} ideas | {stat_text} | {html.escape(last_run_text)}</div>
      </div>
      <div class="toolbar">
        <input id="filter" aria-label="Filter ideas" placeholder="filter ideas" oninput="filterDeck()">
        <input id="chat" aria-label="Agent config" placeholder="focus more on robotics, run at 01:30">
        <button onclick="sendConfig()" title="Send config">></button>
        <button onclick="refreshDeck()" title="Refresh">R</button>
      </div>
    </div>
  </header>
  <main>
    <section class="overview" aria-label="Dashboard summary">
      <div class="stat"><b>{len(ranked)}</b><span>ideas</span></div>
      <div class="stat"><b>{int(stats.get('discoveries', 0))}</b><span>media and research signals</span></div>
      <div class="stat"><b>{html.escape(str(cfg.get('providers', {}).get('mode', 'local')))}</b><span>agent mode</span></div>
    </section>
    <section class="composer" aria-label="Add idea">
      <div class="composer-row">
        <input id="new-title" aria-label="New idea title" placeholder="new idea title">
        <button onclick="createIdea()" title="Add idea">Add</button>
      </div>
      <textarea id="new-body" aria-label="New idea note" placeholder="notes, changes, constraints, questions"></textarea>
    </section>
    <div class="deck">{rows}</div>
  </main>
  <script>
    const API = location.protocol === "file:" ? "http://127.0.0.1:{port}" : "";
    async function post(path, body) {{
      const response = await fetch(API + path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body || {{}})
      }});
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }}
    async function feedback(ideaId, discoveryId, signal, value) {{
      await post("/feedback", {{ idea_id: ideaId, discovery_id: discoveryId, signal, value }});
      location.reload();
    }}
    async function createIdea() {{
      const title = document.getElementById("new-title").value.trim();
      const body = document.getElementById("new-body").value.trim();
      if (!title && !body) return;
      await post("/ideas", {{ title, body }});
      location.reload();
    }}
    async function updateIdea(ideaId) {{
      const title = document.getElementById("edit-title-" + cssEscapeId(ideaId)).value.trim();
      const body = document.getElementById("edit-body-" + cssEscapeId(ideaId)).value;
      await post("/ideas/update", {{ idea_id: ideaId, title, body }});
      location.reload();
    }}
    function cssEscapeId(value) {{
      return value.replace(/[^A-Za-z0-9_-]/g, "_");
    }}
    async function refreshDeck() {{
      await post("/refresh", {{ kind: "manual" }});
      location.reload();
    }}
    async function sendConfig() {{
      const input = document.getElementById("chat");
      const text = input.value.trim();
      if (!text) return;
      await post("/chat-config", {{ text }});
      input.value = "";
      location.reload();
    }}
    function filterDeck() {{
      const needle = document.getElementById("filter").value.trim().toLowerCase();
      for (const card of document.querySelectorAll(".idea-card")) {{
        const text = card.textContent.toLowerCase();
        card.classList.toggle("hidden", needle && !text.includes(needle));
      }}
    }}
  </script>
</body>
</html>
"""


def _render_idea_card(index: int, idea: dict[str, Any]) -> str:
    discoveries = idea.get("discoveries", [])
    media_items = [item for item in discoveries if _media_kind(item)]
    summary_media = _render_summary_media(media_items)
    metrics = [
        ("score", idea["score"]),
        ("fit", idea["relevance"]),
        ("media", idea.get("media_score", 0.0)),
    ]
    metric_html = "".join(
        f'<div class="metric"><b>{int(value * 100)}</b>{html.escape(label)}</div>' for label, value in metrics
    )
    detail_url = _idea_detail_href(str(idea["id"]))
    note_path = html.escape(str(idea["path"]))
    status = str(idea.get("status") or "").strip()
    tags = str(idea.get("tags") or "").strip()
    meta_bits = [note_path]
    if status:
        meta_bits.append(html.escape(status))
    if tags:
        meta_bits.append(html.escape(tags))
    return f"""<a class="idea-card" href="{detail_url}">
  <div class="card-inner">
    <div class="rank">{index}</div>
    <div class="title">
      <strong>{html.escape(str(idea["title"]))}</strong>
      <div class="summary-line">{html.escape(str(idea.get("home_summary") or "No summary yet."))}</div>
      <div class="path">{" | ".join(meta_bits)}</div>
      {summary_media}
    </div>
    <div class="score">{metric_html}</div>
  </div>
</a>"""


def render_idea_detail(vault: Path, idea_id: str) -> str:
    cfg = load_config(vault)
    idea_path = _validated_idea_path(vault, idea_id, cfg)
    conn = state.connect(vault)
    try:
        row = state.get_idea(conn, idea_id)
        discoveries = state.list_discoveries(conn, idea_id)
        metadata = state.get_idea_metadata(conn, idea_id)
        relation_notes = state.relation_note_map(conn, idea_id)
        media_notes = state.media_note_map(conn, idea_id)
    finally:
        conn.close()
    note_text = _note_text(vault, str(idea_path.relative_to(vault.resolve())).replace("\\", "/"), cfg)
    title = str(row["title"]) if row else _title_from_note(note_text, idea_path)
    body = _body_without_title(note_text)
    media_items = [item for item in discoveries if _media_kind(item) and not _is_related_idea(item)]
    related_items = [item for item in discoveries if _is_related_idea(item)]
    research_items = [item for item in discoveries if item not in media_items and item not in related_items]
    media = _render_detail_media(media_items, media_notes)
    related = _render_related_ideas(idea_id, related_items, relation_notes)
    research = _render_research_items(idea_id, research_items)
    feedback_notes = _render_feedback_notes([])
    port = int(cfg.get("server", {}).get("port", 8765))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - Signal Deck</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f7f7f4;
      --ink: #191b1f;
      --muted: #6f747b;
      --line: #dfe2e2;
      --panel: #ffffff;
      --panel-soft: #f0f5f3;
      --accent: #0f766e;
      --paper: #b45309;
      --video: #b91c1c;
      --warn: #b45309;
      --shadow: 0 14px 36px rgba(24, 27, 31, 0.08);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #111315;
        --ink: #f3f4f1;
        --muted: #a4abb3;
        --line: #2b3035;
        --panel: #181b1f;
        --panel-soft: #1f2927;
        --accent: #5eead4;
        --paper: #fbbf24;
        --video: #f87171;
        --warn: #e3a14c;
        --shadow: none;
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 4;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--bg) 92%, transparent);
      backdrop-filter: blur(14px);
    }}
    .topbar, main {{
      max-width: 1160px;
      margin: 0 auto;
      padding: 14px 16px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    main {{ display: grid; gap: 14px; padding-bottom: 42px; }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 12px;
    }}
    h1, h2 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 14px; margin-bottom: 8px; text-transform: uppercase; color: var(--muted); }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 12px; }}
    input, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      padding: 9px 10px;
      font: inherit;
    }}
    textarea {{ min-height: 120px; resize: vertical; }}
    #idea-body {{ min-height: 320px; }}
    button {{
      height: 36px;
      min-width: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      font: 700 14px/1 system-ui, sans-serif;
      cursor: pointer;
    }}
    button:hover {{ border-color: var(--accent); color: var(--accent); }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .actions-row {{ display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }}
    .media-strip {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .media-item, .relation, .signal {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: color-mix(in srgb, var(--panel) 88%, var(--panel-soft));
      padding: 10px;
    }}
    .thumb {{ width: 100%; aspect-ratio: 16 / 9; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); background: var(--panel-soft); }}
    .media-kind, .badge {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 7px;
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }}
    .signals, .relations {{ display: grid; gap: 8px; }}
    .signal {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; }}
    .signal h3, .relation h3 {{ margin: 0 0 4px; font-size: 14px; }}
    .signal p, .relation p {{ margin: 0 0 8px; color: var(--muted); }}
    .badges, .signal .actions {{ display: flex; gap: 6px; flex-wrap: wrap; align-items: start; }}
    .empty {{ border: 1px dashed var(--line); border-radius: 8px; padding: 14px; color: var(--muted); }}
    @media (max-width: 760px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .grid-2, .media-strip, .signal {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>{html.escape(title)}</h1>
        <div class="media-kind">{html.escape(idea_id)}</div>
      </div>
      <a href="/">Dashboard</a>
    </div>
  </header>
  <main>
    <section class="panel" aria-label="Idea source text">
      <label>Idea title<input id="idea-title" value="{html.escape(title)}"></label>
      <label>User summary<textarea id="idea-summary">{html.escape(metadata.get("summary", ""))}</textarea></label>
      <div class="grid-2">
        <label>Status<input id="idea-status" value="{html.escape(metadata.get("status", ""))}"></label>
        <label>Tags<input id="idea-tags" value="{html.escape(metadata.get("tags", ""))}"></label>
      </div>
      <label>Full original idea note<textarea id="idea-body">{html.escape(body)}</textarea></label>
      <div class="actions-row"><button onclick="saveIdea()" title="Save idea">Save idea</button></div>
    </section>
    <section class="panel" aria-label="Media found for this idea">
      <h2>Media found for this idea</h2>
      {media}
    </section>
    <section class="panel" aria-label="Editable notes">
      <h2>Notes and changes</h2>
      <label>User notes<textarea id="idea-notes">{html.escape(metadata.get("user_notes", ""))}</textarea></label>
      <div class="actions-row"><button onclick="saveNotes()" title="Save notes">Save notes</button></div>
      {feedback_notes}
    </section>
    <section class="panel" aria-label="Related ideas">
      <h2>Related ideas</h2>
      {related}
    </section>
    <section class="panel" aria-label="Agent research and citations">
      <h2>Agent research and citations</h2>
      {research}
    </section>
  </main>
  <script>
    const API = location.protocol === "file:" ? "http://127.0.0.1:{port}" : "";
    const IDEA_ID = {json.dumps(idea_id)};
    async function post(path, body) {{
      const response = await fetch(API + path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body || {{}})
      }});
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }}
    async function saveIdea() {{
      await post("/ideas/update", {{
        idea_id: IDEA_ID,
        title: document.getElementById("idea-title").value.trim(),
        body: document.getElementById("idea-body").value,
        summary: document.getElementById("idea-summary").value,
        status: document.getElementById("idea-status").value,
        tags: document.getElementById("idea-tags").value
      }});
      location.href = "/idea?idea_id=" + encodeURIComponent(IDEA_ID);
    }}
    async function saveNotes() {{
      await post("/ideas/note", {{ idea_id: IDEA_ID, user_notes: document.getElementById("idea-notes").value }});
      location.reload();
    }}
    async function saveRelationNote(relatedId) {{
      await post("/ideas/relation-note", {{
        idea_id: IDEA_ID,
        related_idea_id: relatedId,
        note: document.getElementById("relation-note-" + cssEscapeId(relatedId)).value
      }});
      location.reload();
    }}
    async function saveMediaNote(discoveryId) {{
      await post("/ideas/note", {{
        idea_id: IDEA_ID,
        discovery_id: discoveryId,
        media_note: document.getElementById("media-note-" + discoveryId).value
      }});
      location.reload();
    }}
    async function feedback(ideaId, discoveryId, signal, value) {{
      await post("/feedback", {{ idea_id: ideaId, discovery_id: discoveryId, signal, value }});
      location.reload();
    }}
    function cssEscapeId(value) {{
      return value.replace(/[^A-Za-z0-9_-]/g, "_");
    }}
  </script>
</body>
</html>
"""


def _render_detail_media(discoveries: list[Any], media_notes: dict[int, str]) -> str:
    if not discoveries:
        return '<div class="empty">No media has been attached to this idea yet.</div>'
    shown = discoveries[:6]
    count = ""
    if len(discoveries) > len(shown):
        count = f'<div class="empty">{len(discoveries) - len(shown)} more media items are attached to this idea.</div>'
    return '<div class="media-strip">' + "\n".join(_render_detail_media_item(item, media_notes) for item in shown) + "</div>" + count


def _render_detail_media_item(item: Any, media_notes: dict[int, str]) -> str:
    discovery_id = int(item["id"])
    kind = _media_kind(item) or "media"
    title = html.escape(str(item["title"]))
    summary = html.escape(_display_summary(str(item["summary"])))
    link = _render_link(str(item["url"]), title)
    image = _render_thumbnail(item)
    note = html.escape(media_notes.get(discovery_id, ""))
    return f"""<article class="media-item">
  {image}
  <h3>{link}</h3>
  <p>{summary}</p>
  <span class="media-kind">{html.escape(kind)}</span>
  <label>Media note<textarea id="media-note-{discovery_id}">{note}</textarea></label>
  <div class="actions-row"><button onclick="saveMediaNote({discovery_id})" title="Save media note">Save</button></div>
</article>"""


def _render_related_ideas(idea_id: str, related_items: list[Any], relation_notes: dict[str, str]) -> str:
    if not related_items:
        return '<div class="empty">No related ideas found yet.</div>'
    return '<div class="relations">' + "\n".join(_render_related_idea(idea_id, item, relation_notes) for item in related_items) + "</div>"


def _render_related_idea(idea_id: str, item: Any, relation_notes: dict[str, str]) -> str:
    related_id = str(item["url"])
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", related_id)
    title = html.escape(str(item["title"]).replace("Related idea: ", ""))
    summary = html.escape(_display_summary(str(item["summary"])))
    note = html.escape(relation_notes.get(related_id, ""))
    return f"""<article class="relation">
  <h3><a href="{_idea_detail_href(related_id)}">{title}</a></h3>
  <p>{summary}</p>
  <label>Relationship note<textarea id="relation-note-{safe_id}">{note}</textarea></label>
  <div class="actions-row"><button onclick='saveRelationNote({json.dumps(related_id)})' title="Save relationship note">Save</button></div>
</article>"""


def _render_research_items(idea_id: str, research_items: list[Any]) -> str:
    if not research_items:
        return '<div class="empty">No non-media research has been attached to this idea yet.</div>'
    return '<div class="signals">' + "\n".join(_render_signal(idea_id, item) for item in research_items) + "</div>"


def _render_note_panel(idea: dict[str, Any]) -> str:
    idea_id = str(idea["id"])
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", idea_id)
    title = html.escape(str(idea["title"]))
    body = html.escape(str(idea.get("note_body") or ""))
    feedback_notes = _render_feedback_notes(idea.get("feedback", []))
    return f"""<details class="note-panel">
  <summary><span>Note and changes</span><span class="media-kind">open note</span></summary>
  <div class="note-editor">
    <input id="edit-title-{safe_id}" aria-label="Idea title" value="{title}">
    <textarea id="edit-body-{safe_id}" aria-label="Idea note">{body}</textarea>
    <button onclick='updateIdea({json.dumps(idea_id)})' title="Save idea">Save</button>
    {feedback_notes}
  </div>
</details>"""


def _render_feedback_notes(feedback_rows: list[Any]) -> str:
    notes = []
    for row in feedback_rows:
        note = str(row["note"] or "").strip()
        if note:
            signal = html.escape(str(row["signal"]))
            created_at = html.escape(str(row["created_at"]))
            notes.append(
                f'<div class="feedback-note"><b>{signal}</b> {created_at}<br>{html.escape(note)}</div>'
            )
    if not notes:
        return ""
    return '<div class="feedback-notes">' + "\n".join(notes[:8]) + "</div>"


def _render_signal(idea_id: str, item: Any) -> str:
    discovery_id = int(item["id"])
    title = html.escape(str(item["title"]))
    summary = html.escape(_display_summary(str(item["summary"])))
    source = html.escape(str(item["source_type"]))
    score = int(float(item["score"]) * 100)
    wildcard = bool(item["is_wildcard"])
    url = str(item["url"])
    link = _render_link(url, title)
    citation_links = _citation_links(item["citations_json"])
    wild = '<span class="badge wild">wildcard</span>' if wildcard else ""
    return f"""<article class="signal">
  <div>
    <h3>{link}</h3>
    <p>{summary}</p>
    <div class="badges">
      <span class="badge">{source}</span>
      <span class="badge">{score}</span>
      {wild}
      {citation_links}
    </div>
  </div>
  <div class="actions">
    <button onclick='feedback({json.dumps(idea_id)}, {discovery_id}, "useful", 1)' title="More like this">+</button>
    <button onclick='feedback({json.dumps(idea_id)}, {discovery_id}, "weak", -1)' title="Less like this">-</button>
    <button onclick='feedback({json.dumps(idea_id)}, {discovery_id}, "spark", 2)' title="Spark">S</button>
  </div>
</article>"""


def _attach_note_details(
    vault: Path,
    ranked: list[dict[str, Any]],
    cfg: dict[str, Any],
    feedback_by_idea: dict[str, list[Any]],
    metadata_by_idea: dict[str, dict[str, str]] | None = None,
) -> None:
    metadata_by_idea = metadata_by_idea or {}
    for idea in ranked:
        note_text = _note_text(vault, str(idea.get("path") or ""), cfg)
        metadata = metadata_by_idea.get(str(idea.get("id")), {})
        idea["home_summary"] = metadata.get("summary") or _summary_from_text(note_text)
        idea["note_body"] = _body_without_title(note_text)
        idea["full_note"] = note_text
        idea["summary"] = metadata.get("summary", "")
        idea["status"] = metadata.get("status", "")
        idea["tags"] = metadata.get("tags", "")
        idea["user_notes"] = metadata.get("user_notes", "")
        idea["feedback"] = feedback_by_idea.get(str(idea.get("id")), [])


def _home_summary(vault: Path, rel_path: str, cfg: dict[str, Any]) -> str:
    return _summary_from_text(_note_text(vault, rel_path, cfg))


def _validated_idea_path(vault: Path, idea_id: str, cfg: dict[str, Any]) -> Path:
    ideas_root = (vault / str(cfg.get("ideas_dir", "Ideas"))).resolve()
    path = (vault / idea_id).resolve()
    try:
        path.relative_to(ideas_root)
    except ValueError as exc:
        raise ValueError("Idea must be inside the configured Ideas directory.") from exc
    if path.suffix.lower() != ".md" or not path.exists():
        raise ValueError("Idea note not found.")
    return path


def _note_text(vault: Path, rel_path: str, cfg: dict[str, Any]) -> str:
    path = (vault / rel_path).resolve()
    try:
        path.relative_to(vault.resolve())
    except ValueError:
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return strip_agent_blocks(raw, cfg).strip()


def _title_from_note(text: str, path: Path) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem.replace("-", " ").replace("_", " ").strip() or path.name


def _summary_from_text(text: str) -> str:
    fragments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        if "\u2194" in stripped or "<->" in stripped:
            continue
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        stripped = re.sub(r"^Imported idea number:\s*\d+\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\bRelated:\s*.*$", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"\*\*(.*?)\*\*", r"\1", stripped)
        stripped = re.sub(r"`([^`]+)`", r"\1", stripped)
        if stripped:
            fragments.append(stripped)
        if len(" ".join(fragments)) >= 180:
            break
    summary = " ".join(fragments).strip()
    if len(summary) > 170:
        summary = summary[:167].rstrip() + "..."
    return summary


def _body_without_title(text: str) -> str:
    lines = text.splitlines()
    if lines and re.match(r"^\s*#\s+", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _render_media_strip(discoveries: list[Any]) -> str:
    if not discoveries:
        return ""
    cards = "\n".join(_render_media_item(item) for item in discoveries[:6])
    return f'<div class="section-label">Media found for this idea</div><div class="media-strip" aria-label="Media found for this idea">{cards}</div>'


def _render_summary_media(media_items: list[Any]) -> str:
    if not media_items:
        return ""
    thumbnails = []
    for item in media_items[:3]:
        url = _image_url(item)
        if url:
            thumbnails.append(f'<img src="{html.escape(url)}" alt="{html.escape(str(item["title"]))}" loading="lazy">')
    remainder = len(media_items) - len(thumbnails)
    if remainder > 0:
        thumbnails.append(f'<span class="media-count">+{remainder}</span>')
    return '<div class="summary-media" aria-label="Related media preview">' + "\n".join(thumbnails[:3]) + "</div>"


def _render_media_item(item: Any) -> str:
    kind = _media_kind(item) or "source"
    title = html.escape(str(item["title"]))
    link = _render_link(str(item["url"]), title)
    image = _render_thumbnail(item)
    return f"""<article class="media-item">
  {image}
  <strong>{link}</strong>
  <span class="media-kind">{html.escape(kind)}</span>
</article>"""


def _display_summary(text: str) -> str:
    cleaned = re.split(r"\n\s*Why:\s*", text, maxsplit=1, flags=re.IGNORECASE)[0]
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 260:
        cleaned = cleaned[:257].rstrip() + "..."
    return cleaned


def _media_kind(item: Any) -> str:
    source = str(item["source_type"]).lower()
    url = str(item["url"]).lower()
    title = str(item["title"]).lower()
    if source == "youtube" or "youtube.com/watch" in url or "youtu.be/" in url:
        return "video"
    if source == "arxiv" or "arxiv.org" in url or url.endswith(".pdf") or "paper" in title:
        return "paper"
    return ""


def _is_related_idea(item: Any) -> bool:
    url = str(item["url"])
    source = str(item["source_type"]).lower()
    return source == "manual" and url.startswith("Ideas/") and url.endswith(".md")


def _media_priority(item: Any) -> int:
    kind = _media_kind(item)
    if kind == "video":
        return 0
    if _image_url(item):
        return 1
    if kind == "paper":
        return 2
    if kind == "agent pick":
        return 3
    return 4


def _image_url(item: Any) -> str:
    try:
        url = str(item["image_url"] or "")
    except (KeyError, IndexError):
        url = ""
    return url if url.startswith("http") else ""


def _render_thumbnail(item: Any) -> str:
    url = _image_url(item)
    if not url:
        return ""
    alt = html.escape(str(item["title"]))
    return f'<img class="thumb" src="{html.escape(url)}" alt="{alt}" loading="lazy">'


def _citation_links(citations_json: str) -> str:
    try:
        citations = json.loads(citations_json or "[]")
    except json.JSONDecodeError:
        citations = []
    parts = []
    for index, citation in enumerate(citations[:3], start=1):
        url = str(citation.get("url", ""))
        title = html.escape(str(citation.get("title") or f"citation {index}"))
        if url.startswith("http"):
            parts.append(f'<a class="badge" href="{html.escape(url)}" target="_blank" rel="noreferrer">{title}</a>')
    return "\n".join(parts)


def _render_link(url: str, title_html: str) -> str:
    if url.startswith("http"):
        return f'<a href="{html.escape(url)}" target="_blank" rel="noreferrer">{title_html}</a>'
    if url.startswith("Ideas/") or url.startswith("./") or url.endswith(".md"):
        return f'<a href="{_idea_detail_href(url)}">{title_html}</a>'
    return title_html


def _idea_detail_href(idea_id: str) -> str:
    return f"/idea?idea_id={quote(idea_id, safe='')}"


def render_markdown(ranked: list[dict[str, Any]], runs: list[Any]) -> str:
    lines = ["# Signal Deck", ""]
    if runs:
        run = runs[0]
        lines.append(f"Last run: {run['status']} at {run['finished_at'] or run['started_at']}")
        lines.append("")
    if not ranked:
        lines.append("No ideas found in `Ideas/*.md`.")
        lines.append("")
        return "\n".join(lines)
    for index, idea in enumerate(ranked, start=1):
        lines.append(f"## {index}. {idea['title']} ({int(idea['score'] * 100)})")
        lines.append("")
        summary = str(idea.get("home_summary") or "").strip()
        if summary:
            lines.append(summary)
            lines.append("")
        lines.append(f"[Full note]({idea['path']})")
        media = [item for item in idea.get("discoveries", []) if _media_kind(item)]
        if media:
            lines.append("")
            lines.append("Media")
            for item in media[:3]:
                title = str(item["title"])
                url = str(item["url"])
                kind = _media_kind(item)
                link = f"[{title}]({url})" if url.startswith("http") or url.startswith("Ideas/") or url.endswith(".md") else title
                lines.append(f"- {link} `{kind}`")
        research = [item for item in idea.get("discoveries", []) if item not in media]
        if research:
            lines.append("")
            lines.append("Research")
        for item in research[:3]:
            title = str(item["title"])
            url = str(item["url"])
            source = str(item["source_type"])
            score = int(float(item["score"]) * 100)
            link = f"[{title}]({url})" if url.startswith("http") or url.startswith("Ideas/") or url.endswith(".md") else title
            lines.append(f"- {link} `{source}` `{score}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
