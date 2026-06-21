# Obsidian Workflow

Signal Deck can be used directly as an Obsidian vault. Open the project root as the vault folder.

## Vault Surface

- `Ideas/` contains the protected source notes.
- `Media/` contains generated media notes for videos, papers, and other visual/reference material.
- `Signal Deck.md` is a graph-neutral index. It lists paths instead of internal links so it does not become the center of the graph.
- `.signal/` contains local state, provider config, feedback, and run history. It is ignored by git.

## Graph Shape

Generated graph links are written in two places:

- Media notes link back to matching ideas.
- Idea notes get related-idea and attached-media links inside the protected `signal-agent` block.

Signal Deck never rewrites user-authored idea text outside that generated block.

## Media Review

Use the local dashboard detail page for an idea to review media:

- `Attach` keeps the media connected to the idea by writing a generated media link into the idea note.
- `Good` records positive feedback and leaves the item active.
- `Bad` records negative feedback, hides the item from active views, and skips that URL for that idea in future runs.
- `Used` records that the item has been consumed, hides it from active views, and skips it for that idea in future runs.
- `Save` stores a note about that media item.

Generated media note files are not deleted when media is hidden. This preserves any `## Personal notes` content.

## Research Spread

`research.media_items_per_idea` caps active media attachment per idea. The default is `2`, and the cap counts existing visible media, so future runs spread media across more ideas instead of piling onto a few high-scoring notes.

## Local Server

Run:

```powershell
python -m signal_deck --vault . serve --port 8765
```

Then open:

```text
http://127.0.0.1:8765
```
