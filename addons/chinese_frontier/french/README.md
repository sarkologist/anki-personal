# French Frontier

Tools → French Frontier selects the French destination, pauses generation, or
switches between signed-in Codex and Claude subscription CLIs. French settings and
profile state are independent of Chinese settings. Desktop Anki must remain open;
there is no paid API fallback. Existing French notes, their templates, suspension
state, card IDs and review histories remain intact. Only notes created with the
French Frontier model and generated tag can be repaired.

## Sources

The first run downloads and caches resources; dictionary senses are fetched as
needed. No dictionary or corpus data is bundled in the add-on.

- [FLELex CRF and Beacco](https://cental.uclouvain.be/cefrlex/flelex/download/):
  learner frequency and level, CC BY-NC-SA 4.0. Cite François et al. (2014),
  and Pintard & François (2020). Only entries also present in the morphological
  index are eligible; this conservatively excludes many multiword expressions.
- [Lexique 4.1](https://www.lexique.org/): subtitle frequencies, lemmas and
  pronunciation, CC BY-SA 4.0. Cite New et al. (2026).
- [Lefff 3.4](https://pauillac.inria.fr/~sagot/): cross-checks every candidate
  verb analysis, LGPL-LR; cite Sagot (2010). Its original GForge download is
  unavailable, so the add-on uses the pinned redistribution in
  [FrenchLefffLemmatizer](https://github.com/ClaudeCoulombe/FrenchLefffLemmatizer).
  Unsupported or conflicting verb analyses are excluded.
- [DBnary](https://kaiko.getalp.org/about-dbnary/download/): dictionary senses
  extracted from Wiktionary. Preserve Wiktionary attribution and share-alike
  terms when reusing the cached data. Source sense URIs are saved on notes.

These resources identify targets, not familiar vocabulary. Target difficulty is
calibrated to the 90th percentile of established content-word levels once at least
30 such words are available, capped at B2 to resist rare advanced outliers. This
is a vocabulary selection heuristic, not a CEFR proficiency assessment. Within
that range, intermediate inventories select in the less frequent quarter of their
reviewed content vocabulary, because source levels can also contain very common
words. Candidate frequency then ranks words within that band. Explicitly obsolete
dictionary senses are skipped. For intermediate inventories, the subscription model
then selects suitable targets from a verified candidate pool using actual reviewed
examples. It can omit elementary words, obvious cognates and unsuitable topics;
selection cannot add targets outside the pool. A short selection defers omitted
targets so subsequent batches can explore farther. Smaller inventories start
at A1. `minimum_vocabulary_level` can override the automatic floor (1=A1 through
6=C2; 0=automatic). Intermediate inventories reserve conjugation work for new
conditional/subjunctive forms instead of elementary indicative drills. Smaller
inventories normally reserve one place for a frequent missing conjugation of an
established verb; calibrated selection can favor vocabulary instead. Deferred targets yield
to other targets. The first dictionary sense is tried for a new lemma; legacy
knowledge does not claim exhaustive sense coverage. Already-established headwords
are skipped conservatively, rather than automatically introducing their other
senses.

## Evidence

Only the answers tested by live cloze ordinals can establish context. Good/Easy on
two different Anki study days since the last Again is required. Buried cards remain
eligible; suspended, unseen and leech cards do not. Source context provides meaning
metadata, never evidence that visible words are learned. Basic notes are currently
not used as evidence; this user's French deck consists of cloze notes.

Accents, apostrophes, expressions and inflections are preserved. Each learned
surface, lemma/POS and exact verb form is tracked separately. Explicit adjacent
subjects, supplied infinitive hints and an immediately preceding avoir auxiliary
can resolve legacy analyses. A following infinitive can distinguish a finite verb
from a noun homograph without guessing its person or tense. Standalone present subjects can distinguish present indicative from a syncretic present subjunctive; genuine tense ambiguity remains unresolved. Reviewed negation and avoir-participle frames establish construction recognition without promoting their visible words. Unresolved person or tense readings stay ambiguous.
This is conservative inference from sentence context, not a full linguistic parse.

A separate coverage inventory records whole answers and the component words of
phrase answers on all unsuspended cloze ordinals, including unseen, struggling and morphologically ambiguous cards. It
prevents reintroducing an already tested headword or possible verb reading without
granting any context knowledge. A pending card reserves its target but supplies no
familiar context. Admission rechecks this coverage after generation as well.

Regular spelling-preserving -er forms can transfer after the same form has been
learned on two distinct verbs. Each transferred form must still exist in Lefff and
Lexique. Irregular verbs and stem changes require specific evidence. Infinitives
do not establish whole paradigms; partial-word cards do not establish whole words.
Constructions have a separate inventory and cannot be introduced implicitly by a
vocabulary card. Automatic extraction of broad grammar rules from old cards is
intentionally conservative; unsupported constructions are deferred.

## Cards and audits

Vocabulary cards introduce one meaning using established grammar. Conjugation
cards introduce one dictionary-verified form of an established verb and supply its
infinitive on the front. Complete English translations are visible beside the
French blank. Vocabulary translations must not reveal the French answer verbatim.
Target verb morphology is resolved from its declared grammar rather than the
ordering of dictionary readings. Context verb analyses are checked explicitly, including auxiliary
and participle forms. Pronunciation metadata remains available in the cached
lexicon; this version does not create pronunciation or listening cards.

Independent subscription-model review checks sense, grammar, translation, known
context, and competing substitutions for the actual clozed target. Context-verb
analysis stays in the review rationale rather than being mislabeled as a competing
noun completion. A fitting alternative overrides
approval. Rewrites and known-word contrast pairs precede a stem cue fallback;
fallbacks must leave at least two letters to recall. All modes create one card.
Contrast and stem cards display only **Disambiguation** on both sides. The hidden
editor field `Disambiguation` records the checked alternatives, judgments,
provider, content fingerprint and referenced context evidence. Review templates
never display that field. Model review is not an exhaustive ambiguity proof.
The `Source`, `Dependencies` and `Disambiguation` metadata fields are collapsed
in the editor and excluded from unqualified searches.

Source and presentation hashes bind approved drafts to their actual content.
Admission rechecks current evidence, settings, duplicates and native budgets.
Approved drafts are checkpointed immediately after each independent review and
persist through later request failures, pauses and profile reopenings. A later
timeout admits already approved work and automatically retries remaining slots
with a smaller batch after two minutes; repeated timeouts back off up to fifteen
minutes. Two completed batches restore the configured batch size. Account errors
retain the thirty-minute backoff.

Generation retains the complete established-word inventory but omits duplicate
non-verb morphology/headword lists and inadmissible target forms. Audit prompts
contain the actual sentence dependencies, tested contrast/verb-meaning examples
and their exact relevant forms, rather than repeating the entire inventory. Every
used evidence record remains bound to the draft and rechecked at admission.
Independent review criteria and deterministic quality checks remain unchanged.
Tools → French Frontier displays the writing/audit phase, attempt and elapsed
seconds, disables duplicate checks while busy, and records the last thirty model
call timings, input sizes and errors in profile state. Complete prompts and response bodies are not retained
in this timing log.
Automatic refill continues while native admission slots remain. Its cycle allowance
adapts to the remaining slots and observed proposal approval rate, up to 32 batches
per cycle. An exhausted cycle or no eligible targets pauses for 30 minutes, then
retries automatically on the same day. Deferred targets still back off, and account
errors still pause generation. Statistics and cooldowns survive profile reopening;
a new Anki day resets them. This bounds each burst of subscription work without
making a low fixed daily cap prevent filling the deck. Ten remains an admission
ceiling, not a guarantee that ten acceptable cards exist. All existing new cards,
including buried cards, reserve capacity. Deck/ancestor limits, today overrides
and the review-limit switch share the Chinese workflow's conservative accounting.
Vocabulary and conjugation consume the same French new-card budget; missed days
never accumulate introductions.

Only generated French notes are automatically rechecked when their visible fields
or policy change. Right-click one during review to request a replacement. Repairs
preserve card IDs/history/scheduling, reject concurrent edits, and consume no new
slot because this implementation keeps exactly one card per note. Originals never
enter the repair queue. Native operations are grouped for undo. Use one desktop
generator per collection; unsynced devices cannot share admission reservations.

French context must carry the meaning with the entire target hidden, before the
reviewer considers English or spelling cues. Describe behavior, causes, consequences
or contrasts; generic frames such as “Il explique sa…” do not qualify. The hidden
`Disambiguation` field records a French-only judgment and exact situational clues
for each example, followed by the alternative-answer checks. A prefix can resolve
remaining synonym ambiguity, but cannot compensate for missing semantic context.
Existing generated notes are queued for repair when this policy changes; original
notes remain untouched and repairs preserve card IDs and scheduling.

## Preparation

Both languages use the shared approved-note buffer (`buffer_notes`, default 4,
0 disables it). Preparation may run when today's slots are full; native admission
limits and all source, context, conjugation and ambiguity checks still apply.
Writers receive a compact, relevant selection of proven vocabulary and reviewed
usage, plus persistent rejection lessons. Examples do not establish additional words.
French can fill a missing context analysis only when exactly one dictionary reading
has direct learned-form evidence. Explicit model annotations are never overwritten;
ambiguous or unproven forms still require valid annotations and independent review.
No new paradigm or grammar permission is inferred by annotation completion.
