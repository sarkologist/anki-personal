# French Frontier eval

56 cases × 3 runs: 168 case-runs attempted, 0 not run (budget), 0 cancelled. 446 real model calls, 86 replayed. Case-run time 9364s, wall 2416s.

k = 3.

| | cases | case-runs | yield | pass@1 | pass^k | pass@k |
|---|---|---|---|---|---|---|
| Generate | 40 | 120 | 71% | 68% | 42% | 88% |
| Audit vs label | 16 | 48 | 44% | 88% | 81% | 94% |

Yield: approved case-runs over attempted ones. pass@1: mean pass over case-runs. pass^k: cases passing all k runs. pass@k: cases passing at least once. Audit cases pass when the verdict matches the user's label. Timeouts, errors and refusals count as failures.

## Calls

| phase | calls |
|---|---|
| Checking alternative answers | 203 |
| Writing sentences | 243 |

| status | calls |
|---|---|
| ok | 446 |

## Failures

| outcome | case-runs |
|---|---|
| approved | 106 |
| audit_rejected | 38 |
| refused | 1 |
| validation_rejected | 23 |

| reason | case-runs |
|---|---|
| Incorrect context morphology annotation | 12 |
| audit rejected | 11 |
| Cloze a complete target exactly once | 9 |
| audit approved | 6 |
| check:no_fitting_alternative | 4 |
| writer refused | 1 |
| Conjugation must supply the infinitive | 1 |
| Unlearned grammar | 1 |

Whole-word fits_cloze disagreements: 7.

## Groups

| group | case-runs | yield | pass@1 |
|---|---|---|---|
| audit/audit-kept | 15 | 100% | 100% |
| audit/audit-rejected | 33 | 18% | 82% |
| generate/audit | 33 | 70% | 64% |
| generate/passing | 36 | 78% | 72% |
| generate/refused | 24 | 67% | 67% |
| generate/validation | 27 | 67% | 67% |

## Cases

| case | group | label | why | runs | pass^k | last failure |
|---|---|---|---|---|---|---|
| fr-audit-cogner-1792551372298 | audit-kept | kept | The user kept and reviewed this card (policy 2 audit approved it); the audit should sti… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-audit-collier-1792786184499 | audit-kept | kept | The user kept and reviewed this card (policy 2 audit approved it); the audit should sti… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-audit-consultation-1792306073514 | audit-kept | kept | The user kept and reviewed this card (policy 2 audit approved it); the audit should sti… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-audit-croire-1791922436474 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. … | ✗ approved ✗ approved ✗ approved | ✗ | audit approved |
| fr-audit-demander-1791943636501 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-devoir-1791922902224 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. … | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-dieu-1791943739665 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-dire-1791859380149 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-dire-1791922117647 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. … | ✗ approved ✓ audit_rejected ✗ approved | ✗ | audit approved |
| fr-audit-entendre-1791946229121 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-faire-1791859564007 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. … | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-héritier-1792327214167 | audit-kept | kept | The user kept and reviewed this card (policy 2 audit approved it); the audit should sti… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-audit-poignarder-1791957586438 | audit-rejected | rejected | The user asked for this card to be replaced (policy 1 audit approved it); the audit sho… | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-raison-1791859765962 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✓ audit_rejected ✓ audit_rejected ✓ audit_rejected | ✓ |  |
| fr-audit-tension-1792786323171 | audit-kept | kept | The user kept and reviewed this card (policy 2 audit approved it); the audit should sti… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-audit-être-1791859101341 | audit-rejected | rejected | The user suspended this card (policy 1 audit approved it); the audit should reject it. | ✗ approved ✓ audit_rejected ✓ audit_rejected | ✗ | audit approved |
| fr-gen-aimer-subpre3s | audit |  | Deferred twice with No valid proposal. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-aller-subpre1s | audit |  | Needs je or j' as subject; checks that pronouns and elisions are now usable context. | ✗ validation_rejected ✗ validation_rejected ✗ validation_rejected | ✗ | Cloze a complete target exactly once |
| fr-gen-aube | validation |  | Five rejections for Cloze a complete target exactly once plus two morphology rejections… | ✗ validation_rejected ✗ validation_rejected ✗ validation_rejected | ✗ | Cloze a complete target exactly once |
| fr-gen-avoir-cndpre3s | passing | kept | Conditional conjugation card approved 2026-10-28; conjugation table check. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-balader | audit |  | Audit rejected then accepted in one batch; the approved draft was never admitted. | ✓ approved ✓ approved ✗ audit_rejected | ✗ | audit rejected |
| fr-gen-cap | passing | kept | Approved 2026-10-28 first try; a word-mode card that should keep passing. | ✗ approved ✗ approved ✓ approved | ✗ | check:no_fitting_alternative |
| fr-gen-chirurgie | audit | kept | Deferred seven times on the clue check, later approved. | ✓ approved ✗ approved ✓ approved | ✗ | check:no_fitting_alternative |
| fr-gen-clan | passing | kept | Stem card approved after an audit rejection; exercises fuller-context rewrite then stem. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-coupable | audit | kept | Audit found the French-only context insufficient, later approved. | ✓ approved ✓ approved ✗ audit_rejected | ✗ | audit rejected |
| fr-gen-devoir-cndpre2p | audit | kept | Conjugation audit found the mood unfixed; admitted card has a lapse. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-dire-cndpre1s | audit |  | Deferred six times on Context clues must quote visible French; the clue normalisation f… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-dire-indpre2s | validation |  | Rejected for Missing context verb analysis: tu (a pronoun treated as a verb homograph). | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-décoller | passing | kept | Approved 2026-10-28 after a writer refusal and two audit rejections in the same batch; … | ✓ approved ✗ audit_rejected ✓ approved | ✗ | audit rejected |
| fr-gen-décéder | validation | kept | Rejected for Unlearned context: m, x (tokeniser split), later approved. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-défi | passing | kept | Approved after the auditor rejected an anglicism; stem fallback. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-dénoncer | audit |  | Selected sense is giving notice to end a treaty; a sense-choice problem the ranker shou… | ✗ validation_rejected ✓ approved ✓ approved | ✗ | Conjugation must supply the infinitive |
| fr-gen-envisager | audit | kept | Audit rejected the clue dans son esprit; the admitted card has a lapse. | ✓ approved ✓ approved ✗ audit_rejected | ✗ | audit rejected |
| fr-gen-essayer-subpre3s | audit |  | No subjunctive trigger established (il faut que is not an established construction). | ✗ refused ✗ validation_rejected ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-faire-indpre2s | validation |  | Conjugation of a syncretic surface; deferred four times on morphology annotation. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-instinct | passing | kept | Approved as a contrast pair; the only recent contrast-mode card, checks two-example ren… | ✗ audit_rejected ✓ approved ✗ validation_rejected | ✗ | Cloze a complete target exactly once |
| fr-gen-justifier | refused |  | Deferred five times for lack of claim/evidence context. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-ongle | passing | kept | Approved 2026-10-28 on the second attempt (one morphology annotation rejection first). | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-parler-subpre2s | passing | kept | Present subjunctive card approved 2026-10-28; subjunctive trigger must be established. | ✓ approved ✗ validation_rejected ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-poignarder | refused | repair-requested | Nine writer refusals in the last batches (no blade noun established); the user also req… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-pouvoir-cndpre2s | passing | kept | Conditional conjugation card approved 2026-10-28; checks the conjugation table on the b… | ✓ approved ✓ approved ✗ validation_rejected | ✗ | Unlearned grammar |
| fr-gen-pouvoir-subpre3s | validation |  | Rejected as Answer is not a dictionary-verified target form, then the conjugation audit… | ✓ approved ✗ validation_rejected ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-racheter | passing | kept | Approved as a stem card after two morphology rejections and one audit rejection; tests … | ✗ validation_rejected ✓ approved ✓ approved | ✗ | Incorrect context morphology annotation |
| fr-gen-raser | validation | kept | Rejected for Missing context verb analysis: plus (adverb forced to carry a verb analysi… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-repousser | refused |  | Deferred twice as impossible: physical push-back sense needs collocates. | ✓ approved ✗ audit_rejected ✓ approved | ✗ | audit rejected |
| fr-gen-reçu | refused |  | Writer refused three times: the selected sense (admis, établi) needs idées reçues; sens… | ✗ audit_rejected ✗ audit_rejected ✓ approved | ✗ | audit rejected |
| fr-gen-serment | passing | kept | Approved 2026-10-28 after five earlier refusals for missing oath context; regression se… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-signature | validation |  | Seven validation rejections on 2026-10-28, all Incorrect context morphology annotation;… | ✓ approved ✓ approved ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-soudainement | refused |  | Deferred four times: no established event with an abrupt onset. | ✗ audit_rejected ✗ audit_rejected ✗ audit_rejected | ✗ | audit rejected |
| fr-gen-surnom | refused |  | Deferred four times: no context for an added name. | ✓ approved ✗ validation_rejected ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-sécher | passing | kept | Approved 2026-10-28 but the auditor rejected the first example; flaky between runs. | ✓ approved ✓ approved ✗ validation_rejected | ✗ | Incorrect context morphology annotation |
| fr-gen-tapis | audit | kept | Audit: only a location and a material; later approved in the idiom sense. | ✗ approved ✓ approved ✓ approved | ✗ | check:no_fitting_alternative |
| fr-gen-traquer | refused | kept | Refused earlier (hunting sense impossible), later approved as a stem card. | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-verser | refused | kept | Refused earlier for lack of liquid or container nouns, later approved; sentinel for the… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-vilain | validation |  | Twelve validation rejections across senses on 2026-10-28, mostly morphology annotation;… | ✓ approved ✓ approved ✓ approved | ✓ |  |
| fr-gen-être-indimp1s | validation |  | Rejected for Cloze a complete target exactly once on a one-word syncretic form. | ✗ validation_rejected ✗ validation_rejected ✗ validation_rejected | ✗ | Cloze a complete target exactly once |
