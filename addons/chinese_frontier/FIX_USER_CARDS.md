# Fix this sentence on cards the add-on did not generate

Design only; no code yet. Based on `upstream/claude/frontier-claude-compat` at
bd407be8a and the live collection (via AnkiConnect, read only). Revision 2 after
review: suspend on request, always transplant scheduling, chosen deck only, keep
restored notes suspended and tagged, French in scope.

## What the collection looks like

| deck | live cards | note types |
|---|---|---|
| chinese | 4493 | Cloze-dfcd6 3566, Basic (HSK 5) 434, Chinese Frontier ~117 |
| french | 3427 | Cloze-dfcd6 3292, French Frontier 135 |
| HSK6 - * | 0 | Basic Quizlet (all suspended, converted by the add-on) |

The user's own Chinese cloze notes are mostly **one sentence with many cloze
ordinals**, often character-level:
`{{c5::弄}}{{c6::虚}} {{c2::作假}}, {{c3::骗取}}{{c4::荣誉}}`. Each ordinal `cN`
is one card of the note; the card being reviewed hides only its own ordinal. The
translation is a second `<div>` inside `Text`; pinyin and glosses are free text in
`Extra`. There are no `Target`, `Meaning` or `Source` fields. Any design that edits
`Text` touches every sibling card of that note.

## Recommendation in one paragraph

Treat a fix request on a user card the way the add-on already treats a suspended
Basic word: the user card is a *source*, the add-on writes a **new Chinese Frontier
note** linked to it, and the pipeline (writer, reviewer, validation, admission,
audits, later repairs) runs unchanged on that note. Three things make it a "fix"
rather than a conversion. The request records the learner's feedback up front, so
the first generated sentence must already satisfy it. The original card (only that
card, not its siblings) is **suspended at request time**, because it is not
testing the right thing, and stays tracked until a replacement lands or the learner
cancels. At admission the new card **inherits the original card's scheduling**
(interval, due, ease, reps, lapses, FSRS state), so the learner continues the same
word at the same interval and no new-card slot is spent. The user's note fields are
never written; the only writes to the user's note are the suspension and one
tracking tag. Request and swap are each one undo entry, and a reviewer-menu item on
the new note restores the original.

## Which cards get the item, and how the target is found

Show **Fix this sentence...** (cloze notes) or **Make a sentence for this word...**
(Basic notes) when all of these hold:

- Chinese Frontier has a deck configured and the card's home deck (`odid or did`)
  is that deck or one of its subdecks. Extra source decks and context vocabulary
  decks do not get the item. The new note is placed in the original card's home
  deck, so it stays in the same subdeck.
- The note is not Frontier-generated (those keep today's dialog) and is not tagged
  `chinese-frontier::exclude` or `leech`.
- A target word can be read from the reviewed card:
  - **Cloze note** (`model.type == 1`): take the fields the template clozes
    (`{{cloze:Field}}` in `qfmt`, as `snapshot` does) and collect the words hidden
    by the reviewed card's ordinal with `cloze_words(field, card.ord)`. If the
    ordinal hides a single character and the lexicon segmentation of the unfolded
    sentence has a longer token containing it, that token is the default. Several
    words on one ordinal become a choice. The dialog shows the default in an
    editable **Target** box, validated with `is_word` and "occurs in the sentence".
  - **Basic note** (`model.type == 0`): `_basic_words(fields, model, ord, ...)`
    already handles HSK markers and checks that this ordinal's template tests the
    front field, so a reverse card gets no item.
- The dialog also needs a **Meaning** line, because `Source.definition` feeds the
  writer and the key. Basic: `Back`. Cloze: the non-Chinese text of the clozed
  segment plus `Extra`, condensed (what `snapshot` already uses as `meaning` for
  cloze evidence), editable. Whatever the learner leaves there is stored verbatim
  and is the definition from then on.

Three real notes show why Target and Meaning must be editable:

| reviewed card | detected by `cloze_words` | lexicon default | learner may want |
|---|---|---|---|
| `{{c5::弄}}{{c6::虚}} {{c2::作假}}, {{c3::骗取}}{{c4::荣誉}}`, ordinal c5 | 弄 (a dictionary word on its own: "to do, to handle") | 弄虚作假 (CC-CEDICT headword containing the clozed character) | 弄虚作假 |
| `各种{{c1::迹}}象 都 表{{c5::明}} {{c6::形}}{{c7::势}} 将{{c2::好转}}`, ordinal c6 | 形 | 形势 | 形势 |
| `这本书…写得很 {{c1::生}}动。 … 我 {{c1::激}}动…`, ordinal c1 | 生 and 激 (two sentences share c1) | 生动 and 激动, offered as a choice | either, or the learner may decide the card tests both and cancel |

The sense is the other trap: the third note's `Extra` glosses 生动 as "vivid"
(adjective), but 生 alone would be glossed "life, raw". A sentence written for the
wrong word or sense passes every automatic check and still tests the wrong thing,
which is why the original stays suspended but intact and the restore item exists.

The dialog keeps the existing controls (reason checkboxes, words to avoid,
I answered, note) and adds Target and Meaning for non-generated cards. "I answered"
works as today: it becomes a reported confusion, and the first generated sentence
must be a contrast pair with that word.

## Options for what a fix does to the user's note

**A. Edit the user's note in place.** Rewrite `Text` with the new sentence clozed on
the same ordinal; put translation and pinyin somewhere.
Preserves card id, scheduling and revlog perfectly for single-ordinal notes.
Rejected: most live cloze cards share one sentence across five or more ordinals,
so rewriting the sentence rewrites every sibling card; the add-on would have to
guess where translation and pinyin live in a free-form layout; the add-on's own
guarantees (`Translation` on the front, `Disambiguation` hidden, audit
fingerprints) depend on the Frontier template; and every later repair would
re-enter user content. The audit loop and repair tags would also need a second
code path keyed on something other than `Source`.

**B. Convert the note to a Chinese Frontier note** with `change_notetype`.
Keeps the note id, but a cloze→cloze notetype change keeps cards by ordinal, so
the other ordinals' cards are deleted; it is a schema change (full sync on the
next sync); and the original content is gone unless copied aside. Rejected.

**C. New linked Frontier note, original card suspended, scheduling inherited**
(the existing source→replacement relation plus a scheduling transplant).
Recommended and chosen.

| | A in place | B convert | C link + swap |
|---|---|---|---|
| user fields written | yes | yes (replaced) | no |
| sibling cards | rewritten | deleted | untouched |
| card id kept | yes | yes | no (new card; original keeps id + revlog) |
| scheduling kept | yes | yes | copied (ivl, due, ease, reps, lapses, FSRS state) |
| revlog kept on the studied card | yes | yes | no; stays on the suspended original |
| new-card slot | none | none | none |
| sync | normal | full sync | normal |
| undo | note edit | schema | grouped entries + restore item |
| pipeline changes | large | medium | small (snapshot + admit + dialog) |

The one real loss in C is that the new card's review graph starts empty. The
original card keeps the history and is one click away (Browse, `tag:
chinese-frontier::source::<cid>` on the new note). The snapshot keeps counting
that history as evidence (below), so vocabulary knowledge is not lost either.

## Data model

### While a fix is pending

Feedback has to live somewhere before a `Source` field exists. Adding a field to
the user's note types is a schema change (full sync) and writes 10k notes; tags
cannot hold reasons, words and free text. So pending requests live in the
collection config, which syncs and is undoable:

```json
"chineseFrontierFixes": {
  "<original card id>": {
    "note_id": 1234, "ordinal": 2, "deck_id": 5678,
    "word": "弄虚作假", "definition": "seek honour through fraud and deception",
    "key": "<sha256 of word + definition, same as Source.key>",
    "sentence": "弄虚作假, 骗取荣誉",
    "fingerprint": "<fingerprint(fields, []) of the original note at request time>",
    "requested_at": 1761820000,
    "feedback": [ { "reasons": ["unnatural"], "note": "", "avoid_words": [],
                    "answered": "", "sentence": "弄虚作假, 骗取荣誉", "rejected": true } ],
    "disambiguation": { "with": "作假" }
  }
}
```

`feedback` entries and `disambiguation` use exactly the shapes `record_feedback`
and `_record_confusion` write into `Source` today, so `learner_feedback()` and
`reported_confusions()` read a record the same way they read a `Source` (small
refactor: let them take the parsed dict). Requesting again on the same card
appends to the same record. The fingerprint covers fields only, because the
request adds a tag.

Tracking the suspended card: the record is the source of truth, and the original
note gets the tag `chinese-frontier::fix-pending` so the cards waiting for a fix
are one search away in the browser (`tag:chinese-frontier::fix-pending
is:suspended`). The settings dialog lists pending fixes with the target, how long
they have waited, the last deferral reason, and a **Cancel** button that
unsuspends the card, removes the tag and deletes the record. Nothing unsuspends
automatically: a card suspended for a fix is never silently put back in the queue.
The tag is removed at swap or cancel; after the swap the link lives on the
Frontier note (`chinese-frontier::source::<cid>`).

Evidence: a suspended card normally stops counting toward `known`. A card
suspended for a pending fix is still a card the learner was answering correctly,
so the snapshot keeps counting its review log while the record exists, and after
the swap it counts it for the adopted note (below). Otherwise requesting a fix
would make the word unfamiliar for other sentences for weeks.

### After the swap: an adopted Frontier note

The new note is an ordinary generated note plus one block in `Source`:

```json
"adopted": { "card_id": ..., "note_id": ..., "ordinal": 2,
             "notetype": "Cloze-dfcd6", "sentence": "..." }
```

`feedback` and `disambiguation` move from the record into `Source`; the record and
the pending tag are removed in the same transaction. Tags on the new note:
`chinese-frontier::generated`, `chinese-frontier::key::<key>`,
`chinese-frontier::source::<cid>`, `chinese-frontier::adopted`, and
`chinese-frontier::feedback::<reason>`. From here on `record_feedback`, repairs,
`MAX_REPAIRS` → `chinese-frontier::manual`, audits and undo all work unchanged,
because the note is generated.

### After a restore

**Restore the original card** unsuspends the original card and copies the
scheduling back from the adopted card (so reviews done on the adopted card are
not lost). The adopted note is kept, suspended, tagged
`chinese-frontier::restored`, with `"restored_at"` added to `Source.adopted`; its
history and the learner's feedback remain readable. The snapshot treats a restored
note as excluded: it is not audited, not repaired, and does not count as covering
its word. Restoring again raises.

### Identity

`key = sha256(word + definition)` as today, computed once in the dialog and
stored. It can differ from the key of a Frontier note the add-on already made for
the same word from an HSK Basic card (different definition text). That is
allowed: an explicit fix is not deduplicated against `covered_words` or `known`,
exactly as requested repairs are not. The settings dialog shows the collision.

## Pipeline changes

`core.py` and `generation.py` need no changes: a fix is a `Source` with
`force_repair=True`, `avoid_sentence`, `rejected_sentences`, `learner_feedback`,
`avoid_words`, `reported_confusions` and `existing_cards=1`, which the writer and
reviewer prompts already consume and `validate_candidate` already enforces.

**`collection.py`**

- `request_fix(col, card_id, *, word, definition, reasons, note, avoid_words,
  answered)`: validates like `record_feedback` (reasons known, avoid words are
  Chinese words other than the target, answered word is another word), checks the
  card is in the chosen deck tree and not generated, then in one undo entry:
  `suspend_cards([card_id])`, add the pending tag to the note, write the record
  with `set_config(..., undoable=True)`. Returns the key so the runtime calls
  `state.retry_now(key)` and `tick(force=True)`.
- `cancel_fix(col, card_id)`: the inverse, one undo entry.
- `snapshot`: read the records. Drop a record whose original card is gone or whose
  note fingerprint changed (the learner edited it themselves) and count it under
  `skipped`; the card stays suspended and tagged until the learner cancels, and the
  settings list says why it stalled. For the rest, synthesize
  `Source(card_id, note_id, deck_id=home, word, definition, fingerprint=record
  fingerprint, avoid_sentence, existing_cards=1, reported_confusions,
  force_repair=True, learner_feedback, avoid_words, rejected_sentences)` and put
  its key in a new `Snapshot.fixes` set. Sorting and the "already represented" skip
  treat `fixes` like `repairs`; `scanned` bypasses the slot check for them as it
  does for repairs. The original card's review log counts toward `known` while the
  record exists.
- `snapshot`: for generated notes whose `Source` has `adopted`, synthesize the
  potential `Source` from the metadata instead of looking for a Basic note, so
  repair tags and audits keep finding a source after the swap. Use the stored
  fingerprint. Union the original card's revlog into the adopted card's evidence.
  Notes tagged `chinese-frontier::restored` are skipped like `MANUAL_TAG` notes,
  minus the coverage.
- `_admit`: for a source whose key is in `fixes` (no `repair_id`): after
  `add_note` and the existing verification, copy `type, queue, due, ivl, factor,
  reps, lapses, left, flags, memory_state, desired_retention` from the original
  card (a suspended card keeps `type`; `queue` is taken from `type`, so a review
  card comes back as a review card, a learning card as learning) to the first new
  card with `update_card`, re-read and verify; write `adopted`, `feedback` and
  `disambiguation` into `Source` (pass the record as the initial metadata to
  `_write_candidate_fields`); remove the pending tag from the original note;
  delete the record with `set_config(undoable=True)`. All inside the existing
  `transact` and merged into the existing undo entry, so Edit → Undo reverts the
  note, the scheduling copy, the tag and the record together (the original stays
  suspended, as it was before admission).
  `existing_cards=1` makes `extra_cards = card_count - 1`: a word or contrast
  sentence needs no slot; character fallback siblings beyond the first are
  budgeted and staged exactly as repairs are today.
- `restore_original(col, note_id)` as described under "After a restore".
- Stale records (original edited or deleted) are never deleted automatically,
  only reported; `cancel_fix` is the learner's way out.

**`runtime.py`**

- `reviewer_menu`: generated notes keep today's path, plus **Restore the original
  card** on adopted notes; other notes in the chosen deck tree get the fix item
  when target detection finds something. Detection needs the lexicon for the
  single-character case; load it in a `QueryOp.without_collection()` like
  `scanned` does, and fall back to the raw ordinal word if the dictionary is not
  cached yet.
- `feedback_dialog(note_id)` becomes `feedback_dialog(card_id)`, with the Target
  and Meaning rows shown only for non-generated cards, and a line saying the card
  will be suspended now and replaced when a sentence passes review. On OK it calls
  `request_fix` instead of `record_feedback`, then `retry_now(key)` and
  `tick(force=True)` as now.
- Settings dialog status: pending fixes with age, deferral reason and Cancel;
  collisions with an existing generated note for the same word.

**Audit loop, budget, backoff**

- Audits: an adopted note is `in_audit_scope` like any generated note. The
  original card is suspended, so it stops counting in `covered_words`; the
  adopted note takes over.
- Budget: nothing enters the new queue, so a fix bypasses the slot check the way
  repairs do. `_read_budgets` is unchanged. Character fallback siblings are the
  exception and are budgeted as for repairs.
- Backoff: `state.retry_now(key)` on request lifts the deferral and drops drafts
  written without the feedback; `_admit` already rejects a draft whose
  `learner_feedback` differs from the current source as `STALE_SOURCE`.
  A fix that never passes review stays pending, retried on vocabulary growth and
  backoff like any deferred target, visible in the settings list and cancellable.
  `MAX_REPAIRS` applies after the swap; before it, there is nothing to hand over.

## Risks to user-authored content

- **Writes to the user's note are limited to** suspending one card, adding and
  later removing one tag, each inside an undo entry. Fields are never written.
  Siblings stay live.
- **A suspended card that never gets fixed**: the learner loses reviews of that
  word until they cancel or a sentence passes. Mitigations: the pending list with
  age and reason in the settings dialog, the browser tag, and the fact that
  requested fixes are generated ahead of ordinary targets with backoff lifted.
  No automatic unsuspension, so the learner is never surprised by the old card.
- **Wrong target or sense**: see the table above. Editable Target and Meaning,
  the lexicon default, and restore.
- **Learner edits the original after requesting**: the fingerprint no longer
  matches; the fix stalls visibly rather than fixing a sentence they already
  fixed. Cancel restores the card.
- **Siblings keep the complained-about sentence**: with "unnatural" on a
  five-ordinal note, four cards still show it. Out of scope; the learner can
  request fixes on those cards too. Worth a line in the dialog.
- **Scheduling copy**: `due` for a learning card is a timestamp and for a review
  card a day number; copying every field verbatim keeps the pair consistent. A
  card suspended for weeks comes back overdue, as it would after unsuspending.
  The FSRS memory state is per card and copies cleanly. The revlog is not moved:
  the add-on never edits original cards' history, and raw revlog edits are not
  undoable.
- **Deleting the adopted note by hand** leaves the original suspended. The
  settings dialog lists `chinese-frontier::source::<cid>` links whose Frontier
  note is missing and offers to unsuspend.
- **Duplicate coverage** of a word that already has a generated card: allowed on
  purpose; both are audited independently; shown in settings.
- **Sync and other clients**: config and notes sync normally; no schema change. An
  older add-on version ignores the config key and the `adopted` block; its
  snapshot finds no Basic source for the adopted key, so it never repairs that
  note. Rolling back is safe; a pending card stays suspended and tagged, so it can
  be found and unsuspended by hand.

## French

Both French pieces are in scope, after the Chinese work.

### French Frontier generated notes (135 live)

Done: `french.collection.record_feedback` reuses the Chinese `_learner_feedback`
reader and `FeedbackResult`; the learner fields live on `french.core.Target`, outside
its key and its stored metadata.

They have a `Source` field and a repair tag but only the old "Replace this French
sentence automatically" item. This is a port: move the feedback entry shape,
`learner_feedback()` and `record_feedback()` into a shared module used by both
`collection.py` files; store entries in the French `Source`; add
`learner_feedback`, `avoid_words` and `rejected_sentences` to `french.core.Target`
or alongside it in the draft; enforce avoided words and rejected sentences in
`french.core.validate`; pass them through the French writer and reviewer prompts in
`french/generation.py`; same `french-frontier::feedback::<reason>` tags and a
`french-frontier::manual` handover after three repairs. The reason list is shared;
"Gives the answer away" matters more in French (cues, infinitive hints) and stays.
French has a contrast mode but no reported-confusion concept and no rule that a
contrast only answers a reported confusion, so "I answered" is hidden for French
in this phase. Adding it means a `reported_confusions` tuple on the French target
and the same validation rule as Chinese, in a later PR.

### French user cloze notes (3292 live)

Same fix record, swap, restore and tracking as Chinese, under a
`frenchFrontierFixes` config key and `french-frontier::fix-pending` tag, placed in
the French chosen deck tree. The difference is the target. A French `Target` is
`(lemma, pos, sense, kind, definition, level, frequency, form_key)`, and its key
hashes `lemma, pos, sense, kind, form_key`, so all of these must be fixed at
request time:

- surface: the text hidden by the reviewed ordinal (hint stripped), as the French
  snapshot already reads it;
- lemma and pos: `contextual_analyses(value, match, lexicon)` returns the `Form`
  keys the surface can have in that sentence (the snapshot already uses this for
  evidence). One reading → default; several → a choice in the dialog
  (`travailler VER` vs `travail NOM` style); none → the item is hidden;
- kind: `conjugation` with `form_key` set when the chosen reading is a finite verb
  form the French target logic treats as a conjugation target, else `vocabulary`;
- sense and definition: `lexicon.definitions(entry, folder)` fetches the cached
  DBnary/Wiktionary senses for the lemma and pos; the dialog shows them as a
  choice with the first selected, plus an editable definition line; the chosen
  sense URI becomes `Target.sense`. Fetching happens off the main thread before
  the dialog opens, with a plain "no senses cached" fallback that still allows
  the request with an empty sense and the learner's definition line;
- level and frequency: from the FLELex entry when present, else 0; they only rank
  targets and a fix is ranked first anyway.

The French `_admit` then adds the note with `repair_note=None` and the same
transplant and verification as Chinese, and the French snapshot synthesizes the
`Target` from `Source.adopted` for later repairs and reads the original card's
revlog as evidence for `known`.

Order of PRs: Chinese user cards; French generated-note feedback; French user
cards.

## Migration and compatibility

- No note type or template change; `ensure_model` is untouched in both languages.
- New collection config keys `chineseFrontierFixes` and `frenchFrontierFixes`;
  absent means no pending fixes.
- New optional `Source.adopted` block and the tags `*::adopted`, `*::fix-pending`,
  `*::restored`; all readers treat their absence as today's behaviour.
- `learner_feedback`/`reported_confusions` gain a dict-accepting form; the
  field-dict form stays.
- Existing tests are unaffected; `Snapshot` gains a `fixes` field with a default.

## Test plan

Chinese tests in `qt/tests/test_chinese_frontier.py`, French in
`qt/tests/test_french_frontier.py`, run with
`M=/Users/sark/code/anki; PYTHONPATH=$M/out/pylib:$M/pylib:$M/out/qt:$M/qt $M/out/pyenv/bin/pytest qt/tests/test_*frontier*.py`.
A new helper `add_cloze(col, text, extra, deck, established)` builds a user cloze
note with several ordinals, next to the existing `add_word` and `setup_sentence`.

Target detection (pure functions, no collection):
- single-ordinal word cloze → that word; character ordinal inside a lexicon word →
  the whole word (the 弄 → 弄虚作假 case); two words on one ordinal → both offered;
  non-Chinese cloze → none.
- Basic front word with HSK marker → word; reverse-card ordinal → none.
- meaning default: Basic `Back`; cloze translation div plus `Extra`, condensed.

Request and cancel:
- `request_fix` suspends exactly the reviewed card (siblings stay live), adds the
  pending tag, writes the record with key, fingerprint, feedback entry and
  confusion; fields unchanged; `col.undo()` reverts all three.
- a second request on the same card appends feedback; invalid reasons, avoid words
  equal to the target, generated notes, and cards outside the chosen deck tree
  raise.
- `cancel_fix` unsuspends, untags, deletes the record, in one undo entry.

Snapshot:
- a record becomes a `Source` with `force_repair`, `existing_cards=1`,
  `avoid_sentence`, `rejected_sentences`, `learner_feedback`, `avoid_words`,
  `reported_confusions`, sorted ahead of ordinary sources, not skipped although
  the word is in `known` and `covered_words`.
- the suspended original still counts toward `known` while the record exists.
- a record whose original was edited or deleted yields no source and is counted
  in `skipped`; the card stays suspended; nothing is deleted.

Admission:
- with zero slots the draft is still admitted; the new note carries
  `Source.adopted`, `feedback`, key, source and adopted tags, sits in the original
  card's deck; the new card's `type, queue, due, ivl, factor, reps, lapses, left,
  memory_state` equal the original's pre-suspension state; the pending tag and
  record are gone; `col.undo()` reverts all of it in one step and the original
  stays suspended.
- character fallback: first card transplanted, extra siblings staged against the
  budget as repairs are.
- a draft generated before feedback was added is rejected as `STALE_SOURCE`.
- "I answered X" forces contrast mode with X on the first generation (extend
  `test_reported_confusion_is_repaired_only_as_a_contrast_with_that_word`).
- `test_learner_feedback_reaches_writer_and_reviewer` extended to a fix source.

After the swap:
- `record_feedback` on the adopted note queues a repair; `snapshot` synthesizes
  the source from `Source.adopted` with no Basic note present; the repair keeps
  card id and scheduling (existing verification).
- the word stays in `known` through the original card's revlog.
- carried-over feedback entries count toward `MAX_REPAIRS`.
- `restore_original` unsuspends the original, copies scheduling back, suspends
  and tags the adopted note, in one undo entry; the restored note is not audited,
  repaired or counted as coverage; a second call raises.

Runtime (monkeypatched `mw` as in the existing runtime tests):
- the fix item appears for a user cloze card and a Basic card in the chosen deck
  tree, not for a generated note (which keeps its own items), a card in an extra
  source or vocabulary deck, or a card with no detectable target; the restore item
  appears only on adopted notes; the dialog's OK path calls `request_fix`,
  `retry_now` with the record's key, and `tick(force=True)`.

French:
- generated-note feedback: record, tags, writer and reviewer prompt contents,
  avoided word and rejected sentence enforcement in `validate`, stale draft
  rejection, manual handover; mirrors of the Chinese tests.
- user cloze target: one reading → default; several → choice; finite verb →
  conjugation with `form_key`; cached senses → choice with the first selected;
  no senses → request allowed with empty sense; request, swap, restore and
  evidence tests mirror the Chinese ones with `add_french_cloze`.

Manual check on the deployed add-on: request a fix on a multi-ordinal cloze card
with Ollama as provider, confirm the card disappears from the queue while its
siblings keep reviewing, the tag and settings entry appear, the replacement appears
after admission with the same interval, Restore puts the original back, and
Edit → Undo reverts each step.

## Decisions taken

1. Scheduling is always transplanted; no checkbox.
2. Restore keeps the adopted note suspended and tagged `chinese-frontier::restored`.
3. Chosen deck tree only, for the menu item and for where the new note goes (the
   original card's own subdeck).
4. French is in scope: generated-note feedback first, then user cloze cards.
5. The original card is suspended at request time and tracked by the config
   record, the `chinese-frontier::fix-pending` tag on the note (decided: tag it)
   and the settings list; never unsuspended automatically.

No open decisions. Ready to implement, starting with the Chinese user-card PR
against `claude/frontier-claude-compat`.
