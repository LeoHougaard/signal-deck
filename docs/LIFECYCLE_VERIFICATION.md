# Note lifecycle verification, 2026-11-12

Baseline source: `187c02d8fd7959da4c1ad5e8ebedc623368d7f3a`.
The dated portfolio audit was background evidence; results below were run again.

## Verified behavior

- The original suite passed: 33 tests.
- New regressions against the baseline produced two failures and one error:
  capture whitespace was lost, a changed capture after archive failure was
  accepted as another import, and a generated regex containing `\1` raised an
  invalid group reference error. The ordinary edit/refresh/restore case passed.
- The initial local suite passed: 40 tests, including seven isolated lifecycle tests.
- `python -m compileall -q signal_deck tests` and `git diff --check` passed.
- A separate temporary vault passed actual CLI `init`, `process-inbox`,
  `refresh`, `render`, and `status`. Human edits survived refresh.
- The CLI server launched with `--no-agent` on a temporary unused loopback port;
  `/api/status` and `/` returned successfully. The task-owned process was stopped.

The new tests cover capture → process → human edit → repeated refresh → whole-vault
snapshot restore, including intact protected blocks on edit, human text surviving
refresh, and regenerated views after restore. They inject archive/publication
failures and verify retry without duplicate ideas or overwriting intervening edits.
A pending import also survives restoration to a different vault directory.
Later intentional captures with identical text still import independently.
An occupied recovery destination stops processing without overwriting it. Media
regeneration also preserves extra legacy copies with distinct personal notes;
automatic duplicate deletion was removed and its regression test passes.
Malformed block markers stop writes; literal backslashes, line endings, Markdown
trailing whitespace, and unchanged-note modification times are preserved.

## Windows CI continuation

The first PR run, `43243014032` at source `3747942`, failed eight inbox tests.
Python 3.14 resolved the runner's short temporary path (`RUNNER~1`) to its long
directory name, while relative destinations were still calculated against the
unresolved vault root. Local results above did not establish CI success.

The fix canonicalizes the vault once before deriving configured, pending and
recovery paths. Configured paths remain inside the vault; recovered destinations
must remain inside their configured Ideas/archive directories. Rejected paths do
not write or move the capture or authored notes.

Two new regressions cover alias/canonical import and retry in both directions,
including edits between failure and recovery, and traversal through relative,
absolute and wrong-folder receipt destinations. On Windows the alias test uses
`GetShortPathNameW` when supported, otherwise a real parent-directory alias; it
runs without skips on other platforms. The new alias test reproduced the original
`relative_to` exception against `3747942` locally using a real `LONGVA~1` path,
without making another project checkout or copy.
The corrected local suite passes all 42 tests, including nine lifecycle tests;
the compile and whitespace checks also pass. Hosted CI must qualify this corrected
head and the actual merged commit independently of the failed initial run.

Lifecycle fixtures disable external research. The existing suite also passed
media personal-note preservation, discovery relevance, feedback and HTTP checks.
It emitted an existing `ResourceWarning` for an HTTP 401 response; live provider
availability is not established by this suite. No live searches were scheduled.

## Live vault findings, read-only

The required broad scan covered `Ideas`, `Media`, both dashboard files and
`.obsidian`, using `signal[-_][0-9a-f]{8,}[-_]`.

- `Ideas`: 106 files, no matches.
- `Media`: 575 files, 169 matching legacy filenames.
- `Signal Deck.md`: legacy references present.
- `Signal Deck.html`: no matches.
- `.obsidian`: six files inspected, one with legacy references.

These files were not changed. Live cleanup remains a separate authorized task:
project instructions require stopping Obsidian and OneDrive first, and this task
explicitly forbids doing so. Do not refresh the live vault as a cleanup shortcut.
The temporary lifecycle vault passed the generated-name scan.

## Limits and handoff

See [Obsidian workflow](OBSIDIAN_WORKFLOW.md) for exact manual launch, backup,
restore and interrupted-import recovery commands. Run only one importer per
vault. Recovery was verified from copied snapshots, not by replacing live notes.
The raw processed capture cannot recover edits made after import; back up the
whole vault, including personal media notes and private `.signal` state.

Obsidian command-palette integration, browser interaction, active OneDrive sync,
provider credentials, and sudden power-loss recovery were not exercised.
No live notes, browser profiles, Obsidian settings/processes, global config,
existing server ports or unrelated processes were changed.

Delivery includes the reviewed earlier inbox/naming implementation, this recovery
fix, tests and documentation. The inherited `.obsidian/app.json` change is excluded
from the published diff while its local contents are preserved. Repository CI runs
the existing tests and compile check on Windows/Python 3.14 for PRs and main pushes.
