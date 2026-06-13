# Signal Deck

Signal Deck is a local-first idea radar for an Obsidian vault.

It scans `Ideas/*.md`, keeps your idea text untouched, researches useful signals in the background, then writes:

- `Signal Deck.html` - scrollable local dashboard with feedback buttons
- `Signal Deck.md` - Obsidian-native index
- `.signal/*` - config, feedback, state, and agent data

## Quick Start

```powershell
python -m signal_deck --vault "C:\path\to\ObsidianVault" init
python -m signal_deck --vault "C:\path\to\ObsidianVault" serve
```

Open `http://127.0.0.1:8765`.

For a local test vault, use the workspace root or any Obsidian vault path.

## Commands

```powershell
python -m signal_deck --vault "." init
python -m signal_deck --vault "." refresh
python -m signal_deck --vault "." render
python -m signal_deck --vault "." chat-config "focus more on robotics and less youtube"
python -m signal_deck --vault "." serve --port 8765
python -m signal_deck --vault "." status
python -m signal_deck --vault "." doctor
python -m signal_deck --vault "." export-json
python -m signal_deck --vault "." agent-test --idea "pneumatic"
```

PowerShell helpers:

```powershell
.\scripts\start_signal_deck.ps1 -Vault .
.\scripts\doctor.ps1 -Vault .
```

## Data Safety

Signal Deck treats your idea notes as source material. It may write only:

- `.signal/*`
- `Signal Deck.html`
- `Signal Deck.md`
- content inside `<!-- signal-agent:start -->` / `<!-- signal-agent:end -->` blocks

The app never deletes or rewrites user idea text.

## Optional Providers

Codex subscription / Codex CLI mode:

```powershell
codex login
python -m signal_deck --vault "." chat-config "use codex mode cheap"
python -m signal_deck --vault "." agent-test --idea "singular pneumatic joint"
```

This uses `codex exec` in read-only mode through your installed Codex CLI. It is the best path when you want to use your ChatGPT/Codex subscription instead of an OpenAI API key.

OpenAI API mode:

```powershell
$env:OPENAI_API_KEY="..."
python -m signal_deck --vault "." chat-config "use openai mode"
```

Ollama/local mode:

```powershell
ollama serve
python -m signal_deck --vault "." chat-config "use local mode"
```

YouTube metadata mode:

```powershell
$env:YOUTUBE_API_KEY="..."
```

Without keys, Signal Deck still scans ideas, RSS, arXiv, configured YouTube channel RSS feeds, and local feedback.

## Current Seed Vault

Signal Deck stores your private vault data outside the published source tree:

- `Ideas/*.md`: your idea notes
- `.signal/state.sqlite`: rankings, discoveries, feedback, run history
- `.signal/imports.jsonl`: import provenance for imported ZIPs
- `Signal Deck.html` and `Signal Deck.md`: generated dashboard files

Local mode works immediately by connecting related ideas to each other. Codex, OpenAI, YouTube, RSS, arXiv, and Ollama enrich the deck when available.

## Dashboard Controls

- `R`: refresh research/ranking
- `+`: more like this
- `-`: less like this
- `S`: this sparked something
- chat box: natural-language config, for example `focus more on walking robots and run at 01:30`

## Proving The Agent Is Working

```powershell
python -m signal_deck --vault "." status
python -m signal_deck --vault "." agent-test --idea "singular pneumatic joint"
python -m signal_deck --vault "." status
```

The second `status` output should show `"codex"` under `stats.sources`, and the dashboard should show badges labeled `codex`.

## Tests

```powershell
python -m unittest discover -s tests
```

## More Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Safety contract](docs/SAFETY.md)
