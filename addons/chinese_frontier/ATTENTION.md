# Cards that need the learner's attention

Status: implemented (branch `claude/frontier-attention`).

Automation can stop on a note without the learner noticing: a held note stays
suspended, a handed-over note waits for an edit, a fix on the learner's own card
stalls. Each language computes the list from the collection and its last snapshot;
nothing is stored except the handover marker below, so items clear by themselves
when the condition goes away.

## What counts

| kind | condition | clears when | action in the dialog |
|---|---|---|---|
| edit | `Source.handed_over` equals the note's current presentation fingerprint (set when feedback hands the note over) | the learner edits the sentence, or dismisses it | Dismiss |
| stuck | held, and the last snapshot has no repair for it (source card gone, moved, excluded) | a repair becomes possible, or the learner releases it | Release |
| rejected | held, and its key was deferred 3+ times in a row on the current vocabulary | a repair lands, or the learner releases it | Release |
| stalled fix | a fix on the learner's own card reports a problem | the problem goes away, or the learner cancels | Cancel fix |

Release unsuspends the cards the hold suspended and drops the hold; the repair tag
stays, so a later repair still happens. Until a snapshot has run, stuck holds are
not reported (nothing is known yet).

## Where it shows

- Main screen (deck browser): one line per language, only when the count is
  non-zero, with a link that opens the dialog.
- Tools menu: "<Language> Frontier: needs attention…".
- A tooltip once per session when items appear that were not listed before.
- The dialog lists word, reason and kind; "Open in Browser" searches the selected
  (or all) cards by `cid:`.
