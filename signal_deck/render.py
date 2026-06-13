from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from . import state
from .config import load_config
from .scoring import rank_ideas
from .vault import is_agent_owned_path


def render_dashboards(vault: Path) -> dict[str, Path]:
    cfg = load_config(vault)
    conn = state.connect(vault)
    try:
        idea_rows = state.list_ideas(conn)
        discoveries = state.list_discoveries(conn)
        by_idea: dict[str, list[Any]] = {}
        for discovery in discoveries:
            by_idea.setdefault(str(discovery["idea_id"]), []).append(discovery)
        ranked = rank_ideas(idea_rows, by_idea, state.feedback_totals(conn), cfg)
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
      --bg: #f6f4ef;
      --ink: #202124;
      --muted: #65615a;
      --line: #d8d1c4;
      --panel: #fffdf8;
      --accent: #2d6cdf;
      --accent-2: #0f8b6f;
      --warn: #b45f06;
      --bad: #a23b3b;
      --shadow: 0 1px 10px rgba(26, 22, 16, 0.08);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --bg: #181714;
        --ink: #f0eee8;
        --muted: #b5afa5;
        --line: #37322b;
        --panel: #211f1b;
        --accent: #78a8ff;
        --accent-2: #64d0b6;
        --warn: #e3a14c;
        --bad: #df7777;
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
      padding: 14px 16px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
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
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      padding: 0 10px;
      font: inherit;
    }}
    button {{
      height: 34px;
      min-width: 34px;
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
      padding: 14px 16px 42px;
    }}
    .deck {{
      display: grid;
      gap: 10px;
    }}
    details.idea {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: clip;
    }}
    summary {{
      list-style: none;
      cursor: pointer;
      display: grid;
      grid-template-columns: 54px 1fr auto;
      gap: 12px;
      align-items: center;
      min-height: 68px;
      padding: 10px 12px;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    .rank {{
      width: 42px;
      height: 42px;
      border-radius: 6px;
      display: grid;
      place-items: center;
      border: 1px solid var(--line);
      color: var(--muted);
      font-weight: 800;
    }}
    .title {{
      min-width: 0;
    }}
    .title strong {{
      display: block;
      font-size: 16px;
      overflow-wrap: anywhere;
    }}
    .path {{
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .score {{
      display: grid;
      grid-template-columns: repeat(5, 44px);
      gap: 6px;
      align-items: end;
    }}
    .metric {{
      height: 44px;
      display: grid;
      align-content: center;
      justify-items: center;
      border-left: 3px solid var(--line);
      padding-left: 4px;
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
      padding: 8px 12px 14px 78px;
    }}
    .signals {{
      display: grid;
      gap: 8px;
    }}
    .signal {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      padding: 10px 0;
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
    }}
    .badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
    }}
    .badge {{
      border: 1px solid var(--line);
      border-radius: 999px;
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
      summary {{ grid-template-columns: 42px 1fr; }}
      .score {{ grid-column: 1 / -1; grid-template-columns: repeat(5, minmax(42px, 1fr)); }}
      .body {{ padding-left: 12px; }}
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
      for (const card of document.querySelectorAll(".idea")) {{
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
    signals = "\n".join(_render_signal(idea["id"], item) for item in discoveries[:8])
    if not signals:
        signals = '<div class="signal"><div><h3>No signals attached yet</h3><p>Refresh will fill this row when sources match.</p></div></div>'
    metrics = [
        ("score", idea["score"]),
        ("work", idea["activity"]),
        ("fit", idea["relevance"]),
        ("new", idea["novelty"]),
        ("mix", idea["diversity"]),
    ]
    metric_html = "".join(
        f'<div class="metric"><b>{int(value * 100)}</b>{html.escape(label)}</div>' for label, value in metrics
    )
    open_attr = " open" if index <= 5 else ""
    return f"""<details class="idea"{open_attr}>
  <summary>
    <div class="rank">{index}</div>
    <div class="title">
      <strong>{html.escape(str(idea["title"]))}</strong>
      <div class="path">{html.escape(str(idea["path"]))}</div>
    </div>
    <div class="score">{metric_html}</div>
  </summary>
  <div class="body">
    <div class="signals">{signals}</div>
  </div>
</details>"""


def _render_signal(idea_id: str, item: Any) -> str:
    discovery_id = int(item["id"])
    title = html.escape(str(item["title"]))
    summary = html.escape(str(item["summary"]))
    why = html.escape(str(item["why"]))
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
    <p>{why}</p>
    <div class="badges">
      <span class="badge">{source}</span>
      <span class="badge">{score}</span>
      {wild}
      {citation_links}
    </div>
  </div>
  <div class="actions">
    <button onclick="feedback({json.dumps(idea_id)}, {discovery_id}, 'useful', 1)" title="More like this">+</button>
    <button onclick="feedback({json.dumps(idea_id)}, {discovery_id}, 'weak', -1)" title="Less like this">-</button>
    <button onclick="feedback({json.dumps(idea_id)}, {discovery_id}, 'spark', 2)" title="Spark">S</button>
  </div>
</article>"""


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
        return f'<a href="{html.escape(url)}" target="_blank" rel="noreferrer">{title_html}</a>'
    return title_html


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
        lines.append(f"- Note: `{idea['path']}`")
        for item in idea.get("discoveries", [])[:5]:
            title = str(item["title"])
            url = str(item["url"])
            source = str(item["source_type"])
            score = int(float(item["score"]) * 100)
            link = f"[{title}]({url})" if url.startswith("http") or url.startswith("Ideas/") or url.endswith(".md") else title
            lines.append(f"- {link} `{source}` `{score}`")
            if item["why"]:
                lines.append(f"  - {item['why']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
