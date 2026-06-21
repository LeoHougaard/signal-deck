# Signal Deck Safety Contract

Signal Deck treats user-authored ideas as protected source material.

Allowed writes:

- `.signal/*`
- `Signal Deck.html`
- `Signal Deck.md`
- generated media notes in the configured media-note folder, `Media/` by default
- content between:
  - `<!-- signal-agent:start -->`
  - `<!-- signal-agent:end -->`

Forbidden writes:

- deleting idea notes
- rewriting user-authored idea text
- modifying text outside agent blocks
- generating media notes inside `Ideas/`
- using YouTube transcripts in v1
- assuming a ChatGPT subscription provides OpenAI API access

Codex mode does not use the OpenAI Platform API key path. It shells out to `codex exec` with a read-only sandbox and asks the nested agent for structured research output only.

The app enforces the note-write rule with `assert_only_agent_block_changed` before writing agent blocks.
Generated media notes link to ideas from the media side, which gives Obsidian graph/backlink behavior without editing user idea notes. Regeneration preserves the `## Personal notes` section in each media note.
Related idea links are generated only inside `signal-agent` blocks, preserving user-authored idea text outside those blocks.
Media marked `bad` or `used` is hidden from active results and skipped on future attachment attempts for that idea. The generated media note file is not deleted, so any personal notes remain recoverable.
