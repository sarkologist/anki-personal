# Agent pane: context tray

Replaces the single auto-attached editor selection and the transcript's
"Quote into the message" with a tray of attachments above the prompt.

## Decisions (user, 2026-11-21)

- Editor selections get in only by an explicit **Add**. The live selection is
  previewed under the tray with an Add button; nothing goes along by surprise.
- The transcript's Quote button becomes **Add to context**; it no longer
  inserts `>` text into the message.
- Sending empties the tray. A note change drops editor items (they describe
  the old note); transcript items stay until sent or removed.

## Model

`attachments.py`: `Attachment(source, text, comment, field_name, field_index,
role)`, `source` is `"field"` or `"transcript"`; `role` (transcript) is
`"assistant"`, `"user"` or None when a selection spans messages. Dicts in the
store, validated on read.

- Store: schema v4 adds `messages.attachments_json`. `create_thread(...,
  first_attachments=...)`, `reply(..., attachments=...)`. `Message.attachments`.
- Model input: `attachments_prompt(text, attachments)` puts a numbered block
  before the user's text, the same for every provider and for history replay
  (`replay_history`, `pending_request` use it). Field items name the field.
- A message may be sent with attachments and no text.
- Old threads keep `snapshot.selected_text` ("Selection sent from ...");
  new threads no longer set it.

## UI

- `ContextTray` (thread_panel.py), shared by the editor pane and the queue
  window: a row per item (source + excerpt, a comment line edit, remove).
  Only widgets the shell's plugin host emulates.
- Editor pane: the live selection preview gets an **Add** button.
- Transcript (`surface.py`): the button posts `agentAddContext:{text, role}`.
- Transcript rendering: a user message shows its attachments (label, quote,
  comment) above its text.

## Checks

- Unit: attachment validation and prompt block; store v3 -> v4 migration and
  round trip; replay includes attachments; tray add/dedupe/remove/clear rules;
  send with attachments for new threads and replies; restore on failure;
  rendering escapes.
- Running: dev Anki (throwaway profile) and the shell harness
  (`shell/harness/transcript-quote.js` becomes the tray check).

## Status

- [x] model + store + runner
- [x] tray UI, editor Add, transcript Add (pane and queue window)
- [x] rendering
- [x] tests (add-on 564, plugin host 59); ruff/dprint on changed files
- [x] dev Anki (send stores both attachments with the comment; transcript
      shows them) and shell (`harness/context-tray.js` passes)
- [ ] review, PR, deploy

Not done: a keyboard shortcut for Add; attachments can't be edited after
sending (re-add them to a reply).
