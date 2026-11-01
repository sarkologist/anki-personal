# Adaptive feedback categories

Design record, implemented in `addons/chinese_frontier/categories.py`.

## The idea

The "Fix this sentence..." dialog has five fixed reasons (`core.FEEDBACK_REASONS`)
plus a freeform "Anything else" box. Here a **comment** is the text typed in that
box, and a **note** is always an Anki note. A background job classifies the
comments. A category reported on enough distinct notes becomes a structured field
in the dialog: a checkbox, or an input when the category carries a value. The same counts
flag a category that keeps coming back as a candidate for a generation-rule change,
because one complaint repeated across many cards is a rule problem, not a card
problem.

The live collection today has 4 comments across 14 notes with feedback, e.g.
"the chinese alone should disambiguate it" (already turned into a global rule) and
"i'm confusing it with était" (duplicates "I answered"). So the classifier must
map onto the built-in reasons and fields too, and most comments should land in an
existing category.

## Registry

One collection-config key, `frontierFeedbackCategories`, shared by Chinese and
French so it syncs and one complaint type is not split by language:

```json
{
  "threshold": {"notes": 3, "days": 30},
  "version": 4,
  "categories": {
    "only_translation": {
      "label": "Only the translation disambiguates",
      "description": "The Chinese/French context alone admits another answer.",
      "input": "flag",
      "created": 1793500000, "promoted": 1793600000
    }
  },
  "addressed": {"ambiguous": 1793700000}
}
```

- The five `FEEDBACK_REASONS` and the structured fields "Words to avoid" and "I
  answered" are implicit built-in categories, always shown and never stored. A
  stored key never collides with them.
- `input` is `flag` (a checkbox), `words` (one or more words in the card's
  language, e.g. "the hint uses [words] I don't know") or `text` (a short phrase,
  e.g. "wrong register: [too formal]"), plus an `input_label` for the latter two.
  The classifier proposes the input kind with the category. Once created, the kind
  is fixed like the key.
- `version` goes up by one each time a category is added.
- `promoted` is 0 until the threshold is reached. Once promoted, a category
  stays promoted, so the dialog does not shuffle. There is no automatic demotion.
- `addressed` maps any category, built-in or learnt, to when the learner marked
  its rule change as done (see below).

## Taxonomy stability

Labels drift if the model re-labels everything on each run. So:

- **Assign once.** Each classified feedback entry gets
  `"categories": {key: value}` (value `true` for a flag, else the words or phrase
  the classifier pulled out of the comment) and `"classified": <registry version>`
  written next to it.
  Classified entries are never re-sent, with one exception: an entry with no
  category is offered again once after the version goes up, and then it may only
  gain the newer keys.
- **Existing first.** The prompt lists every category (built-in, promoted and
  candidate) with key, label, description, input kind and up to 3 example comments. The schema
  makes the model choose `existing: [keys]` per note, and allows `proposed:
  {key, label, description, input, input_label, closest_existing, why_not}` only
  when nothing fits. Several comments in one batch that propose the same new category must share one key.
- **Keys are immutable.** The model never renames or merges. Proposed keys are
  slugged (`[a-z_]{3,32}`), and a slug that matches an existing key or label is
  folded into that key. Unknown keys in the response are dropped.
- A comment that restates a built-in field ("i'm confusing it with était") maps
  to that field with its value (`answered: "était"`). It never creates a category.
  The classifier does not fill the field in after the fact: the repair has already
  run with the comment in its prompt. The value counts for that field's statistics.

## Timing

Classification runs in the background off the existing refill `tick`, through the
add-on's own `CliGenerator` (same provider/model/timeout settings and telemetry
`phase: "classify"`):

- It runs only when the runtime is idle (`busy` false), so it never competes with
  generation for the CLI or blocks the dialog. One call handles up to 20 entries.
- It runs from the periodic (unforced) tick, at most once per 10 minutes, when
  there are unclassified or undated entries. A forced tick after a feedback
  submit goes straight to the repair, so classification waits for the next
  periodic tick.
- On failure the entries stay unclassified and are retried on a later tick. A
  failure does not touch generation's `retry_after`.
- A module-level flag lets only one runtime (Chinese or French) classify at a time,
  and that run covers both languages.

Submitting feedback never waits for the classifier. The repair prompt already gets
the note verbatim, so classification adds nothing to generation, and the
generation prompts are untouched (no conflict with the context-disambiguation
change).

## Where the entries are

The classifier reads and writes feedback entries in all four stores:

| store | entries |
|---|---|
| generated Chinese/French notes | `Source.feedback` |
| pending fixes | `chineseFrontierFixes` / `frenchFrontierFixes` records' `feedback` |
| adopted notes | `Source.feedback` (copied from the fix record at swap) |

New entries get `"at": <epoch>`. Older entries have no timestamp. They get the
time they are first classified, so the existing notes count as recent once.

Writing `categories` back into an entry also adds the
`<lang>-frontier::feedback::<key>` tag to generated and adopted notes. The
learner's own notes are never written, as before. A pending fix's categories reach
its adopted note's tags at swap time, the same way its reasons do now.

## Counting and promotion

A category's count is the number of **distinct Anki notes** (generated, adopted,
or the learner's note behind a pending fix) with a feedback entry in the last `days` days whose `reasons` (ticked) or `categories`
(classified) include the key. Several comments on one note count once. Ticks of a promoted category keep its count alive
for the rule-change signal. A candidate whose count reaches `notes` is promoted in
the same op that writes the classifications.

## Dialog and reasons

- Both dialogs show the built-in fields, then promoted categories in promotion
  order: a checkbox for `flag`, a labelled line edit for `words` or `text`. They
  read the registry when the dialog opens.
- `record_feedback` / `request_fix` (Chinese and French) accept built-in or
  promoted keys, plus `values: {key: str}` for input categories. `words` values
  are validated like "Words to avoid" (words in the card's language, not the
  answer). Anything else is still an error.
- A filled promoted field is written into the entry with its text,
  `"reason_text": {key: "label: description[: value]"}`. `_learner_feedback` then
  resolves the label without reading the registry. This keeps the generation path
  pure and keeps old entries readable if a category is ever removed.
- The tag is `<lang>-frontier::feedback::<key>`, the same as for built-ins.
- A promoted category only adds text to the writer/reviewer prompt. It does not
  get machine behaviour like "I answered" (rule the word out) or "Words to avoid"
  (validated absent). If a category needs enforcement, that is code work, and the
  rule-change signal below is what prompts it.

## Rule-change signal

A category whose count reaches 2 × `notes` since its `addressed` time (or ever) is a
candidate rule change:

- **Diagnostics → Overview**: a "Recurring feedback" section with every category's
  count, example comments, and a marker on the candidates.
- **Settings status line** (Chinese and French): "Recurring feedback: Only the
  translation disambiguates (6 notes in 30 days). Consider a generation rule."
- Settings lists the categories, with a **Mark addressed** button that sets
  `addressed` to now. Counts for the signal then restart; promotion is unaffected.

## Not in scope

- Renaming or merging categories from the UI. Merging can be added later with an
  `alias` field, without changing stored keys.
- Feeding categories into the writer/reviewer prompts beyond the existing
  `Problems:` line.
- Changing generation rules automatically.

## Tests (red/green)

- registry: slug folding, built-in collision, fixed input kind, version bump,
  promotion at threshold, distinct-notes counting, window, `addressed` reset
- classifier round trip with a fake client: existing-first mapping, unknown keys
  dropped, assign-once, the one-time re-offer after a version bump, entries in all
  four stores updated and tagged, learner notes untouched
- classifier maps a restated confusion to `answered` with its word
- `record_feedback` / `request_fix` accept promoted keys and values, validate
  `words`, reject candidates; `_learner_feedback` uses `reason_text`
- status/diagnostics text lists candidates
