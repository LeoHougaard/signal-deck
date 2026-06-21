# Signal Deck Architecture

Signal Deck has four layers:

1. **Vault layer**
   - Reads `Ideas/*.md`.
   - Removes `signal-agent` blocks before interpreting user idea text.
   - Writes generated artifacts only to `.signal/*`, `Signal Deck.html`, `Signal Deck.md`, and the configured generated media-note folder.

2. **State layer**
   - Stores normalized idea rows, discoveries, feedback, run history, and config events in `.signal/state.sqlite`.
   - Mirrors feedback append-only in `.signal/feedback.jsonl`.
   - Tracks import provenance in `.signal/imports.jsonl`.

3. **Agent layer**
   - Scans ideas.
   - Collects candidates from local ideas, RSS, arXiv, YouTube metadata, Codex CLI, Ollama, and OpenAI web search when configured.
   - Scores candidates by relevance, novelty, source weight, focus terms, feedback, and wildcard diversity.
   - Caps media separately per idea via `research.media_items_per_idea`.
   - Skips media URLs marked `bad` or `used` for that idea in future attachment passes.

4. **Interface layer**
   - Renders `Signal Deck.html` and `Signal Deck.md`.
   - Materializes media-like discoveries into Obsidian Markdown notes under `Media/` by default.
   - Links media notes back to matching `Ideas/*` notes so Obsidian graph/backlinks work without rewriting idea notes.
   - Writes generated related-idea links into `signal-agent` blocks inside idea notes.
   - Writes attached media links into `signal-agent` blocks when the user chooses `Attach`.
   - Keeps `Signal Deck.md` graph-neutral by listing paths instead of internal Markdown links.
   - Serves local HTTP routes:
     - `GET /`
     - `GET /api/status`
     - `POST /feedback`
     - `POST /refresh`
     - `POST /chat-config`

The default mode is local-first. For this vault, Codex mode can use the installed Codex CLI via `codex exec` in read-only mode.
