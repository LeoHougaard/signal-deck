# Signal Deck Safety Contract

Signal Deck treats user-authored ideas as protected source material.

Allowed writes:

- `.signal/*`
- `Signal Deck.html`
- `Signal Deck.md`
- content between:
  - `<!-- signal-agent:start -->`
  - `<!-- signal-agent:end -->`

Forbidden writes:

- deleting idea notes
- rewriting user-authored idea text
- modifying text outside agent blocks
- using YouTube transcripts in v1
- assuming a ChatGPT subscription provides OpenAI API access

Codex mode does not use the OpenAI Platform API key path. It shells out to `codex exec` with a read-only sandbox and asks the nested agent for structured research output only.

The app enforces the note-write rule with `assert_only_agent_block_changed` before writing agent blocks.
