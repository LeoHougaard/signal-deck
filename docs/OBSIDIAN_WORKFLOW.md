# Obsidian Workflow

Signal Deck can be used directly as an Obsidian vault. Open the project root as the vault folder.

## Vault Surface

- `Idea Inbox/` is for fast capture. Put a plain Markdown note there with any title/body you want.
- `Idea Inbox/Processed/` keeps the original quick notes after they have been imported.
- `Ideas/` contains the protected source notes.
- `Media/` contains generated media notes for videos, papers, and other visual/reference material.
- `Signal Deck.md` is a graph-neutral index. It lists paths instead of internal links so it does not become the center of the graph.
- `.signal/` contains local state, provider config, feedback, and run history. It is ignored by git.

## Graph Shape

Generated graph links are written in two places:

- Media notes link back to matching ideas.
- Idea notes get related-idea and attached-media links inside the protected `signal-agent` block.

Signal Deck never rewrites user-authored idea text outside that generated block.

## Quick Idea Capture

Create a simple note in `Idea Inbox/`. The note can be as small as one sentence.

On the next agent run, Signal Deck:

- creates a formatted idea note in `Ideas/`
- keeps the full text you wrote under `## Captured note`
- adds generated categories and related links inside the protected `signal-agent` block
- moves the original inbox file to `Idea Inbox/Processed/`
- refreshes the dashboard and Obsidian links

Manual run:

```powershell
.\scripts\run_signal_agent.ps1 -Vault .
```

Equivalent direct command for a full refresh:

```powershell
python -m signal_deck --vault "." refresh
```

Inbox-only import command:

```powershell
python -m signal_deck --vault "." process-inbox
```

When the local server is running, the background loop also watches `Idea Inbox/` and refreshes after the quiet edit window.

## Run From Obsidian

Core Obsidian cannot run PowerShell commands by itself. To trigger the agent from inside Obsidian, install a shell-command community plugin, then add a command that runs:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "C:\Users\Leo\OneDrive\Projects\Home Bases\scripts\run_signal_agent.ps1" -Vault "C:\Users\Leo\OneDrive\Projects\Home Bases"
```

After that, run the command from Obsidian's command palette whenever you want the inbox and links updated.

## Manual launch without a background agent

From the source checkout, use an absolute vault path. To view a restored or test
vault without scheduling research, choose an unused port and run:

```powershell
Set-Location "C:\Users\Leo\OneDrive\Projects\Home Bases"
python -m signal_deck --vault "C:\path\to\restored-vault" serve --host 127.0.0.1 --port 8877 --no-agent
```

Open `http://127.0.0.1:8877`. If the port is occupied, choose another; do not stop
the existing server. Ctrl+C stops this foreground server. `--no-agent` disables
the scheduler and edit watcher; the dashboard Refresh action still starts research.
`render` rebuilds local views without requesting new research. A manual `refresh`
uses the selected vault's provider configuration.

## Recovery

Use one importer/refresh runner per vault. Before recovery, finish any active
refresh and make a whole-vault backup to a **new folder outside the vault** with no
active writer. Keep `Ideas`, `Idea Inbox` (including `Processed`), `Media`, and
`.signal` together. `.signal` contains private config, feedback and SQLite state;
Git does not back up these notes or local data. Copy the backup to a new restore
directory and verify it there before considering any replacement of the original.

For example, with a quiescent vault and a destination that does not exist:

```powershell
Copy-Item -LiteralPath "C:\path\to\vault-backup" -Destination "C:\path\to\restored-vault" -Recurse -ErrorAction Stop
python -m signal_deck --vault "C:\path\to\restored-vault" render
python -m signal_deck --vault "C:\path\to\restored-vault" status
```

Open the restored idea and media notes and check edits and `## Personal notes`.
Use the manual server command above to inspect the restored dashboard. `render`
can regenerate missing dashboard files and media from intact state. It cannot
recover missing authored idea text or media personal notes; restore those files
from backup. The processed capture is the original raw note, so it cannot recover
edits made after import.

If import fails while moving a capture to `Processed`, leave the idea and inbox
file in place, resolve the filesystem/permission error, then rerun:

```powershell
python -m signal_deck --vault "C:\path\to\restored-vault" process-inbox
```

`.signal/inbox_pending/*.json` records the original capture hash and the selected
idea/archive paths before publishing the idea. Retry reuses that idea without
overwriting intervening edits. The capture is archived only after the idea exists.
Keep the idea's `inbox_capture_id` frontmatter during interrupted-import recovery;
it identifies the destination. If the destination is occupied by a different note,
processing stops with both notes preserved. Move the unrelated note to a distinct
path after backing it up, then retry.
Completed imports remove the receipt. Writes publish via a temporary file, so a
failed publication can be retried without leaving a partial idea.

If the capture changed during recovery, processing stops. Back up all versions,
compare the pending receipt's `idea` with the inbox file, and preserve the changed
capture as a separate new capture. Restore the original capture bytes from your
backup to finish the pending import before processing the new version. Do not
delete receipts to force a retry. If interruption happened after the archive move
but before receipt removal, inspect the recorded archive and idea against backup;
an obsolete receipt can be moved out of `inbox_pending` after confirming both are
intact and no original capture remains. Preserve it with the backup.

Malformed or reversed `signal-agent` markers cause block refresh to stop without
writing that note. Compare with backup and repair the single start/end pair. Keep
human text outside the pair; its contents are generated and replaced on refresh.

## Isolated lifecycle verification

From the source checkout:

```powershell
python -m unittest discover -s tests -p test_note_lifecycle.py -v
python -m unittest discover -s tests
```

The lifecycle tests create temporary vaults, disable external research, and prove
capture → process → human edit → repeated refresh → snapshot restore. They also
inject an archive failure and check retry, changed-capture protection, raw capture
preservation, generated-block replacement, and a broad generated-name scan.
Existing tests cover media personal-note preservation, relevance filtering, and
HTTP actions on OS-selected temporary ports. No live Obsidian or browser session
is needed for these checks.

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
