# Signal Deck Architecture

Signal Deck has four layers:

1. **Vault layer**
   - Reads `Ideas/*.md`.
   - Removes `signal-agent` blocks before interpreting user idea text.
   - Writes generated artifacts only to `.signal/*`, `Signal Deck.html`, and `Signal Deck.md`.

2. **State layer**
   - Stores normalized idea rows, discoveries, feedback, run history, and config events in `.signal/state.sqlite`.
   - Mirrors feedback append-only in `.signal/feedback.jsonl`.
   - Tracks import provenance in `.signal/imports.jsonl`.

3. **Agent layer**
   - Scans ideas.
   - Collects candidates from local ideas, RSS, arXiv, YouTube metadata, Codex CLI, Ollama, and OpenAI web search when configured.
   - Scores candidates by relevance, novelty, source weight, focus terms, feedback, and wildcard diversity.

4. **Interface layer**
   - Renders `Signal Deck.html` and `Signal Deck.md`.
   - Serves local HTTP routes:
     - `GET /`
     - `GET /api/status`
     - `POST /feedback`
     - `POST /refresh`
     - `POST /chat-config`

The default mode is local-first. For this vault, Codex mode can use the installed Codex CLI via `codex exec` in read-only mode.
