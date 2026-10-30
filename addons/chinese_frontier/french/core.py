# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..core import ValidationError
from .lexicon import Form, Lexicon, normalize, tokens

MODEL = "French Frontier"
TAG = "french-frontier::generated"
KEY_TAG = "french-frontier::key::"
POLICY = 2
# Only these spelling-preserving -er patterns can be generalized. Dictionary
# verification remains mandatory, including for orthographic stem changes.
ER_ENDINGS = {
    "ind:pre:1:s": "e",
    "ind:pre:2:s": "es",
    "ind:pre:3:s": "e",
    "ind:pre:1:p": "ons",
    "ind:pre:2:p": "ez",
    "ind:pre:3:p": "ent",
    "ind:imp:1:s": "ais",
    "ind:imp:2:s": "ais",
    "ind:imp:3:s": "ait",
    "ind:imp:1:p": "ions",
    "ind:imp:2:p": "iez",
    "ind:imp:3:p": "aient",
    "par:pas:m:s": "é",
}

# An elided clitic is spelling, not a new word: it is established through
# its full form, which also keys its reviewed evidence.
ELISIONS = {
    "l'": ("le", "la"),
    "d'": ("de",),
    "qu'": ("que",),
    "n'": ("ne",),
    "j'": ("je",),
    "s'": ("se",),
    "c'": ("ce",),
    "m'": ("me",),
    "t'": ("te",),
    "jusqu'": ("jusque",),
}


def full_form(unit: str, surfaces: Any, following: str = "") -> str:
    if unit in surfaces:
        return unit
    # s'il and s'ils elide si; elsewhere s' is the pronoun se.
    full = ("si",) if unit == "s'" and following in ("il", "ils") else ()
    return next((f for f in full or ELISIONS.get(unit, ()) if f in surfaces), unit)


def answer_pattern(answer: str) -> re.Pattern[str]:
    """Match a whole answer, also after an elided clitic (l'aube, j'étais),
    but never after an apostrophe inside a word (aujourd'hui)."""
    clitics = "|".join(re.escape(e[:-1]) for e in sorted(ELISIONS, key=len)[::-1])
    return re.compile(
        rf"(?<![\w’'])(?P<clitic>(?:{clitics})[’'])?"
        rf"(?P<answer>{re.escape(answer)})(?![\w’'])",
        re.I,
    )


@dataclass
class Knowledge:
    surfaces: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    forms: set[str] = field(default_factory=set)
    meanings: set[tuple[str, str]] = field(default_factory=set)
    constructions: set[str] = field(default_factory=set)
    # Lemma/POS readings each answered surface could have had.
    identities: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    _rules: set[str] | None = field(default=None, repr=False)

    def learn(
        self,
        answer: str,
        lexicon: Lexicon,
        cid: int,
        context: str,
        *,
        partial: bool = False,
        form_key: str = "",
        allowed_keys: tuple[str, ...] = (),
    ) -> None:
        if partial:
            return
        answer = normalize(answer)
        if not answer or not tokens(answer):
            return
        evidence = {"card": cid, "context": context[:800]}
        self.surfaces.setdefault(answer, []).append(evidence)
        analyses = lexicon.analyses(answer)
        if form_key:
            analyses = [f for f in analyses if f.key == form_key]
        elif allowed_keys:
            analyses = [f for f in analyses if f.key in allowed_keys]
        identities = {(f.lemma, f.pos) for f in analyses}
        self.identities.setdefault(answer, set()).update(identities)
        if len(identities) == 1:
            self.meanings.update(identities)
        # A syncretic answer does not establish all its tense/person readings.
        if len({f.key for f in analyses}) == 1:
            self.forms.add(analyses[0].key)
            self._rules = None

    def licenses(self, form: Form, lexicon: Lexicon) -> bool:
        if form.key in self.forms:
            return True
        if form.pos != "VER" or form.lemma == "aller" or not form.lemma.endswith("er"):
            return False
        suffix = ER_ENDINGS.get(form.features)
        if not suffix or form.surface != form.lemma[:-2] + suffix:
            return False
        return form.features in self.regular_rules

    def recognizes(self, form: Form, lexicon: Lexicon) -> bool:
        """Context may use a reading of an answered syncretic surface (fais,
        dit) whose verb meaning is established: recognition, not production.
        Only person/tense syncretism qualifies: a surface answered as a noun
        (été, bois) is never recognised as its verb homograph."""
        identity = (form.lemma, "VER")
        return self.licenses(form, lexicon) or (
            self.identities.get(form.surface) == {identity}
            and identity in self.meanings
        )

    def non_verb_reading(self, surface: str, lexicon: Lexicon) -> bool:
        """Whether a verb homograph reads as a non-verb: its established
        meaning is a unique non-verb, or a non-verb reading dominates."""
        analyses = lexicon.analyses(surface)
        established = {
            (f.lemma, f.pos) for f in analyses if (f.lemma, f.pos) in self.meanings
        }
        if len(established) == 1 and next(iter(established))[1] != "VER":
            return True
        return max((f.frequency for f in analyses if f.pos != "VER"), default=0) > max(
            (f.frequency for f in analyses if f.pos == "VER"), default=0
        )

    @property
    def regular_rules(self) -> set[str]:
        if self._rules is None:
            self._rules = set()
            for feature, suffix in ER_ENDINGS.items():
                lemmas = {
                    key.split("|")[1]
                    for key in self.forms
                    if key.split("|")[-1] == feature
                    and key.split("|")[1].endswith("er")
                    and key.split("|")[1] != "aller"
                    and key.split("|")[0] == key.split("|")[1][:-2] + suffix
                }
                if len(lemmas) >= 2:
                    self._rules.add(feature)
        return self._rules

    def feature_known(self, feature: str) -> bool:
        return any(key.split("|")[-1] == feature for key in self.forms)

    def prompt(self) -> dict[str, Any]:
        return {
            "established_context": sorted(self.surfaces),
            "directly_tested_forms": sorted(self.forms),
            "established_lemmas": [list(x) for x in sorted(self.meanings)],
            "established_constructions": sorted(self.constructions),
            "regular_er_rules": {
                feature: ER_ENDINGS[feature] for feature in sorted(self.regular_rules)
            },
        }

    @property
    def version(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [POLICY, self.prompt()], sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()


LEARNER_FIELDS = (
    "learner_feedback",
    "avoid_words",
    "rejected_sentences",
    "reported_confusions",
)


@dataclass(frozen=True)
class Target:
    lemma: str
    pos: str
    sense: str
    kind: str
    definition: str
    level: int
    frequency: float
    form_key: str = ""
    # The learner's complaints about earlier versions of this card; never part
    # of the key, and never stored in the note's target metadata.
    learner_feedback: tuple[str, ...] = ()
    avoid_words: tuple[str, ...] = ()
    rejected_sentences: tuple[str, ...] = ()
    # Words the learner confuses with the target; only these may be contrasted.
    reported_confusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Drafts reread from JSON carry lists.
        for name in LEARNER_FIELDS:
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def stored(self) -> dict[str, Any]:
        """The target metadata kept on the note, without the learner's feedback."""
        return {k: v for k, v in asdict(self).items() if k not in LEARNER_FIELDS}

    def prompt(self) -> dict[str, Any]:
        """The target for the writer and reviewer; feedback only when there is some."""
        return {
            k: v for k, v in asdict(self).items() if v or k not in LEARNER_FIELDS
        } | {"key": self.key}

    @property
    def key(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [self.lemma, self.pos, self.sense, self.kind, self.form_key],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Candidate:
    sentence: str
    answer: str
    translation: str
    meaning: str
    mode: str = "word"
    cue: str = ""
    uses: tuple[dict[str, str], ...] = ()
    grammar: tuple[str, ...] = ()
    constructions: tuple[str, ...] = ()
    contrast: dict[str, Any] | None = None
    explanation: str = ""

    @classmethod
    def read(cls, data: dict[str, Any]) -> Candidate:
        if not isinstance(data, dict):
            raise ValidationError("French proposal must be an object")
        names = (
            "sentence",
            "answer",
            "translation",
            "meaning",
            "mode",
            "cue",
            "explanation",
        )
        if any(not isinstance(data.get(name), str) for name in names):
            raise ValidationError("French proposal has invalid string fields")
        arrays = ("uses", "grammar", "constructions")
        if any(not isinstance(data.get(name), list) for name in arrays):
            raise ValidationError("French proposal has invalid annotations")
        return cls(
            **{name: data[name].strip() for name in names},
            **{name: tuple(data[name]) for name in arrays},
            contrast=data.get("contrast"),
        )


@dataclass(frozen=True)
class Validated:
    text: str
    translation: str
    dependencies: tuple[str, ...]
    form_key: str
    candidate: Candidate


def _units(sentence: str, known: set[str], answer: str) -> list[str]:
    pieces = tokens(sentence)
    result = []
    phrases = {tuple(tokens(s)): s for s in known | {normalize(answer)}}
    index = 0
    while index < len(pieces):
        match = next(
            (
                (seq, word)
                for seq, word in sorted(phrases.items(), key=lambda x: -len(x[0]))
                if tuple(pieces[index : index + len(seq)]) == seq
            ),
            None,
        )
        if match:
            result.append(match[1])
            index += len(match[0])
        else:
            result.append(pieces[index])
            index += 1
    return result


def contrast_form(other: Candidate, known: Knowledge, lexicon: Lexicon) -> Form:
    alternatives = lexicon.analyses(other.answer)
    declared = [
        f
        for f in alternatives
        if any(
            isinstance(use, dict)
            and normalize(use.get("surface", "")) == normalize(other.answer)
            and normalize(use.get("lemma", "")) == f.lemma
            and use.get("pos") == f.pos
            and use.get("features") == f.features
            for use in other.uses
        )
    ]
    if declared:
        alternatives = declared
    elif other.cue:
        alternatives = [
            f
            for f in alternatives
            if f.pos == "VER" and f.lemma == normalize(other.cue)
        ]
    f = next(
        (f for f in alternatives if f.pos != "VER" or known.licenses(f, lexicon)),
        None,
    )
    if f is None:
        raise ValidationError("Contrast requires an established target form")
    return f


def spelled(text: str) -> set[str]:
    """Tokens with each elided clitic also read as its full forms."""
    words = set(tokens(text))
    for word in list(words):
        words.update(ELISIONS.get(word, ()))
        words.update(("si",) if word == "s'" else ())
    return words


def answers_confusion(other: Candidate, form: Form, target: Target) -> None:
    if target.reported_confusions and not (
        {normalize(other.answer), form.lemma} & set(target.reported_confusions)
    ):
        raise ValidationError("Contrast pairs only answer a reported confusion")


def respect_feedback(c: Candidate, target: Target) -> None:
    if target.reported_confusions and c.mode != "contrast":
        raise ValidationError("A reported confusion needs a contrast pair")
    if " ".join(c.sentence.split()) in target.rejected_sentences:
        raise ValidationError("The learner rejected this sentence")
    context = set(tokens(c.sentence)) - set(tokens(c.answer))
    context = spelled(" ".join(context))
    avoided = [
        normalize(w)
        for w in target.avoid_words
        if tokens(w) and all(t in context for t in tokens(w))
    ]
    if avoided:
        raise ValidationError("The learner asked to avoid: " + ", ".join(avoided))


def validate(
    c: Candidate, target: Target, known: Knowledge, lexicon: Lexicon
) -> Validated:
    if c.mode not in ("word", "contrast", "stem") or not 5 <= len(c.sentence) <= 400:
        raise ValidationError("Invalid French sentence or disambiguation mode")
    respect_feedback(c, target)
    if (
        any(ch in c.sentence + c.answer + c.cue for ch in "<>{}\n\r")
        or not c.translation.strip()
        or len(c.translation) > 800
    ):
        raise ValidationError("Use plain French and a complete English translation")
    answer = normalize(c.answer)
    if target.kind == "vocabulary" and re.search(
        r"(?<![\w'’])" + re.escape(answer) + r"(?![\w'’])", normalize(c.translation)
    ):
        raise ValidationError("English translation reveals the French answer")
    forms = [
        f
        for f in lexicon.analyses(answer)
        if f.lemma == target.lemma
        and f.pos == target.pos
        and (not target.form_key or f.key == target.form_key)
    ]
    if target.kind == "vocabulary" and target.pos == "VER" and forms:
        context_features = {
            use.get("features") for use in c.uses if use.get("pos") == "VER"
        }
        target_features = set(c.grammar) - context_features
        declared = [
            f for f in forms if f.features in (target_features or set(c.grammar))
        ]
        if len({f.key for f in declared}) > 1:
            raise ValidationError("Declare the target's exact verb morphology")
        if declared:
            forms = declared
    form = next(iter(forms), None)
    if not form:
        raise ValidationError("Answer is not a dictionary-verified target form")
    spans = [m.span("answer") for m in answer_pattern(c.answer).finditer(c.sentence)]
    if len(spans) != 1:
        raise ValidationError("Cloze a complete target exactly once")
    units = _units(c.sentence, set(known.surfaces), answer)
    resolved = {
        unit: full_form(unit, known.surfaces, next(iter(tokens(following)), ""))
        for unit, following in zip(units, units[1:] + [""])
    }
    dependencies = tuple(
        dict.fromkeys(
            resolved[unit] for unit in units if unit != answer and not unit.isdecimal()
        )
    )
    unknown = [unit for unit in dependencies if unit not in known.surfaces]
    if unknown:
        raise ValidationError("Unlearned context: " + ", ".join(unknown))
    if target.kind == "conjugation":
        if (target.lemma, target.pos) not in known.meanings or not target.form_key:
            raise ValidationError("Conjugation requires an established verb meaning")
        if normalize(c.cue) != target.lemma:
            raise ValidationError("Conjugation must supply the infinitive")
    elif c.cue:
        raise ValidationError("Vocabulary cards must not reveal the target lemma")
    if form.pos == "VER" and target.kind == "vocabulary":
        # Infinitives introduce meaning in an already demonstrated construction;
        # finite forms require a demonstrated regular pattern.
        if form.features == "inf":
            if not known.feature_known("inf"):
                raise ValidationError("Unlearned target grammar: infinitive")
        elif not known.licenses(form, lexicon):
            raise ValidationError("Unlearned target grammar: " + form.features)
    if any(not isinstance(g, str) for g in c.grammar + c.constructions):
        raise ValidationError("Invalid grammar annotations")
    allowed_feature = form.features if target.kind == "conjugation" else ""
    for feature in c.grammar:
        actual = {form.features} | {
            use.get("features", "") for use in c.uses if isinstance(use, dict)
        }
        expanded = [f for f in actual if f == feature or f.startswith(feature + ":")]
        if not expanded or any(
            f != allowed_feature and not known.feature_known(f) for f in expanded
        ):
            raise ValidationError("Unlearned grammar: " + feature)
    for construction in c.constructions:
        if construction not in known.constructions:
            raise ValidationError("Unlearned construction: " + construction)
    # Exhaustively identify the dictionary analysis used for each context unit.
    uses = {}
    for use in c.uses:
        if not isinstance(use, dict) or any(
            not isinstance(use.get(k), str)
            for k in ("surface", "lemma", "pos", "features")
        ):
            raise ValidationError("Invalid morphology annotation")
        key = "|".join(
            normalize(use[k]) if k in ("surface", "lemma") else use[k]
            for k in ("surface", "lemma", "pos", "features")
        )
        surface = normalize(use["surface"])
        surface = resolved.get(surface, full_form(surface, known.surfaces))
        matching = [
            f
            for f in lexicon.analyses(use["surface"]) or lexicon.analyses(surface)
            if f.lemma == normalize(use["lemma"])
            and f.pos == use["pos"]
            and (
                f.features == use["features"]
                or f.features.startswith(use["features"] + ":")
                # Agreement is optional for non-verbs (je PRO for je PRO s).
                or (not use["features"] and f.pos != "VER")
            )
        ]
        selected = (
            matching[0]
            if len(matching) == 1
            else next((f for f in matching if f.key == key), None)
        )
        if selected and normalize(use["surface"]) == answer:
            if selected.key != form.key:
                raise ValidationError("Incorrect target morphology annotation")
            continue
        if not selected or surface not in dependencies:
            raise ValidationError("Incorrect context morphology annotation")
        uses[surface] = selected
        if selected.pos == "VER" and not known.recognizes(selected, lexicon):
            raise ValidationError("Unlearned context verb form: " + selected.surface)
    for unit in dependencies:
        # An explicit non-verb annotation also counts: it is what the audit sees.
        if any(f.pos == "VER" for f in lexicon.analyses(unit)) and unit not in uses:
            raise ValidationError("Missing context verb analysis: " + unit)
    start, end = spans[0]
    replacement = "{{c1::" + c.sentence[start:end] + "}}"
    if c.mode == "stem":
        # Keep an actual orthographic cue; never turn this into multiple new cards.
        cue = c.explanation
        if (
            not cue
            or cue == c.answer
            or not c.answer.startswith(cue)
            or len(c.answer) - len(cue) < 2
        ):
            raise ValidationError(
                "Stem fallback requires a proper answer prefix in explanation"
            )
        replacement = (
            html.escape(cue) + "{{c1::" + html.escape(c.answer[len(cue) :]) + "}}"
        )
    text = html.escape(c.sentence[:start]) + replacement + html.escape(c.sentence[end:])
    translation = html.escape(c.translation)
    if c.mode == "contrast":
        if not isinstance(c.contrast, dict):
            raise ValidationError("Missing contrast example")
        other = Candidate.read(c.contrast)
        if (
            normalize(other.answer) == answer
            or normalize(other.answer) not in known.surfaces
        ):
            raise ValidationError("Contrast requires a different established answer")
        alternatives = lexicon.analyses(other.answer)
        if not alternatives or other.mode != "word":
            raise ValidationError("Unsupported contrast answer")
        f = contrast_form(other, known, lexicon)
        answers_confusion(other, f, target)
        other_target = Target(
            f.lemma,
            f.pos,
            "established",
            "conjugation" if f.pos == "VER" else "vocabulary",
            "established",
            1,
            0,
            f.key,
        )
        # The second example obeys the avoided words; the pair is rejected as
        # a whole through its first sentence.
        respect_feedback(
            other, replace(target, rejected_sentences=(), reported_confusions=())
        )
        other_valid = validate(other, other_target, known, lexicon)
        if normalize(other.answer) in normalize(
            c.sentence.replace(c.answer, "")
        ) or answer in normalize(other.sentence.replace(other.answer, "")):
            raise ValidationError("Contrast reveals the other answer")
        text = "<div>" + text + "</div><br><div>" + other_valid.text + "</div>"
        translation = (
            "<div>"
            + translation
            + "</div><br><div>"
            + other_valid.translation
            + "</div>"
        )
        dependencies = tuple(dict.fromkeys((*dependencies, *other_valid.dependencies)))
    elif c.contrast is not None:
        raise ValidationError("Only contrast mode permits a second example")
    return Validated(text, translation, dependencies, form.key, c)
