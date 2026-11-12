# Agent Notes

- Obsidian graph labels come from filenames. Keep `Ideas/*.md` and `Media/*.md` names short, readable, and free of generated hashes.
- Never create media notes named `signal-<hash>-...`; keep stable IDs only inside frontmatter as `signal_id`.
- If old `Media/signal-*.md` files reappear, stop Obsidian and OneDrive before cleanup, then remove/rename all matches and regenerate `Signal Deck.md`.
- Verify broadly, not by example: scan `Ideas`, `Media`, `Signal Deck.md`, `Signal Deck.html`, and `.obsidian` for `signal[-_][0-9a-f]{8,}[-_]`.
