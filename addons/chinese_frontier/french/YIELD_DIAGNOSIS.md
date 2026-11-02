# French Frontier yield diagnosis (2026-10-22)

Telemetry since 2026-10-15: 29 approved of 2250 attempted targets (1.3%).
Failures: about 6400 empty writer proposals, 137 validation rejections and
111 audit rejections. About 2.9 empty proposals per target means most targets
are refused on all three writer attempts. Writer refusal is over 90% of the
loss. Validation and audit rejections only matter once the writer produces
something.

Evidence comes from code on `upstream/claude/frontier-claude-compat`
(cd03333f5), the installed `french-state.json` (last 300 events, last 100
calls and 1297 deferred lessons) and a collection copy from 10:56 today. That
copy was used because the live DB was locked. Scratch scripts are in this
session's scratchpad (`load.py`, `sel.py`, `val.py`, `rev.py`, `basic.py`).
No model calls were made.

## Root cause: the lexicon has no articles, pronouns or determiners

`Lexicon.parse` keeps a Lexique row only when its `5_Cgram` is exactly one of
`ver nom adj adv art pro pre con ono`. Lexique tags closed-class words with
subtypes, such as `ART:def`, `PRO:per`, `ADJ:pos`, `ADJ:dem` and `AUX`, so all
of them are dropped. The cached `lexicon-v3.json` has **0 ART and 0 PRO
forms**. Only homograph noise survives:

| surface | analyses in lexicon |
|---|---|
| le | `le NOM m` (0.25/M) |
| la | `la NOM m:i` (musical note) |
| il | `il NOM` |
| lui | **only** `luire VER par:pas:*` |
| son, ce, cette, ses, leur | none |

Downstream effects, each confirmed offline:

1. **The writer is never shown `le`, `la`, `il`, `on`, `je`, `une` or `les`.**
   `generate` shows the writer only the `context_bundle` subset of
   established surfaces: 32 frequency anchors plus query matches, 96 to 160
   in total. Anchors are sorted by lexicon frequency, so `le` ranks 1636th
   and `il` 1350th of 1896, and neither is ever selected. All of these words
   *are* in `Knowledge.surfaces`. **77 of 114** recent writer refusals cite
   missing articles, determiners or pronouns, for example "no articles
   le/la", "subject pronoun 'tu' is not in established_context" and "'je'
   nor 'j'' is an established surface". **22** refusals cite nothing else,
   including every recent `cnd:pre`/`sub:pre` conjugation target.
2. **Content words that the writer asks for are also hidden.** Relevance uses
   only the target lemma, definition and lesson text, so collocates never
   match. `amour`, `peur`, `client`, `main`, `table`, `juge`, `demande` and
   `besoins` are established but not shown at any attempt size (`sel.py`).
   The full inventory is only 20 kB of JSON, against a 53 kB prompt.
3. **`lui` can never pass validation.** Its only analyses are verbs, and
   none is licensed, so `context_analyses["lui"]` is `[]`. Omitting it gives
   "Missing context verb analysis: lui" (3 of 13 recent real validation
   rejections). Annotating it as a pronoun gives "Incorrect context
   morphology annotation".
4. **Most "Incorrect context morphology annotation" errors come from correct
   annotations.** Any `uses` entry must match a lexicon analysis exactly. On
   a sentence that otherwise passes (`val.py`):
   - `son ADJ m:s` fails: no entry exists.
   - `je PRO` fails: no entry exists.
   - `secrétaire NOM m:s` fails: the lexicon says `e:s`.
   - `que CON` passes.

   Claude helpfully annotates non-verbs, and every such annotation of a
   closed-class word is fatal.

## Other format and strictness failures

**Elisions are separate unknown words.** `l' d' qu' n' j' s' c'` are absent
from `Knowledge.surfaces`, while `le la de que ne je se ce` are present. Any
sentence with `l'` gets "Unlearned context: l'". The elided clitic is
spelling, not new knowledge.

**Noun/verb homographs force verb analyses.** `plus` (adverb, 3746/M) and
`affaire` (noun, 257/M) need a verb annotation because `plaire` and
`affairer` readings exist. `annotate_context` also does the reverse: 53
established noun or adjective homographs, such as `bois`, `garde`, `fête` and
`passé`, are silently tagged with their only proven *verb* form. The auditor
noticed `bois` tagged as `boire ind:pre:1:s` in "matériel en bois".

**Syncretic verb surfaces are unusable as context.** `learn` adds a form only
when its analysis is unique, so `fais`, `dis`, `dit`, `attends`, `viennent`,
`rends`, `doivent` and 20 more established surfaces have no licensed analysis
and always fail. The writer cites "'dit' has no verb analysis".

**The audit clue check is a literal, case-sensitive substring test**
(`clue not in contexts[index]`, `rev.py`). These all reject the *whole*
review:

| clue | verdict |
|---|---|
| `dans son esprit` | accepted |
| `Dans son esprit` (case) | rejected |
| `avant d'agir` when the sentence has `’` | rejected |
| `ne … pas encore` / `ne...pas encore` (discontinuous) | rejected |
| `dans  son esprit` (whitespace) | rejected |
| `« dans son esprit »` | rejected |
| one bad clue among good ones | rejected |

This is **6 of 16** recent audit rejections, 3 of them on stem or
post-stem candidates. Claude's own audit reasons use `…` to join
discontinuous clues, as in "'ne vont pas rendre des comptes … plutôt'". The
raw review is not stored, so the exact failing clue is inferred, not seen.
A clue error also replaces the useful feedback with "Context clues must
quote…", so the writer rewrites a sentence that was fine.

**Conjugation audits judge the wrong thing.** `AUDIT_INSTRUCTIONS` tells the
auditor to ignore infinitive cues in the French-only check. A conjugation
card supplies its infinitive, so `pouvoir sub:pre:3:s` was rejected twice
because nothing distinguishes "puisse from veuille, doive". That tests the
lemma, which the cue already gives, instead of the mood or person, which is
the actual test.

**The stem fallback is logged as a validation rejection.** "Try fuller
context and contrast before stem fallback" (5 of 18) is the stem candidate
being stashed by design, not a defect. It inflates the rejection count.

## Genuine constraints after the artefacts are removed

**Sense 1 is often not the modern sense.** `choose_targets` takes
`definitions[0]` after dropping explicitly obsolete senses. In the 64 cached
sense files, first senses include:

- crucial: "en forme de croix"
- prime: "première heure canoniale"
- syndicat: "(1477) Fonction de syndic"
- protester: "promettre formellement"
- montage: "transporter de bas en haut"
- digital: "qui appartient aux doigts"

Writer refusals for épuiser ("mettre à sec en puisant"), bourrer ("garnir de
bourre"), dénoncer (end a treaty), portefeuille (dated case) and disposer
come from this. These cards are both unwritable and poor learning targets.

**The established vocabulary is sparse at the bottom.** Only 51% of A1
content entries are established, and 21% of A2. `eau`, `porte`, `problème`,
`police`, `aimer`, `argent` and `enfant` are missing, although `porte`,
`problème` and `enfant` appear in 5 to 9 reviewed card contexts. Targets are
B1+ (floor 3) in the rarer quarter. Rare, concrete senses need concrete
collocates the learner hasn't been tested on, so even a perfect writer will
refuse many targets.

**Real audit rejections are reasonable.** Examples are synonym overlap
(embaucher/engager/recruter, matériel/équipement, préoccuper/inquiéter) and
generic frames (poignarder, honorer). These reflect the quality bar and
should stay.

## Answers

1. **Target selection versus coverage.** Partly. The established set is not
   too small, at 1896 surfaces, but **the writer only sees about 100 of them
   and never the function words**. The function words are also missing from
   the lexicon. This is the dominant cause. Behind it, sense-1 selection and
   B1+ rare targets demand collocates that are genuinely missing.
2. **Validation format mismatches.** Yes. About 10 of 13 recent real
   validation rejections are format or lexicon artefacts: the missing
   `ART`/`PRO`/`ADJ:pos` analyses, `lui`, elisions and homograph annotation
   rules. None of them is a real error in Claude's sentence.
3. **Audit clue check.** Yes. Case, apostrophe, whitespace, quote marks and
   `…`-joined fragments all void the review. That is about 37% of recent
   audit rejections.

## Recommended fixes, ranked by expected impact

Each fix needs a failing test first, as with the earlier fixes.

1. **Parse Lexique POS by prefix.** Use `cgram.split(":")[0]` (`ART`, `PRO`,
   `ADJ`, `AUX`→`VER`) and keep the subtype, then bump the lexicon cache to
   v4. This restores `le`, `il`, `lui`, `son` and the others, fixes anchor
   ranking, and makes correct pronoun and determiner annotations validate.
   *Check:* FLELex `DET`/`PRO` entries become rankable, so confirm that the
   floor or ceiling still excludes them as targets.
2. **Show the writer the complete `established_context`.** That is 20 kB.
   Keep `context_bundle` only for choosing `context_evidence` examples.
   Failing that, always pin the established closed-class words. Together
   with fix 1, this should turn most of the 77 function-word refusals into
   attempts. The 22 refusals that cite only function words should become
   writable at once.
3. **Treat elided clitics as their full forms in `_units` and dependency
   checks.** `l'`↔`le`/`la`, `d'`↔`de`, `qu'`↔`que`, `n'`↔`ne`, `j'`↔`je`,
   `s'`↔`se`, `c'`↔`ce`, plus `m'`, `t'` and `jusqu'`.
4. **Normalise audit clues.** Apply `normalize` (casefold, `’`→`'`, collapse
   spaces) and strip surrounding quotes. Split on `…` or `...` and require
   each fragment to be visible. Then ignore invalid clues and fail only when
   a `sufficient: true` check has no valid clue left. Store the raw clue in
   the telemetry reason. This keeps the anti-hallucination intent.
5. **Pick the sense deliberately.** Pass all non-obsolete senses to the
   ranker and let it choose the current, common sense, or skip the lemma.
   Ask it to judge writability from the full established inventory, which
   the ranker already receives.
6. **Fix the context verb-analysis rules.**
   - Don't require a verb annotation when the established evidence for that
     surface is a unique non-verb meaning, or when its non-verb reading
     dominates by frequency (`plus`, `affaire`).
   - Make `annotate_context` fill from established *meanings*, not only
     proven verb forms, so `bois` is not tagged as `boire`.
   - License a context use of an established syncretic surface (`fais`,
     `dit`) when the declared analysis's lemma is an established meaning.
     Recognising a form in context is not producing it.
7. **Fix the conjugation audit.** In the French-only check, treat the
   infinitive cue as given and check that the French fixes mood, tense and
   person, for example a `souhaiter que` trigger plus the subject.
8. **Telemetry hygiene.** Log the stem stash as `fallback`, not `rejected`.
   Persist the raw writer and audit JSON for rejected items, which makes
   cases like the clue failures directly visible next time.
9. **Policy decision for the user** (only if yield is still low after 1 to
   7):
   - Accept words visible in at least 2 reviewed contexts as *context*
     familiarity. Reading a word you see at every review is recognition,
     not recall. This adds 126 A1 entries.
   - Alternatively, schedule occasional gap-filling targets for the
     high-frequency basics that writer lessons name as blockers most often.

   Either relaxes a stated README principle, so it is the user's call.

Fixes 1 to 4 are mechanical. They don't lower the quality bar: the auditor's
semantic checks are untouched. Fixes 1 and 2 address the largest share. A
live batch is needed after them to measure real yield, because refusals
often cite several blockers and it can't be predicted offline how many
become writable.
