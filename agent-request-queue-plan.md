# Agent request queue — design and spec

**Status:** design. Nothing implemented yet. Phases at the bottom.

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
```

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
3. If the resume fails for any reason (unknown session, CLI error, Ollama,
   provider or model changed), the runner falls back to replay in the same
   run, transparently. The transcript message records which path was taken.
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

(Update as phases land.)
