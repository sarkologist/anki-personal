# Chinese Frontier

An Anki desktop add-on that gradually replaces suspended Chinese word notes with
one-word sentence clozes. It runs automatically while desktop Anki is open. Original
notes stay suspended and intact; replacements are linked through their `Source`
field and `chinese-frontier::source::*` tags. Newly introduced sentence cards have
their own scheduling history, including when the original was a suspended review.

## Installation

Run `python tools/package_chinese_frontier.py` from this repository, then install
`out/addons/chinese_frontier.ankiaddon` through Anki's Add-ons dialog and restart.
Choose **Tools → Chinese Frontier**, select the source deck, and Save. There are no
daily approvals, tagging steps, or manual unsuspensions. That dialog also pauses
conversion, switches providers, and displays progress and deferred targets.

For an automatic, profile-specific installation:

```sh
python tools/package_chinese_frontier.py \
  --install-to "/path/to/Anki2/addons21" \
  --deck chinese \
  --source-deck "HSK6 - 1000" \
  --collection "/path/to/Anki2/User 1/collection.anki2"
```

The installer preserves existing configuration and `user_files`. Initial deck
configuration applies only to that exact collection and never overrides saved
settings, including a paused converter. Desktop Anki must be open to generate and
admit cards. Once synced, sentence cards work on ordinary Anki clients.

## Subscription generation

Codex is the default. Run `codex login` and choose ChatGPT sign-in. Generation uses
`codex exec --output-schema`, not the OpenAI API. Claude uses `claude -p
--json-schema`; sign in to the Claude Code CLI first. Both consume your existing
subscription allowances. There is no automatic fallback to a paid API or to the
other provider. API-key environment variables are removed from child processes.

The CLI receives target definitions and a relevant selection of established Chinese
words, their meanings, and actual reviewed usage. Generation and an independent language-review pass happen in
temporary directories with no collection access. Claude tools and MCP servers are
disabled. Codex ignores user configuration, disables shell, apps, multi-agent and
web-search features, and uses a read-only sandbox. Optional model settings select
the CLI model; a blank model uses the CLI's built-in default, not custom user config.
Closing the profile or changing settings cancels generation. Account, timeout, and
quota errors retain prepared drafts and back off rather than block studying.

## Vocabulary and validation

By default, a context word needs a live card and Good/Easy on two separate Anki
study days since its last Again. Buried cards are still eligible; unseen and
suspended cards are not. Only the explicitly tested Front or live cloze ordinal
provides evidence. Context text, other cloze siblings, and Extra do not establish
words. Existing cloze text and Extra can provide meaning metadata only.

Basic note sources need an all-Chinese single word in Front, a nonempty Back, and
all sibling cards suspended. Leech and `chinese-frontier::exclude` notes are skipped.
Other field names are configurable. The chosen deck includes subdecks. The settings dialog also selects extra source
decks and extra context-only decks. Suspended targets from extra sources receive
replacements in the chosen Chinese deck, sharing its limits; originals remain in
their source decks. Extra sources supply targets only, even if they contain live
cards. Context evidence comes from the chosen Chinese deck and explicitly selected
context-only decks. Generated replacements can establish context once learned.
Whole words already established by live cards are skipped as new targets.

Quizlet-style HSK fronts such as `精心 6` are parsed as the word plus level metadata,
only in HSK-labelled decks or note types. Lower HSK levels are tried first. A level
in the note-type name is used as a coarse fallback (an HSK5 import includes lower
levels too); unknown levels follow recognized levels. Ties retain old due/card
order, and requested sentence repairs take priority. This is a curriculum proxy,
not an individual difficulty estimate. Every proposed sentence still passes the
same familiar-context checks; word length is not treated as difficulty.

On first use, the add-on downloads and caches CC-CEDICT headwords from
[MDBG](https://www.mdbg.net/chinese/dictionary?page=cc-cedict). CC-CEDICT is licensed
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The cached
headword list is derived from CC-CEDICT and carries that license; code is AGPLv3.
No dictionary data is bundled. A download failure defers generation; it never
silently replaces word segmentation with character matching.

Dictionary maximum matching is deliberately conservative: knowing 学 and 生 does
not establish 学生. Ambiguous segmentation can reject a valid sentence. The
generator tries alternatives instead of relaxing vocabulary checks. Both vocabulary
and source contents are rechecked before admission. Context senses, naturalness,
grammar, translation, and pinyin receive a separate model review, but remain model
judgments rather than guarantees. Connector examples must make their logical
relationship clear from the surrounding clauses, rather than relying only on an
English hint. Examples are ordered by useful context, not shortest length, and can
include a preceding clause or sentence. Review tries plausible competing words and
rejects distinctions that require prior idiom knowledge or an unstated intention.
Genuine synonym overlap is not a learner error. Prefer simple everyday constructions. Unknown
targets are deferred with backoff and retried when vocabulary changes.

## Daily limits and recovery

Generation independently checks plausible alternative answers for every blank.
Ambiguous examples are rewritten with fuller context before using character clozes
as a fallback. Character fallback assigns a separate ordinal to each distinct
character; repeated characters share an ordinal to prevent copying.
Single-character and repeated-only words cannot use that fallback.

Contrast pairs are only made for a confusion you report, never to rescue an
ambiguous example: a confusion is a fact about your recall, seen only in review.
Right-click a generated card during review and choose **I answered a different
word...**. The word is recorded in the note's `Source` field, and the note is
regenerated in place as a pair contrasting the two words. Both whole words are
hidden with `c1`, producing one card that keeps its history. If no clear pair passes
review, the existing card is kept and the attempt is logged. Older contrast pairs
made without a reported confusion are reduced to their first example once and
re-audited.

Every disambiguation card shows **Disambiguation** on both review sides. Ordinary
multiple-cloze cards are not labeled automatically. The editor-only `Disambiguation`
field logs the provider, candidate examples, checked alternatives, whether each fits
the situation and visible characters, reasons, and approval/defer status. The model's
approval is rejected when its own report says an alternative fits. These checks are
model judgments, not proof that every possible alternative was considered.
The `Source`, `Dependencies` and `Disambiguation` metadata fields are collapsed
in the editor and excluded from unqualified searches.

Existing generated cards are audited automatically after this upgrade or a visible
content edit. Approved examples keep their content; ambiguous examples are repaired
in place. Failed audits are recorded without silently changing the card or claiming
it is unambiguous. User edits made during generation are never overwritten.

Each extra character sibling counts against all relevant new-card limits. Siblings
can be introduced across days, including in a deck limited to one new card per day.
An existing word card can receive a character cue immediately while additional
siblings wait for capacity. The whole word becomes available as familiar context
only after **all** its character siblings meet the learning threshold; individual
characters do not become familiar words through this fallback.
The full English translation appears alongside the Chinese on the front, without
definition hints in the blank. Pronunciation and word explanations remain on the
answer side. Existing generated hints and the original front template are upgraded
on profile opening without changing card IDs or scheduling; customized templates
are preserved. Pending new
cards (including buried siblings), today's introductions, and due reviews reserve
admission capacity. The add-on reads preset, deck, and today-only limits, and
honors the review-limit switch and Anki's study-day boundary. All ancestor budgets
are reserved conservatively, even when Anki permits studying a child independently.
Filtered cards and sibling burying may therefore cause under-admission. Native Anki
scheduling remains the final authority over which admitted cards are shown.

Retries and profile reopenings do not add duplicate replacements. Linked keys live
in synced note tags, while prepared drafts, failures, and usage counters live in
profile-specific `user_files` JSON. Admission is a native collection transaction
with a grouped undo entry. Original cards and their history are never edited.
Missed days do not accumulate extra introductions. Use only one desktop generator
per collection; concurrent unsynced writers cannot share an admission reservation.

During review, right-click a generated card and choose **Fix this sentence...**.
Tick what's wrong, list words to avoid, give the word you answered (which makes a
side-by-side contrast), or add a note; leave it all blank for a plain reroll. The
feedback stays in the note's `Source` metadata: the writer must fix it, the reviewer
rejects sentences that repeat it, avoided words and rejected sentences are refused in
code, and later audits keep checking it. Each reason also adds a
`chinese-frontier::feedback::<reason>` tag, so the browser shows which problems
recur. After three repairs, a further request tags the note
`chinese-frontier::manual` for you to edit, and automation leaves it alone.
The add-on regenerates that note in place, preserving its card ID,
review history, and scheduling. Existing cards keep their schedules; additional character siblings consume new-card slots. Pausing in Tools
also pauses requested repairs. Restoring the old source is as simple as suspending
the replacement and unsuspending its original; the linked replacement is not
automatically recreated. To undo a conversion permanently, pause automation first.

Your own cards in the chosen deck tree (cloze notes and Basic word cards) offer
**Fix this sentence...** or **Make a sentence for this word...** too. The dialog
adds a **Target** (the reviewed cloze, widened to its dictionary word when only one
character is hidden, so 弄 becomes 弄虚作假) and a **Meaning**, both editable. On OK
only that card is suspended and its note tagged `chinese-frontier::fix-pending`; the
note's fields and other cards are never touched. The request waits in the synced
collection config (`chineseFrontierFixes`) and is generated ahead of other targets.
When a sentence passes, a new Chinese Frontier note tagged `chinese-frontier::adopted`
goes into the card's own subdeck, its card takes over the original's scheduling
(interval, due, ease, reps, lapses, FSRS state), so no new-card slot is used, and the
tag is removed, all in one undo step. The original keeps its review history, which
still counts toward familiar vocabulary. **Restore the original card** on the new
note copies the schedule back, unsuspends the original and leaves the new note
suspended and tagged `chinese-frontier::restored`. Settings list the waiting fixes,
with a Cancel button that unsuspends the card. A fix whose note you edit or delete
stalls there rather than proceeding; nothing is unsuspended automatically.

Automatic refill sizes its generation allowance from remaining native capacity
and observed approval yield. Each cycle is bounded to at most 32 batches; if valid
cards still leave slots unfilled, another cycle can run after a 30-minute cooldown
on the same Anki day. Rejected targets retain their individual backoff and every
sentence still passes the same context and ambiguity checks. Attempts use up to
four targets per batch by default. A buffer of four approved notes is prepared ahead
of admission (`buffer_notes`, clamped to 0–16; set 0 to disable). Buffer generation
can run when today's slots are full. Every card still waits for native admission
capacity, including character siblings. Cached target identities are excluded from
new selection. Context and source evidence are rechecked at admission.

Writers receive a small target-relevant vocabulary bundle with actual established
usage, expanding on subsequent attempts. Retrieval never promotes words merely
visible in examples. The independent auditor checks the proposed sentence against
live evidence and the full validation rules. Compact rejection lessons persist
across batches; they guide rewrites without licensing vocabulary or approving cards.
Empty proposals must explain the obstacle; those reasons and validation failures
refocus the next vocabulary retrieval instead of repeating the same inputs.
Approvals are checkpointed individually so a later timeout retains finished work.
Both languages record the last 30 writer/auditor calls with prompt characters, elapsed time,
phase, target count, and errors, alongside attempted/approved yield counters in
profile state. These are diagnostics, not evidence of linguistic approval.
Setting `auto_fill` to false restores the explicit eight-batch daily ceiling.
The add-on rescans on startup, after sync or review changes, and every five minutes.
Routine runs stay quiet; persistent errors appear once and in the status dialog.

## Verification

```sh
PYTHONPATH=pylib:out/pylib:out/qt out/pyenv/bin/python -m pytest \
  qt/tests/test_chinese_frontier.py
```

Tests use isolated native Anki collections and cover ordinal-specific vocabulary,
word segmentation, new-card and review budgets, source changes during generation,
idempotency, transaction rollback, undo, and schedule-preserving sentence repair.

## French

The same installed add-on now also exposes Tools → French Frontier. It adds
French vocabulary and conjugation notes using independent settings and evidence,
and preserves existing French cards. See [French Frontier](french/README.md).

## Diagnostics

Both Tools dialogs contain Charts, Overview, Model calls and Decisions tabs,
refreshed each second. Charts draws the retained telemetry as static SVG: yield and
rejection losses per Anki day, recent batches, writer prompt size, call latency by
phase, top validation/audit rejection reasons and keyword-bucketed writer refusal
themes. Overview shows the last native budget snapshot, approved buffer, active
request age, cooldown and cycle budget, observed target approval yield, admitted
notes and physical cards, and existing-note updates (including audit metadata).
Model calls include French target ranking as well as writing and independent review.
Select a call for model setting, schema/input/output character counts, targets and
errors. Decisions show validation, language-audit, empty-output and admission
rejections, plus stored deferred reasons. Export diagnostics saves a local JSON report.

Telemetry is profile-local inside the existing state JSON files. It keeps the last
100 completed calls, 300 events and 14 Anki-day summaries, with persistent aggregate
counters. Recent legacy call records are imported once; missing batch history is
not reconstructed. Restarted in-flight calls are marked interrupted with unknown
latency. Character counts are not tokens, billing or subscription quota. A call
returning JSON does not imply a card passed review, and gate acceptance does not
measure learning outcomes. No telemetry is uploaded automatically.
