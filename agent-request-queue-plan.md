# Agent request queue — design and spec

**Status:** design. Nothing implemented yet. Phases at the bottom.

Builds on the repo-local add-on in [addons/editor_agent_pane](addons/editor_agent_pane),
which already has the provider clients (Codex, Claude, Ollama), the fixed JSON
response schema, patch validation, diff rendering, read-only card lookups, and
run logging. This plan reuses all of that and changes what is _around_ it.

## Problem

While reviewing, the user often wants an agent to change something (fix a
field, add a note, write a disambiguation card for 唉声 vs 呼声). Today the
only path is the editor agent pane, which has three couplings that make this
impossible without leaving the review flow:

- a request can only be made from an open editor window (Add, Browser, Edit
  Current), so you must open one and find the card;
- the run is owned by that window's pane: closing the window kills it, and the
  pane is blocked until the run finishes;
- the proposal lives only in the pane's memory, so it must be approved or
  discarded then and there. There is no "look at it later", and "later" also
  loses the context that motivated the request.

## The shape of the solution

Separate the three things the pane couples, and give the middle one a durable
identity:

1. **Capture** (cheap, during review): type a request, the card context is
   attached automatically, done. Focus returns to the reviewer.
2. **Run** (background, window-independent): a worker owned by the add-on, not
   by any window, runs the agent and stores the result.
3. **Review** (later, at the user's pace): a queue window where each item is a
   _thread_, not a proposal. A thread holds the conversation, the context it
   was captured with, and the current proposal. Reviewing an item means
   applying, discarding, **or replying**, and a reply sends the thread back
   to the worker and produces the next proposal version without blocking the
   user from moving to the next item.

The perspective that makes the rest fall out: **the queue is a pull-request
list for your collection.** The agent authors PRs, each PR has a thread, the
user leaves review comments, merge = apply. Everything below is the usual PR
machinery: status, base revision, stale detection, re-review after changes.

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
| context_json  | captured context (below); immutable after capture                                                                              |
| settings_json | provider, model, effort, card access, project folder; snapshot of config at capture, editable per thread from the queue window |
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

- `source`: `reviewer` | `overview` | `deck_browser` | `editor` | `browser`
- `card_id`, `note_id`, `deck_id`, `notetype_id` (nullable: a free-form
  request from the deck list has no card)
- `fields`, `tags` of the note at capture time, plus `note_mod`
- `side`: `question` | `answer` (what the reviewer was showing)
- `selected_text`: `window.getSelection()` in the reviewer webview, if any
- `rendered_question_html`, `rendered_answer_html` — what the user actually
  saw; useful for "the thing in blue", "the example sentence". Sanitized.

This is turned into the existing `EditorSnapshot` shape by a new
`note_snapshot(col, note, card)` builder so the provider clients and patch
validators work unchanged. Mode string: `"queue"`.

### Patch schema extension

Today's schemas only update existing notes (`field_updates`/`note_updates`).
The motivating example needs a new note. Queue runs use one unified schema:

```
patch: null | {
  summary: string,
  note_updates: [ { note_id, notetype_id, field_updates, tags } ],   // existing
  note_creates: [ { notetype_id, deck_id, fields: [{name, html}], tags: [string] } ],
  assumptions: [string],   // what the agent decided without asking
  questions:   [string]    // things it would have asked if the user were there
}
```

Validation for `note_creates` (new, in `patches.py`): notetype exists and its
field names match exactly; first field non-empty; deck exists (default: the
source card's home deck); HTML fragment rules as for updates. A duplicate
check (`col.find_notes` on the first field, same notetype) is run at review
time and shown as a warning, not an error.

`assumptions`/`questions` exist because the user is _not_ available during the
run. The prompt tells the agent to make a best-effort proposal and record what
it guessed rather than stop and ask. The user answers at review time by
replying.

The editor pane keeps its current schemas in phase 1 and migrates to the
unified one later (it is a strict superset).

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

## Components

All new modules live in `addons/editor_agent_pane/` next to the existing ones.

### `queue_store.py`

Pure Python over `sqlite3`, no Qt, fully unit-testable. CRUD for the three
tables, `next_runnable()`, `transition(thread_id, from, to)` enforcing the
state machine, retention sweep.

### `queue_runner.py`

One worker (configurable `queue_concurrency`, default 1; Codex/Claude runs are
heavy and sequential is predictable). Owned by the add-on at `install()`, so
it has no window. Loop:

1. pop next `queued` thread, mark `running`;
2. build `EditorSnapshot` via `note_snapshot` (main thread, `run_on_main` +
   `Future`, same marshalling the editor pane already uses for lookups);
3. build the prompt from the thread's messages (as the existing `history`
   argument) **plus the current proposal's JSON** when replying, so "move the
   example to the back" edits the previous patch rather than starting over;
4. run the provider client with `send_with_card_reads` (lookups marshalled to
   the main thread as today) and the thread's `stop_event`;
5. validate the patch, compute `base_json`, write messages/proposal, transition
   to `ready`/`answered`/`failed`;
6. notify: `gui_hooks`-style add-on signal `queue_thread_changed(thread_id)`
   consumed by the badge, the queue window, and a non-focus-stealing tooltip
   ("Agent: 1 proposal ready", configurable).

Stop/cancel: per-thread `Event`, as now. Cancelling a `running` thread marks it
`failed` with "cancelled"; the user can reply or retry.

Default card access for queue runs is `all` (read-only), overridable per
thread. The reviewer pane's whole point is autonomy, and the motivating
example (compare 唉声 with 呼声) needs to read other cards.

### `reviewer_pane.py`

A `QDockWidget` on the main window, hidden by default, toggled by the existing
`Ctrl+Alt+Shift+E` (currently editor-only) from the review, overview, and deck
browser states. Contents, top to bottom:

- one-line context label: "Card: 不要受了一点儿挫折就唉声叹气。 (Chinese::Pleco)",
  or "No card" from the deck list;
- the existing `PromptEdit` (Enter sends, Shift+Enter newline, history recall);
- a status line: "2 queued · 1 running · 3 ready" that doubles as a button to
  open the queue window.

Sending captures the context, inserts a `queued` thread, shows a 1.5 s
tooltip, clears the prompt, and returns focus to the reviewer webview so the
next keypress is a review answer. Esc does the same without sending. There
are no provider/model controls here; the thread takes the saved config (the
editor pane's settings form is the place to change defaults).

A small count in the main-window bottom bar ("Agent: 3 ready") is a phase 3
nicety.

### `queue_window.py`

Non-modal `QMainWindow`-style dialog opened from Tools › Agent Queue, the
pane's status line, or `Ctrl+Alt+Shift+Q`. Also offered once per session when
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

`PROMPT_TEMPLATE` gets a queue-mode section (and the Claude client's
equivalent):

- the user is reviewing and cannot answer now; propose, don't ask; record
  guesses in `assumptions` and open points in `questions`;
- new notes are allowed via `note_creates`; default notetype and deck are the
  source card's unless the request implies otherwise; keep field conventions
  of the source note (the context includes its fields, so the agent can copy
  the house style, e.g. the `notes:` field with pinyin and the `pleco`
  source line in the screenshot);
- on reply, "Current proposal (version n) as JSON" is included; amend it.

### Config additions (`config.md`)

`queue_concurrency` (1), `queue_card_access` (`all`), `queue_notify` (true),
`queue_retention_days` (30), `queue_shortcut`, `queue_window_geometry`.

## Interactions with the existing editor pane

- The editor pane gets a **Send to queue** button next to Send, so a request
  made while editing can also be deferred. Same store, same worker.
- A queue thread's "Apply then edit" lands in the editor; the editor pane's
  chat does not inherit the thread (keep the two conversations separate in
  phase 1; "continue thread in editor pane" is a later feature).
- Long-term the editor pane's in-memory run becomes a thread with
  `source = editor` and `ephemeral = true`, so there is one runner and one
  transcript model. Not needed for the feature and not planned here.

## Risks and decisions

- **Collection access from the worker.** Only via `run_on_main`; the worker
  never touches `mw.col` on its thread. Same rule the editor pane follows.
- **Applying while reviewing.** Allowed; apply goes through `CollectionOp`,
  which triggers the normal `operation_did_execute` refresh, and the reviewer
  redraw handles the current-card case. Scheduling is unaffected by field
  edits.
- **Auto-apply.** Not in v1. A later `auto_apply` policy (e.g. tag-only
  changes) is a natural knob once the user trusts the proposals.
- **Provider sessions.** Neither client uses `codex exec resume` /
  `claude --resume`; history is re-sent as text, as today. This keeps the
  store the single source of truth and works across provider switches on
  reply. Cost: long threads re-send their transcript. Acceptable.
- **Anki quit mid-run.** The CLI subprocess dies with Anki; the thread is
  re-queued at startup. No partial writes: nothing is written to the
  collection until Apply.

## Phases

Each phase ends with `./check`, a PR, and a status update here. Red/green: the
store, state machine, `note_creates` validation, staleness, and prompt
builder are pure Python and get tests in `qt/tests/test_agent_queue.py`
before the code; the runner is tested with a fake agent.

**Phase 1 — capture, run, review (updates only).**
`queue_store`, `queue_runner`, `note_snapshot`, `reviewer_pane`,
`queue_window` with Apply / Reply / Discard / Open in Browser, resume at
startup. Shippable on its own: covers "fix this card" requests end to end.

**Phase 2 — new notes.** `note_creates` in the schema and validation,
card-render preview, duplicate warning, `assumptions`/`questions`. Covers the
disambiguation-card example.

**Phase 3 — robustness and polish.** Staleness + re-run, Retry/Change model,
Defer, retention sweep, bottom-bar badge, "Send to queue" in the editor pane,
"Apply then edit".

## Results / notes

(Update as phases land.)
