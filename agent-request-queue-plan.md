# Agent request queue — design and spec

**Status:** phase 1 implemented on `claude/agent-queue-phase1`; phase 2 (2a:
new notes, unified schema; 2b: queue window) on `claude/agent-queue-phase2`;
phase 3a (runner side: resume, staleness, retry, catalog trimming) and 3b
(UI: stale actions, retry, defer, change model, apply then edit, toolbar badge)
on `claude/agent-queue-phase3` (see Results / notes). Phases at the bottom.

Builds on the repo-local add-on in [addons/editor_agent_pane](addons/editor_agent_pane),
which already has the provider clients (Codex, Claude, Ollama), the fixed JSON
response schema, patch validation, diff rendering, read-only card lookups, and
run logging. This plan reuses all of that and changes what is _around_ it.

## Problem

While reviewing, the user often wants an agent to change something (fix a
field, add a note, write a disambiguation card for 唉声 vs 呼声). Today the
editor agent pane can do the work, but supervising it interrupts the review
flow, and deferring it loses the context and motivation. Two couplings cause
this:

- the run is owned by the pane's window: closing the window kills it, and the
  pane is blocked until the run finishes;
- the proposal lives only in the pane's memory, so it must be approved or
  discarded then and there. There is no "look at it later".

## The shape of the solution

**Every agent request becomes a persisted thread, and every agent run is
executed by a background worker that no window owns.** The editor pane stays
the one place where requests are typed, but it turns into a view over the
thread store, and a separate queue window reviews threads across all notes.

The three activities this separates:

1. **Capture** (during review): press `e` for Edit Current, type the request
   in the pane, close the editor, keep reviewing. The card context is
   attached automatically. Nothing waits on the run.
2. **Run** (background, window-independent): the worker runs the agent and
   stores the result. Closing the editor, switching notes, or reopening the
   pane later does not affect it.
3. **Review** (later, at the user's pace): each queue item is a _thread_, not
   a proposal. A thread holds the conversation, the context it was captured
   with, and the current proposal. Reviewing means applying, discarding, **or
   replying**, and a reply sends the thread back to the worker and produces
   the next proposal version without blocking the user from moving on.

The perspective that makes the rest fall out: **the queue is a pull-request
list for your collection.** The agent authors PRs, each PR has a thread, the
user leaves review comments, merge = apply. Everything below is the usual PR
machinery: status, base revision, stale detection, re-review after changes.

Why all requests go through the queue, not just reviewer-originated ones:
one runner, one transcript model, one apply path, and one set of tests. The
pane's "ask, see proposal, apply" flow is unchanged from the user's point of
view; it is just backed by the store, so it survives closing the window.

## Data model

Per-profile SQLite file at `<profile folder>/agent_queue.sqlite` (note ids are
per collection, so the store must be too; it must not live in the collection
DB, which syncs). Three tables.

**threads**

| column        | notes                                                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| id            | uuid                                                                                                                           |
| created_at    |                                                                                                                                |
| updated_at    |                                                                                                                                |
| status        | see state machine                                                                                                              |
| title         | first ~60 chars of the first prompt; editable                                                                                  |
| note_id       | nullable; the note the pane was showing (Add Cards threads have none until applied)                                            |
| context_json  | captured context (below); immutable after capture                                                                              |
| settings_json | provider, model, effort, card access, project folder; snapshot of config at capture, editable per thread from the queue window |
| session_json  | provider CLI session id of the last run, with provider and model (see "Replies")                                               |
| run_log_path  | last run's JSONL log (existing `JsonLineAgentRunLogger`)                                                                       |
| error         | last failure text, if any                                                                                                      |

**messages** (the transcript; `thread_id`, `seq`, `role` in {user, agent,
system}, `text`, `html`, `created_at`). The agent's compacted activity
summary is stored as a `system` message so the thread shows what the run did.

**proposals** (`thread_id`, `version`, `created_at`, `patch_json`,
`base_json`, `status` in {current, superseded, applied, discarded}). A reply
that yields a new patch creates version n+1 and marks n superseded. Only one
`current` per thread.

### Captured context

Everything the agent sees about _where the request came from_. Captured on the
main thread at send time; never refreshed afterwards (the user's motivation is
tied to what they were looking at).

- `source`: `edit_current` | `add_cards` | `browser` | `browser_multi`
- the existing `EditorSnapshot` / `MultiCardSnapshot` fields: note id,
  notetype, fields, tags, current field, selected text in the editor, images
- `note_mod` at capture
- **reviewer context**, when Edit Current was opened from the reviewer
  (`mw.reviewer.card` is the editor's card): `side` shown (`question` |
  `answer`), the rendered question and answer HTML (sanitized), and the
  reviewer webview's text selection at the moment the editor opened. This is
  what makes "the sentence in blue" or "the example" resolvable, since the
  editor sees fields, not the rendered card.

Stored as `context_json = {"source", "snapshot", "note_mod", "reviewer"}`:
`snapshot` is `snapshots.snapshot_to_dict` of the `EditorSnapshot` /
`MultiCardSnapshot` (with local image paths), `note_mod` is the editor note's
`mod` at capture (null for Add Cards and multi-card), and `reviewer` is
`{"side", "question_html", "answer_html", "selection"}` or null. The runner
appends `reviewer` to every prompt as a "Reviewer context as JSON" section.

Both the run and the pane's context label are built from this snapshot, so the
provider clients and patch validators work unchanged. Mode strings stay as
they are today (`add`, `edit_current`, `browse`, `browse_multi`).

### Patch schema

Today's schemas only update existing notes (`field_updates`/`note_updates`).
The motivating example needs a new note. One unified schema replaces both
existing ones (it is a strict superset, so existing prompts and tests
migrate mechanically):

```
patch: null | {
  summary: string,
  note_updates: [ { note_id, notetype_id, field_updates, tags } ],
  note_creates: [ { notetype_id, deck_id, fields: [{name, html}], tags: [string] } ],
  assumptions: [string],   // what the agent decided without asking
  questions:   [string]    // things it would have asked if the user were there
}
```

Validation for `note_creates` (new, in `patches.py`): notetype exists and its
field names match exactly; first field non-empty; deck exists (default: the
source card's home deck, or the Add Cards target deck); HTML fragment rules
as for updates. A duplicate check (`col.find_notes` on the first field, same
notetype) is run at review time and shown as a warning, not an error.

`assumptions`/`questions` exist because the user is usually _not_ watching
the run. The prompt tells the agent to make a best-effort proposal and record
what it guessed rather than stop and ask. The user answers by replying.

### Base revision and staleness

`base_json` records, for each note the patch touches, `note_id`, `note.mod`
and a hash of fields+tags at run time (not capture time: the agent may have
read a later state through lookups, and a reply re-reads). At apply time:

- note missing → **stale**, can only discard or re-run;
- `mod`/hash differ → **stale**; offer _re-run_ (worker gets the current note
  plus the thread history; prompt says "the note changed since your proposal,
  here is the current content, re-propose"), _apply anyway_ (field-level
  overwrite, clearly labelled), or _discard_;
- otherwise apply.

Created notes have no base and never go stale, but the duplicate check reruns.

In the editor pane this replaces today's `_context_generation` guard: a
proposal shown in the pane is applied against the note the editor currently
holds, and the base check catches a note edited by hand between proposal and
apply.

## State machine

```
queued ──► running ──► ready        (patch present)
                   ├─► answered     (message only, no patch)
                   └─► failed       (error; retry = back to queued)
ready ────► applied | discarded | stale
stale ────► queued (re-run) | discarded | applied (forced)
ready / answered / failed ──reply──► queued
failed ───► applied   (a failed run keeps the previous proposal current)
failed ───► stale     (its proposal's base changed; then applied (forced))
applied ──► ready | failed   (revert when the apply's collection write fails
                             before anything was written; a partial write stays applied)
```

A run that ends without a patch supersedes the thread's current proposal (a
null patch drops it); a failed run leaves it current. A run whose collection
is closed, or that is interrupted by shutdown, goes running → queued with no
error.

Anything in `queued`/`running` at Anki startup is reset to `queued` and the
worker resumes it (a run that died with Anki is simply re-run; the thread
history makes this idempotent from the user's point of view). `applied` and
`discarded` threads stay in the store for a configurable retention (default 30
days) and are hidden from the default queue view.

## Replies: session resume with replay fallback

A reply must give the agent the thread so far. Two mechanisms, used together:

- **Replay** (the only mechanism the pane has today): the stored transcript is
  re-sent as the `history` prompt section, plus the current proposal's JSON
  so "move the example to the back" amends the previous patch instead of
  starting over. Provider and model agnostic; the store is the single source
  of truth; works after CLI upgrades, session pruning, or a machine move;
  Ollama has nothing else. Costs: the agent keeps only what we stored (final
  message and proposal), so it redoes lookups and loses the reasoning behind
  its choices, and cost per turn grows with thread length.
- **Resume** (`claude --resume <id>`, `codex exec resume <id>`): the CLI
  session keeps the agent's full working context, including tool results and
  card lookups it already made. Cheaper and faster on long threads, and
  cache-friendly. Costs: ties the thread to one provider and model, session
  ids go invalid silently, and the store becomes a cache of a truth we do not
  own.

Policy: **replay is the baseline and resume is an optimisation on top.**

1. Every run records `session_json = {provider, model, session_id}` from the
   CLI's stream (`session_id` in Claude's envelope, `thread_id` in Codex's).
2. On reply, if the thread's provider and model are unchanged and
   `session_json` is present, the runner resumes and sends only the delta:
   the user's reply, and the current note state if `note_mod` changed since
   the last run (the resumed session still believes the note looks the way
   it did).
3. If the resume cannot be used (Ollama, provider or model changed) the
   runner replays; if the resume never started (unknown session, or the CLI
   exited before starting it) it falls back to replay in the same run,
   transparently. A failure after the resumed turn started (timeout, API
   error, bad response) is a normal failure, not a replay, so side effects
   and cost are not repeated. The transcript message records which path was
   taken.
4. Replay always builds from the store, never from the CLI's session files,
   so the two cannot disagree about what the user saw.

Phase 1 ships replay only; resume is added in phase 3 once the fallback path
is exercised.

## Components

All new modules live in `addons/editor_agent_pane/` next to the existing ones.

### `queue_store.py`

Pure Python over `sqlite3`, no Qt, fully unit-testable. CRUD for the three
tables, `next_runnable()`, `transition(thread_id, from, to)` enforcing the
state machine, `threads_for_note(note_id)`, retention sweep.

### `queue_runner.py`

One worker (configurable `queue_concurrency`, default 1; Codex/Claude runs are
heavy and sequential is predictable). Owned by the add-on at `install()`, so
it has no window. Loop:

1. pop next `queued` thread, mark `running`;
2. load the snapshot from `context_json`; for a reply or a stale re-run,
   refresh the note fields from the collection (main thread, `run_on_main` +
   `Future`, the same marshalling the pane already uses for lookups);
3. build the prompt: resume delta or replay, per the policy above;
4. run the provider client with `send_with_card_reads` (lookups marshalled to
   the main thread as today) and the thread's `stop_event`;
5. validate the patch, compute `base_json`, write messages/proposal and
   `session_json`, transition to `ready`/`answered`/`failed`;
6. notify: `gui_hooks`-style add-on signal `queue_thread_changed(thread_id)`
   consumed by open panes, the queue window, the badge, and a
   non-focus-stealing tooltip ("Agent: 1 proposal ready", configurable).

Stop/cancel: per-thread `Event`, as now. Cancelling a `running` thread marks it
`failed` with "cancelled"; the user can reply or retry.

Default card access for runs is `all` (read-only), overridable per thread
and in the pane. The motivating example (compare 唉声 with 呼声) needs to read
other cards, and the user is often not watching.

### Editor pane, thread-backed

`EditorAgentPane` keeps its UI (settings form, transcript webview, prompt
box, proposal diff, Apply/Discard/Stop) and changes what is behind it:

- **Send** captures the context, inserts a `queued` thread with `note_id`,
  and returns. The pane shows the thread's transcript with a "running"
  banner. The prompt box stays enabled, so a second request on the same note
  starts a second thread; threads are listed in a small selector above the
  transcript when there is more than one for the note.
- **Loading a note** (`editor_did_load_note`, browser row change) switches
  the pane to that note's threads (`threads_for_note`); the most recent
  non-terminal thread is selected. A thread whose run finishes while the
  pane is on another note just updates the store; the pane picks it up when
  the note is shown again.
- **Apply** and **Discard** are the same actions as in the queue window and
  go through the same code, including the base-revision check. In Edit
  Current, Apply still goes through `call_after_note_saved` so a half-typed
  field is not clobbered.
- **Reply** is just Send on an existing thread.
- **Closing the window** does nothing to running threads. Reopening the
  editor on the same note shows them.
- The chat-clearing behaviour on card-access change becomes "start a new
  thread", since a thread's permitted scope is part of its settings.
- A **Queue** button opens the queue window filtered to the current note, or
  to everything.

In Add Cards there is no note id yet, so the thread stores the captured
snapshot and the target deck; Apply creates the note (via `note_creates`,
or by filling the editor as today for a field patch) and sets `note_id`.

### `queue_window.py`

Non-modal `QMainWindow`-style dialog opened from Tools › Agent Queue, the
pane's Queue button, or `Ctrl+Alt+Shift+Q`. Also offered once per session when
the user leaves the reviewer with ready items (a tooltip with a link, not a
dialog).

Layout: thread list on the left, thread detail on the right.

List columns: status glyph, title, card excerpt, deck, age, proposal version.
Default filter hides applied/discarded; sort by status (ready first, then
answered, failed, running, queued) then age. Live-updates on
`queue_thread_changed`.

Detail (an `AnkiWebView` reusing `surface.py` rendering):

- the captured context (card excerpt, what side, selected text) collapsed;
- the transcript: user/agent messages, compacted activity, errors;
- the current proposal: per-note diff via the existing
  `render_proposal_diff`; for `note_creates`, a rendered preview of each
  generated card (`col.render` of the new note's templates) beside the field
  grid, plus the duplicate warning; `assumptions` and `questions` listed above
  the diff so the user sees what to correct first;
- a reply `PromptEdit` at the bottom.

Actions (buttons and shortcuts, all keyboard-reachable):

| action          | effect                                                                                                                                          |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Apply           | `CollectionOp` (`update_notes` + `add_note`), undoable; thread → `applied`; if an applied note's card is the reviewer's current card, redraw it |
| Apply selected  | for multi-note patches, the existing checkbox dialog                                                                                            |
| Reply           | append user message, thread → `queued`, selection moves to the next item; the thread returns to the top of the list when `ready` again          |
| Discard         | thread → `discarded`, with an optional reason stored as a message (helps the user remember why)                                                 |
| Retry / Re-run  | `failed`/`stale` → `queued`; a re-run on stale includes the current note state                                                                  |
| Open in Browser | `browser.search_for("nid:…")`                                                                                                                   |
| Apply then edit | apply, then open the note in Edit Current for manual touch-ups                                                                                  |
| Defer           | hides the thread until a chosen time; status unchanged                                                                                          |
| Change model    | edit `settings_json` (provider/model/effort/card access) for the next run                                                                       |

Reply must never block: the detail view keeps showing the last proposal with
a "running" banner, and the user can move on. This is the property that makes
the queue different from "approve/reject".

### Prompt changes

`PROMPT_TEMPLATE` and the Claude client's equivalent:

- the user may not be watching and cannot be asked now; propose, don't ask;
  record guesses in `assumptions` and open points in `questions`;
- new notes are allowed via `note_creates`; default notetype and deck are the
  source card's unless the request implies otherwise; keep field conventions
  of the source note (the context includes its fields, so the agent can copy
  the house style, e.g. the `notes:` field with pinyin and the `pleco`
  source line in the screenshot);
- reviewer context, when present: which side was shown, rendered HTML, and
  the reviewer selection, with the note that these are what the user saw;
- on replay, "Current proposal (version n) as JSON" is included; amend it. On
  resume, only the delta described above is sent.

### Config additions (`config.md`)

`queue_concurrency` (1), `queue_notify` (true), `queue_retention_days` (30),
`queue_resume_sessions` (true), `queue_shortcut`, `queue_window_geometry`.
`card_access` default changes from `current` to `all`.

## Risks and decisions

- **Collection access from the worker.** Only via `run_on_main`; the worker
  never touches `mw.col` on its thread. Same rule the pane follows today.
- **Applying while reviewing.** Allowed; apply goes through `CollectionOp`,
  which triggers the normal `operation_did_execute` refresh, and the reviewer
  redraw handles the current-card case. Scheduling is unaffected by field
  edits.
- **Edit Current cost.** Opening a window to capture a request is heavier
  than a dock on the main window. Accepted for simplicity: one capture
  surface. A reviewer-side dock over the same store can be added later
  without touching the model.
- **Auto-apply.** Not in v1. A later `auto_apply` policy (e.g. tag-only
  changes) is a natural knob once the user trusts the proposals.
- **Resume correctness.** A resumed session can hold beliefs about a note
  that no longer hold; the delta always includes the current note state when
  it changed, and `base_json` is recomputed from the collection on every run,
  never from the session.
- **Anki quit mid-run.** The CLI subprocess dies with Anki; the thread is
  re-queued at startup. No partial writes: nothing is written to the
  collection until Apply.
- **Pane behaviour change.** Today's pane forgets everything when the window
  closes; after this it remembers. Threads for a note are visible on every
  reopen until applied or discarded, which is the point, but the pane must
  make the status of each obvious at a glance.

## Phases

Each phase ends with `./check`, a PR, and a status update here. Red/green: the
store, state machine, unified-schema validation, staleness, and prompt
builder are pure Python and get tests in `qt/tests/test_agent_queue.py`
before the code; the runner is tested with a fake agent; the existing
`qt/tests/test_editor_agent_pane.py` is updated as the pane moves onto the
store.

**Phase 1 — store, runner, thread-backed pane (updates only, replay only).**
`queue_store`, `queue_runner`, pane on the store (Send/Apply/Discard/Reply
backed by threads, threads persist across window close and note switches),
reviewer context capture on Edit Current, resume at startup. Shippable on its
own: "fix this card" from the reviewer works end to end, with review done by
reopening the editor.

**Phase 2 — new notes and the queue window.** Unified patch schema with
`note_creates`, validation, card-render preview, duplicate warning,
`assumptions`/`questions`; `queue_window` with Apply / Reply / Discard / Open
in Browser. Covers the disambiguation-card example and cross-note review.

**Phase 3 — robustness and polish.** Session resume with replay fallback,
staleness + re-run, Retry/Change model, Defer, retention sweep, bottom-bar
badge, "Apply then edit".

## Results / notes

**Phase 1 (2026-10-22, uncommitted on `claude/agent-queue-phase1`).**
`queue_store.py`, `queue_runner.py`, `snapshots.py` (earlier commits), plus:

- `queue_runtime.py`: opens the store at `<profile>/agent_queue.sqlite` on
  `profile_did_open` (sweeping with `queue_retention_days`), starts one
  `QueueRunner` (`queue_concurrency`), closes on `profile_will_close`.
  `run_on_main` blocks the worker on a `Future` and raises `AgentStopped` once
  the profile is closing, so shutdown never deadlocks; the runner re-queues a
  thread interrupted that way. `notify` fans out to open panes on the main
  thread and shows a tooltip for ready/answered/failed (not for a user cancel)
  when `queue_notify` is on. Streamed activity lines reach panes through a new
  `QueueRunner(on_stream_event=...)` hook (also emits the card-read line).
- `thread_view.py`: pure helpers (thread picking, transcript fragments, status
  banner, base-revision check `stale_reason`, reviewer HTML cleanup).
- `runtime.py`: the pane is a view over the store. Send creates a thread
  (settings snapshot from the form, including `deck_ids`), or replies when the
  shown thread is ready/answered/failed; the prompt box stays enabled; Stop =
  `runner.cancel`; Discard/Apply transition the thread. A thread selector shows
  when a note has more than one thread. The in-memory run path (`history`,
  `pending_patch`, `_start_agent_request`, `_handle_agent_done`) is gone.
- Reviewer context is captured at Send in Edit Current on the reviewer's card.
- Config: `queue_concurrency`, `queue_notify`, `queue_retention_days`;
  `card_access` default is now `all`.

Deviations:

- Staleness: a stale proposal is refused with a warning and stays `ready` (no
  transition to `stale`, since phase 1 has no re-run and `stale` cannot be
  replied to). The check compares `mod` from the collection and the fields/tags
  hash of the note as currently held by the editor (which may have unsaved
  typing). Multi-note apply uses the same base check, then validates against the
  snapshot refreshed to the current notes.
- Card-access change no longer clears anything; it makes the next Send start a
  new thread. "Reset chat" became **New request** (deselects; nothing deleted).
- Multi-card Browser threads, like Add Cards threads, have `note_id = null` and
  are listed by the pane that created them for its lifetime, filtered to the
  same card selection. Phase 2's queue window is how to find them later.
- The multi-card review dialog opens when the shown thread finishes running,
  not on every re-render.
- System messages store only the compact activity summary; the expandable
  per-line details of a finished run are not persisted.
- `retention sweep` runs in phase 1 (once per profile open), not phase 3.

Not done in phase 1: Queue button/window, `note_creates`, resume, re-run on
stale, `queue_resume_sessions`/`queue_shortcut`/`queue_window_geometry`.

Live check (2026-10-22, second Anki instance from this checkout, own base
folder, driven through a control add-on; provider Ollama because Claude and
Codex both failed on account/environment grounds from inside that session):

- Edit Current → Send creates a thread with the reviewer side, rendered HTML
  and selection captured; closing the editor mid-run does not stop the run;
  the result persists and the reopened pane selects it with Apply enabled.
- A failed thread survives an Anki restart (`reset_interrupted` path), and a
  later Send on it is a reply.
- Reply on a ready thread yields proposal v2 with v1 superseded; Apply writes
  the note and marks v2 applied. Live re-render on completion works with the
  editor open.
- Fixed from the check: a reply now takes the pane's current provider, model
  and card access (`QueueStore.set_settings`), since the stored settings were
  silently reused after changing the form; and the reviewer prompt now says
  the rendered HTML is not field content (a small model copied the rendered
  answer into the Back field).
- Observed and reproduced _without_ the pane in that instance: a cold-start
  Edit Current that is closed writes an empty Back field (Front intact).
  Pre-existing editor behaviour in the hidden-window harness, related to the
  blank-webview work in editor-blank-webview-recovery-plan.md; not caused by
  this branch, but worth a separate look.

**Phase 2a (2026-10-23, uncommitted on `claude/agent-queue-phase2`).** New
notes and the unified patch schema; the queue window is phase 2b.

- Schema: one `PATCH_SCHEMA` (`summary`, `note_updates`, `note_creates`,
  `assumptions`, `questions`, all required) for Codex `--output-schema` and
  Claude `--json-schema`; the prompt's "When proposing a patch" section
  describes only it, plus "the user may not be watching: propose, record
  guesses in assumptions and open points in questions". Ollama's normalizer
  fills defaults for the unified shape and still accepts the legacy list and
  top-level `field_updates` shapes; its repair prompt describes the new shape.
- `patches.py`: `NoteCreate`, `NotetypeInfo`; `note_creates`/`assumptions`/
  `questions` on `NotePatch` and `MultiNotePatch`; `has_changes()` counts
  creates (`NotePatch.has_note_update()` is "changes the current note").
  `validate_patch(raw, snapshot, *, notetypes, deck_ids)` dispatches on the
  snapshot kind and accepts the legacy top-level `field_updates` shape.
  Creates: note type exists, field names match exactly (stored in the note
  type's order), first field non-empty after stripping HTML (media counts as
  content), deck exists, same HTML-fragment rules, tags normalized. Single-note
  `note_updates` has at most one entry, for the current note (`note_id` null in
  Add Cards). `validate_note_patch`/`validate_multi_note_patch` remain as
  wrappers.
- Create ids: a missing/null/empty `notetype_id` or `deck_id` takes
  `CreateDefaults` (the runner passes `creation_defaults`: source note type and
  deck); numeric strings and a note type's name (case-insensitive) are accepted;
  structure-only (client) validation falls back to the snapshot's note type and
  leaves the deck None; with the catalog, no default is an error ("New note 1:
  no deck_id and no default deck."). Apply refuses a None deck.
- `snapshots.patch_to_dict` writes the unified shape;
  `patch_from_dict(data, snapshot)` now takes the thread's snapshot (the shape
  no longer says single vs multi) and still reads phase-1 rows.
- Runner: `read_catalog` (note types with field names, normal decks) via
  `run_on_main` once per run; a "Available note types and decks as JSON"
  prompt section lists the source note type and the default deck first,
  marked `default`. The default deck is captured at Send as
  `context_json.deck_id` (card's home deck, Add Cards target deck, or the first
  selected card's deck). `base_json` lists only updated notes.
- Store: schema v2 adds `proposals.result_json`;
  `set_proposal_result(thread_id, version, result)` records
  `{"created_note_ids": [...]}`, which the transcript shows as `nid:` links
  that open the Browser.
- Rendering: assumptions and questions above the diff; per new note the note
  type and deck names, a field grid, and each card it would generate
  (`col.new_note` → fields/tags → `ephemeral_card(ord)` → `question()`/
  `answer()` → style stripped, sanitized; cloze notes use
  `cloze_numbers_in_fields()`), falling back to the grid alone. Duplicate
  warning from Anki's dupe search, `"dupe:<notetype id>,<first field>"`
  (quotes and backslashes escaped), run at render time: "Possible duplicate of
  nid … (Front: …)", first three hits. Collection access lives in `preview.py`;
  HTML in `surface.py`.
- Apply: `preview.apply_proposal_changes` saves updated notes and adds the new
  ones inside one `CollectionOp` under a custom undo entry merged with
  `merge_undo_entries`, so one Undo reverts the whole apply. The thread is
  marked applied before the op and reverted on failure (as the multi-note
  path). Edit Current/Browser: the editor is filled through
  `call_after_note_saved` as before, and when the patch also creates notes the
  current note is saved in that same op instead of by `update_note`. A
  creates-only proposal (single or multi) applies directly: no editor fill, no
  dialog, and in Add Cards no unsaved-note staleness check.

Deviations:

- Clients validate structure only (`notetypes=None, deck_ids=None` skips the
  collection checks); the runner re-validates every patch against the catalog,
  so the collection checks never depend on which client ran. A rejected patch
  fails the run with "The agent's proposal was rejected: …".
- The card preview uses `ephemeral_card` + `question()`/`answer()` rather than
  `col.render` directly (same renderer underneath). For standard note types
  every template is previewed, including ones whose front would render empty
  (Anki would not generate those cards).
- Multi-note proposals that also create notes: the review dialog shows the new
  notes on every page and "Apply checked" applies the checked updates plus all
  new notes.
- If the combined op fails after the editor was filled (Edit Current, update
  plus creates), the thread returns to ready but the editor keeps the filled
  fields unsaved; a retry then reports the note as changed.

**Phase 2b (2026-10-23, uncommitted on `claude/agent-queue-phase2`).** The
queue window.

- `thread_panel.py` (new): what the pane and the window share. `ThreadPanel` is
  a mixin over a widget with a transcript `surface` and Apply/Discard/Stop
  buttons: rendering the selected thread (transcript, created-note links,
  status banner, current proposal with diff / create previews / assumptions
  and questions), `_current_proposal`, the base-revision check, Apply (single,
  creates-only, multi-note checkbox dialog), Discard, Stop and the `nid:` link
  bridge command. The host supplies `editor` (or None), `browser` (or None)
  and `_broadcast_thread_changed`; hooks `_context_fragment` (window: the
  captured context) and `_after_thread_rendered` (pane: open the multi-card
  review when its run finishes; window: Reply/Open in Browser buttons).
  `PromptEdit`, `MultiCardProposalDialog` and `prepare_multi_note_updates`
  moved there too (re-exported from `runtime`). `EditorAgentPane` is now
  `ThreadPanel, QWidget`; its only apply-specific override is
  `_apply_note_update`, which fills the editor after `call_after_note_saved`
  as before. Without an editor (the window), `_apply_note_update` checks
  `stale_reason` against the collection's note, applies the field/tag update to
  `col.get_note` (`prepare_note_update`) and saves it with any new notes in the
  same `apply_proposal_changes` `CollectionOp`.
- `queue_window.py` (new): `AgentQueueWindow(ThreadPanel, QMainWindow)`, child
  of the main window, one per main window (`open_window` re-raises it and
  takes the new note filter). Opened from Tools > Agent Queue, the
  application-wide `queue_shortcut` (default `Ctrl+Alt+Shift+Q`) and the
  pane's **Queue** button (filtered to the pane's note; "Note N only" toggles
  show-all). Left: a `QTableWidget` (status glyph, title, card excerpt, deck,
  age, latest proposal version) with "Show applied and discarded"; right: the
  shared panel, a reply `PromptEdit` and Apply (Ctrl+Shift+A), Reply
  (Ctrl+Shift+R, or Enter in the box), Discard (Ctrl+Shift+D, optional reason
  stored as a user message "Discarded: …"), Open in Browser (Ctrl+Shift+B,
  `nid:` of the thread's note(s) plus notes its applies created), Stop
  (Ctrl+Shift+S); Escape closes. Geometry and splitter sizes are saved to
  `queue_window_geometry` / `queue_window_splitter` on close.
- List order (`thread_view.queue_threads`): ready, answered, failed, running,
  queued (then stale, applied, discarded when shown), newest `updated_at`
  first. When the selected thread leaves the list (applied, discarded) the
  selection moves to the row that followed it; Reply moves it to the next row
  explicitly, and the replied thread drops to the queued group until its run
  finishes and it sorts back to the top.
- Live updates: `runtime._all_panes()` also yields the open window, so
  `queue_runtime.thread_changed`/`thread_activity` reach it. A change to the
  selected thread re-renders the detail; any other change only refreshes the
  rows.
- Reviewer and editors after Apply: nothing extra was needed. The op's
  initiator is the window, so `operation_did_execute` reloads an open Edit
  Current or Browser editor showing the note (`editor.set_note`), and the
  reviewer marks `NOTE_TEXT` and redraws the current card when the main window
  regains focus (`Reviewer.op_executed` / `refresh_if_needed`).
- Offer: leaving the reviewer (`state_did_change` from `review`) with ready or
  answered threads shows "Agent queue: N requests ready for review
  (Ctrl+Alt+Shift+Q)" once per profile session (reset on `profile_did_open`;
  not used up while nothing is waiting).
- Tests: `qt/tests/test_agent_queue_window.py` (26), driving the window
  through `_init_state` and fake widgets like the pane tests. The pane test
  stub importer now also drops stale `editor_agent_pane` package attributes
  (`from . import x` reuses them) and patches UI helpers in both `runtime` and
  `thread_panel` (`_patch_ui`). The real window was also smoke-built under
  offscreen Qt (menu action, list, selection, geometry save, reopen).

Deviations:

- The offer is a plain tooltip naming the shortcut, not a link: Anki's
  `tooltip` label hides itself on any click, so a link in it cannot work.
- Reply from the window keeps the thread's stored provider/model/settings (the
  pane uses its form; changing them per thread is phase 3's Change model). The
  window's reply box has no prompt history.
- Apply from the window to a note that an open editor shows writes the
  collection note; unsaved typing in that editor is replaced by the reload.
  The pane's own Apply still goes through the editor.
- An Add Cards proposal that fills the unsaved note can only be applied from
  its Add Cards pane; the window refuses it with a message (creates-only Add
  Cards proposals apply from the window).
- The applied/discarded status banner still says "Send starts a new request"
  (pane wording) in the window.
- The window is filtered by note id; multi-card threads match any of their
  selected notes.

Review fixes (2026-10-23):

- Apply builds and checks every new note (note type exists, every field name
  present) before the first write; a write failing after an earlier one raises
  `PartialApplyError`, which keeps the thread applied, stores the partial
  `created_note_ids` plus `error` in `result_json` and adds a system message.
- The multi-card dialog carries the proposal version it shows: Apply checked
  refuses ("The proposal changed…") when it is no longer current, and
  `on_thread_changed` closes it and reopens it on the new version.
- `QueueStore.discard(thread_id, reason)` checks the status, adds "Discarded: …"
  and transitions in one transaction; the pane and window warn when it refuses.
- The v2 migration runs in one explicit transaction and skips the ALTER when
  `proposals.result_json` already exists.
- A run whose note was deleted fails with "The note no longer exists." before
  the agent is called (multi: "The selected notes no longer exist." when all
  are gone; otherwise the deleted ones are dropped from the snapshot and a
  system message names them).

**Phase 3a (2026-10-23, uncommitted on `claude/agent-queue-phase3`).** The
runner side of phase 3; the UI (buttons, Defer, badge, Apply then edit) is 3b.

- Sessions: `AgentResult.session_id` (Claude: `session_id` of the stream-json
  init/result events; Codex: `thread_id` of `thread.started`; Ollama: None).
  Every successful run stores `session_json = {provider, model, session_id,
  note_mods}` through `complete_run(session=…)` in the run's transaction
  (`note_mods`: `{"<nid>": mod}` the run saw); a run without a session id clears
  it, and a failed run clears it too (`clear_session=True`), so the turn after a
  failure always replays.
- Resume (`queue_resume_sessions`, default true, read live from the config):
  when the thread's session has the current provider and model (never Ollama),
  the client is called with `resume_session_id` and the delta prompt only: the
  pending user turn(s), plus "Current note state as JSON" when a note's `mod`
  differs from `note_mods`, plus the stale section (below). The reviewer context
  (immutable), the catalog and the current-proposal section are not resent; the
  card-access section is, as on every round. `ClaudeCliAgent.send(…,
  resume_session_id)` adds `--resume <id>`; `CodexCliAgent.send(…,
  resume_session_id)` runs `codex exec --sandbox … --skip-git-repo-check --color
  never --cd <cwd> resume -c sandbox_mode="…" [-c …] --json --output-schema …
  --output-last-message … [--model m] <id> -` (resume itself accepts no
  `--sandbox/--cd/--color`; checked against codex-cli 0.159.3), and both send
  `RESUME_PROMPT_TEMPLATE` (the new turn plus "earlier instructions, context and
  response format still apply") instead of the full template. Any exception
  from the resumed send except a stop/closed collection falls back to a replay
  in the same `run_once`; the transcript gets "[Resumed session <id>]" or
  "[Replayed transcript (resume failed: <error>)]" (bracketed, so the pane
  shows it as an activity line), and the activity summary is the replay's.
- Persistence: resuming needs the sessions on disk, so while
  `queue_resume_sessions` is on the runner builds clients with
  `persist_session=True` (Claude drops `--no-session-persistence`, Codex drops
  `--ephemeral`). Live check (Claude 2.1.301, haiku): a persisted run, then a
  `--resume` from a different temp cwd, recalled the first turn; an unknown id
  exits 1 with "No conversation found", which triggers the fallback. Codex
  resume could not be run live (the account rejects every model in this
  environment); the flag layout was checked with `codex exec resume` against a
  real and an unknown thread id.
- Staleness: `QueueStore.mark_stale(thread_id, reason)` (ready -> stale, reason
  as a system message, proposal stays current), `rerun(thread_id)`
  (stale/ready/failed -> queued, adds `RERUN_MESSAGE` "[Re-run requested: the
  note changed]", clears the error), `retry(thread_id)` (failed -> queued, no
  message). `ThreadPanel._refuse_stale` replaces the stale warning in the three
  apply paths (window/collection, multi-note, and the pane's editor path for
  notes with an id): it marks the thread stale, broadcasts, then warns.
  `thread_view`: `APPLICABLE_STATUSES` includes `stale` (for the forced apply;
  the base check still refuses a plain Apply), `RETRYABLE_STATUSES`,
  `RERUNNABLE_STATUSES`; `REPLYABLE_STATUSES` excludes `stale`; stale sorts right
  after ready in the queue list and counts as a result in `pick_thread`.
- Runner on any queued thread with a current proposal: `base_changed(base,
  states)` against the collection; if it changed, the prompt gets "The note
  changed since your proposal (version n)…" with the current note
  (`snapshot.as_tool_result()`) and the previous patch, replacing the
  current-proposal section on replay and also sent on the resume delta (then
  without a separate note-state section). `pending_request` re-sends the last
  user turn when no user turn is pending and the transcript ends with a system
  message (a retry after a failure, or a re-run after an answered turn); a
  thread ending with the agent's answer still fails with `NO_PENDING_REQUEST`.
- Catalog trimming: `read_catalog` also returns `usage` (`[deck, note type,
  notes]` per home deck, one grouped query over cards/notes on the main
  thread). `catalog_prompt_section(…, limit)` (`trim_catalog`) lists the source
  deck and its `::` subdecks plus Anki's Default deck (id 1), and the note types
  used in that subtree (most notes first) plus the source note type, defaults
  first, each capped at `queue_catalog_limit` (30); the header says how many were
  omitted and that any other existing id is accepted. Without a source deck or
  usage data it lists everything up to the cap. `validate_against_catalog`
  still uses the full catalog.
- Config: `queue_resume_sessions` (true), `queue_catalog_limit` (30).
- Tests: 43 new (store transitions and session column, resume/delta/fallback
  and ineligible cases, stale re-run on both paths, retry, catalog trimming,
  Claude/Codex resume commands and session ids, live config read); three
  existing apply tests now expect `stale` instead of `ready`.

Deviations:

- `session_json` also carries `note_mods`, the per-note `mod` the session last
  saw, which is what "note changed since the last run" compares against
  (`base_json` only exists for runs with a proposal).
- "Default deck" in the trimmed catalog is Anki's Default deck (id 1), beside
  the source deck's subtree (which already includes the deck marked default).
- A failed run clears the session rather than keeping it: a failed resumed
  turn (e.g. a rejected patch) is already inside the CLI session, so resuming
  it would show the agent a turn the store never recorded.
- With resume on, card-lookup rounds resume the same session again with the
  full delta plus the lookup results so far (Claude and Codex keep the id), so
  the session holds each round; replay rounds each start a fresh persisted
  session, and the last one is stored. Persisted sessions accumulate in the
  CLIs' own session folders (Claude: one project folder per temp cwd).
- A re-run compares against the collection: a proposal marked stale only
  because the editor holds unsaved typing gets no "note changed" section until
  the note is saved.

**Phase 3b (2026-10-23, uncommitted on `claude/agent-queue-phase3`).** The UI
half of phase 3.

- Stale handling, shared by the pane and the window (`ThreadPanel`): **Re-run**
  (`store.rerun` → `runner.enqueue_kick()` → broadcast; enabled for
  `RERUNNABLE_STATUSES`: stale, ready, failed), **Retry** (`store.retry`, shown
  only for failed threads), **Apply anyway** (shown only for stale threads with
  a current proposal; labelled "the proposed fields and tags overwrite the
  current ones") and Discard. Plain Apply is disabled on a stale thread and
  still refuses if invoked (shortcut): `_apply_pending_patch(force=False)` keeps
  the base check, `force=True` skips it in all three apply paths (collection,
  the pane's editor fill, and the multi-note dialog, which carries `force` to
  "Apply checked"). The write is field-level: `prepare_note_update` /
  the editor fill change only the patch's fields and tags on the current note.
  A forced apply whose write fails returns the thread to `stale` (new store
  edge applied → stale). In the window Re-run/Retry move the selection on like
  Reply (`_following_thread_id`/`_after_requeue` hooks); the pane stays on the
  thread. The buttons are optional attributes on `ThreadPanel`, so a host
  without them does not offer the action.
- Transcript: a system message that is a stale reason (`STALE_WARNING`,
  `MISSING_NOTE_WARNING`) renders as a "Stale" notice (`render_notice_message`,
  class `agent-notice`), not the Error box; `RERUN_MESSAGE`, "[Resumed session
  …]" and "[Replayed transcript …]" render as one-line activity entries
  (`render_activity_note`) instead of an expandable summary with nothing in it.
  `status_fragment(thread, closed_hint=…)`: the window's applied/discarded
  banner is "[Applied.]" / "[Discarded.]"; the pane keeps "Send starts a new
  request.".
- Defer: store schema v3 adds `threads.deferred_until` (real, nullable), same
  idempotent single-transaction migration as v2 (`_MIGRATE_V3`, skipped when the
  column exists). `store.defer(thread_id, until)` / `undefer(thread_id)` change
  neither the status nor `updated_at` (raise `QueueTransitionError` for a
  missing thread). `list_threads(statuses, *, include_deferred=False, now=None)`
  leaves out rows whose `deferred_until` is after `now`; the runner's startup
  scan passes `include_deferred=True`. Window: a **Defer** button
  (Ctrl+Shift+L) with a menu (for an hour, until tomorrow 08:00, until next
  week = next Monday 08:00, "Until a date and time…" via a `QDateTimeEdit`
  dialog defaulting to tomorrow 08:00, and Undefer when the thread is
  deferred) and a "Show deferred" checkbox beside "Show applied and
  discarded". Deferred rows get `⏸` after the status glyph and "until 08:00" /
  "until Thu 08:00" / "until 30 Oct 08:00" in the age column
  (`thread_view.queue_row`, `defer_choice_time`, `until_text`, local time).
  The reviewer-exit tooltip count and the badge exclude deferred threads.
- Change model: the window's detail has a **Next run** row (provider, model,
  effort, card access combos and "Use for next run"), prefilled from
  `thread.settings` on every render. Options come from new shared helpers
  `model_options.model_options_for(provider, model, ollama_models)` and
  `effort_options.effort_options_for(provider, model)` (the same tables the
  pane's form uses; Ollama offers only "Model default" effort), with the pane's
  `effort_choice_for_options`/`effort_options_with_legacy` and
  `CARD_ACCESS_OPTIONS`. Ollama models are discovered once per window in the
  background (`discover_ollama_models` with the thread's `paths` or the config,
  as the pane does). Saving calls `store.set_settings(thread_id,
  merge_thread_settings(...))`, which overlays provider/model/effort/card
  access on the stored dict (paths, project folder, timeouts, instructions
  kept; an effort the model does not take falls back to default; "deck" access
  without stored `deck_ids` takes the captured deck(s), `thread_deck_ids`),
  and shows "The next run of this request uses Claude sonnet.". It is enabled
  for every non-closed thread; it applies to the next reply, re-run or retry.
- Apply then edit: **Apply and edit** (Ctrl+Shift+E) beside Apply in the window.
  It marks the thread, and the new `ThreadPanel._after_apply(thread_id,
  updated_ids, created_ids)` hook (called after a successful collection write,
  single or multi-note) opens `aqt.dialogs.open("Browser", mw,
  search=("nid:…",))` with the updated note(s), else the first created note.
  Nothing opens after a failed or refused apply.
- Badge: `queue_badge.py` adds "Agent: N ready" (ready + answered + stale,
  excluding deferred) to the top toolbar through `top_toolbar_did_init_links`,
  registering its link handler on the toolbar; it opens the queue window. At 0
  the link is drawn with `display:none`. Updates do not redraw the toolbar
  (`toolbar.draw()` reloads its whole page): `refresh()` runs one `eval` on
  `mw.toolbar.web` that sets the link's text and visibility. Its listener is in
  `runtime._all_panes()`, so every `thread_changed` fan-out refreshes it; it
  also refreshes on `profile_did_open` and `state_did_change` (a deferral that
  ends changes nothing in the store). `queue_runtime.thread_changed(...,
  notify=False)` is used for defer/settings changes so they do not trigger the
  "proposal ready" tooltip.
- Config: `queue_show_badge` (true), documented in config.md with the new
  window actions and the stale behaviour.
- Tests: 44 new (512 pass, 1 skipped): store defer/undefer/migration/half
  migration/forced-apply revert; transcript notice and activity lines; banner
  wording; defer times and deferred row cells; settings merge and option
  helpers; window actions per status, re-run/retry/apply anyway (single,
  failed-write revert, multi-note force), defer + show deferred + pick +
  offer count, settings row prefill/merge/Ollama, apply and edit (updated,
  first created, not on plain apply, not on failure), badge text, hidden at
  zero, live update, click, config off; pane stale buttons, re-run, plain
  Apply refusing then Apply anyway, retry. The real window (menu, dialog,
  combos) was smoke-built under offscreen Qt.

Deviations:

- "Apply then edit" opens the note in the Browser (as the brief asked), not in
  Edit Current as the actions table says.
- Plain Apply is disabled (not just refused) on a stale thread; Apply anyway
  is the stale action. Re-run in the pane uses the thread's stored settings
  (only Reply takes the pane's form).
- Defer is allowed in any non-closed status and does not stop or delay a
  queued run; a deferred thread whose run finishes stays hidden until its time.
  Neither the window list nor the badge refreshes by itself when a deferral
  ends; the badge catches up on the next state change or thread change, the
  list on its next refresh.
- The settings row is re-filled whenever the shown thread re-renders (e.g. its
  run finishes), dropping unsaved choices in the row.
- Badge visibility follows `queue_show_badge` at the next toolbar draw.

Not done: a clickable reviewer offer (Anki's tooltip swallows clicks, so it
still names the shortcut); per-thread instructions/project folder editing in
the Next run row; live checks of the window's new actions and the badge in a
running Anki (only offscreen construction and unit tests); Codex session resume
was never run live (3a). Everything else listed for phase 3 is in.

**Phase 3 review fixes (2026-10-23, uncommitted on `claude/agent-queue-phase3`).**

- Replay fallback only when the resume never started: the clients raise
  `ResumeUnavailable` (a `RuntimeError`, in `codex_client`) when a resumed run
  exits non-zero before starting (Claude: no `system/init` event; Codex: no
  `thread.started`) or the CLI names the session unknown (Claude "No
  conversation found", on stderr and in the result event's `errors`; Codex
  "no rollout found for thread id", stderr, checked with `codex exec resume
  <bogus-uuid>` on codex-cli 0.159.3). The runner catches only that; a
  timeout, lookup limit, parse/schema failure or API error after the turn
  started fails the run. `send_with_card_reads` turns a `ResumeUnavailable`
  on a later lookup round into a plain error.
- Apply anyway with the multi-card dialog already open: the dialog no longer
  carries `force`; `_apply_checked_multi_patch` forces when the thread is
  stale at click time, so the raised dialog applies.
- A failed thread whose proposal base changed can be marked stale (store
  edge failed -> stale; `mark_stale` accepts ready or failed), then
  force-applied.
- With `queue_resume_sessions` off, runs store no session (cleared).
- A requeued run (closed collection, shutdown) and `reset_interrupted` clear
  the session, so the interrupted turn is replayed.
- `set_settings` / `set_session` leave `updated_at` alone, so changing a
  thread's settings does not move it in the queue.
- `merge_thread_settings(…, config=)`: a provider change resets `fast_mode`
  and `stream_reasoning_summaries` (Codex: from the add-on config; others:
  off) and clears `project_folder` for Ollama, as the pane does.
- Images on resumed turns: when the delta resends the note (note state or
  stale section) and the note has images, Codex resumes with
  `resume_images=True` (`--image` after `resume`); Claude replays instead of
  resuming.
- Catalog: a source deck with no usage rows and no source note type lists
  the first note types of the full catalog (up to the cap) instead of none.
- Tests: 17 new (16 in test_agent_queue.py, 1 pane dialog test); two window
  tests updated (dialog no longer gets `force`; a provider switch now adds
  the reset flags), two store tests updated (failed is no longer refused by
  `mark_stale`; the fallback test raises `ResumeUnavailable`). 529 pass, 1
  skipped.
