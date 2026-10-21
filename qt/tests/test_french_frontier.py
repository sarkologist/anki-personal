# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.core import ValidationError  # noqa: E402
from chinese_frontier.french.core import (  # noqa: E402
    POLICY,
    Candidate,
    Knowledge,
    Target,
    validate,
)
from chinese_frontier.french.lexicon import (  # noqa: E402
    Form,
    Lexicon,
    normalize,
    tokens,
)


def dictionary():
    return Lexicon(
        [
            Form("je", "je", "PRO"),
            Form("tu", "tu", "PRO"),
            Form("le", "le", "ART"),
            Form("train", "train", "NOM"),
            Form("prends", "prendre", "VER", "ind:pre:1:s"),
            Form("prenions", "prendre", "VER", "ind:imp:1:p"),
            Form("regarde", "regarder", "VER", "ind:pre:1:s"),
            Form("parle", "parler", "VER", "ind:pre:1:s"),
            Form("emprunte", "emprunter", "VER", "ind:pre:1:s"),
            Form("emprunter", "emprunter", "VER", "inf"),
        ],
        [],
    )


def learned(*surfaces):
    lex = dictionary()
    k = Knowledge()
    for surface in surfaces:
        k.learn(surface, lex, 1, surface)
    return k


def candidate(answer="emprunte"):
    return Candidate(
        "Je emprunte le train.",
        answer,
        "I borrow the train.",
        "borrow",
        "word",
        "",
        (),
        ("ind:pre:1:s",),
        (),
    )


def target():
    return Target("emprunter", "VER", "borrow", "vocabulary", "dictionary", 1, 10)


def test_infinitive_does_not_establish_conjugated_forms():
    k = learned("emprunter")
    assert "emprunte" not in k.surfaces
    assert not k.licenses(dictionary().analyses("emprunte")[0], dictionary())


def test_one_regular_verb_does_not_license_an_entire_pattern():
    k = learned("regarde")
    assert not k.licenses(dictionary().analyses("emprunte")[0], dictionary())


def test_two_regular_verbs_license_only_the_demonstrated_form():
    k = learned("regarde", "parle")
    assert k.licenses(dictionary().analyses("emprunte")[0], dictionary())
    assert not k.licenses(dictionary().analyses("prenions")[0], dictionary())


def test_vocabulary_target_rejects_unknown_conjugation():
    with pytest.raises(ValidationError, match="grammar"):
        validate(candidate(), target(), learned("je", "le", "train"), dictionary())


def test_unknown_context_is_rejected():
    with pytest.raises(ValidationError, match="train"):
        validate(
            candidate(), target(), learned("je", "le", "regarde", "parle"), dictionary()
        )


def test_whole_word_has_visible_translation_and_no_definition_hint():
    valid = validate(
        candidate(),
        target(),
        learned("je", "le", "train", "regarde", "parle"),
        dictionary(),
    )
    assert valid.text == "Je {{c1::emprunte}} le train."
    assert valid.translation == "I borrow the train."
    assert "borrow" not in valid.text


def test_apostrophes_and_accents_are_preserved():
    assert normalize("L’autre") == "l'autre"
    assert tokens("J’ai déjà payé.") == ["j'", "ai", "déjà", "payé"]
    assert normalize("été") != normalize("ete")


def test_partial_word_cloze_never_establishes_a_whole_word():
    lex = dictionary()
    k = Knowledge()
    k.learn("prend", lex, 1, "stem", partial=True)
    assert not k.surfaces and not k.forms


def test_fitting_alternative_overrides_model_approval():
    from chinese_frontier.french.generation import validate_review

    c = candidate()
    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "fine",
        "search_summary": "checked verbs",
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": True,
                "fits_cloze": True,
                "reason": "also works",
            }
        ],
    }
    assert not validate_review(review, c)


def test_audit_requires_alternative_checks_for_every_example():
    from chinese_frontier.french.generation import validate_review

    with pytest.raises(ValidationError, match="alternatives"):
        validate_review(
            {
                "approved": True,
                "context_checks": context_checks(),
                "reason": "fine",
                "search_summary": "checked",
                "alternatives": [],
            },
            candidate(),
        )


@pytest.fixture
def collection(tmp_path):
    from anki.collection import Collection

    col = Collection(str(tmp_path / "test.anki2"))
    yield col
    col.close()


def add_cloze(col, text, *, learned=False, suspended=False):
    model = col.models.by_name("Cloze")
    note = col.new_note(model)
    note["Text"] = text
    col.add_note(note, 1)
    for card in note.cards():
        if learned:
            card.queue = card.type = 2
            card.reps = 2
            card.due = col.sched.today + 100
            col.update_card(card)
            for days in (2, 3):
                stamp = (
                    col.sched.day_cutoff - days * 86400 + 3600
                ) * 1000 + card.id % 1000
                col.db.execute(
                    "insert into revlog values (?,?,?,?,?,?,?,?,?)",
                    stamp,
                    card.id,
                    0,
                    3,
                    1,
                    1,
                    2500,
                    1000,
                    1,
                )
        if suspended:
            col.sched.suspend_cards([card.id])
    return note


def test_snapshot_only_establishes_live_tested_ordinals(collection):
    from chinese_frontier.french.collection import snapshot

    note = add_cloze(collection, "{{c1::je}} {{c2::tu}} le train.", learned=True)
    collection.sched.suspend_cards([note.cards()[1].id])
    current = snapshot(collection, {"deck_id": 1}, {}, dictionary())
    assert set(current.known.surfaces) == {"je"}
    assert "le" not in current.known.surfaces


def test_partial_cloze_is_not_learning_evidence(collection):
    from chinese_frontier.french.collection import snapshot

    add_cloze(collection, "Je {{c1::prend}}s le train.", learned=True)
    assert not snapshot(collection, {"deck_id": 1}, {}, dictionary()).known.surfaces


def test_native_admission_leaves_all_existing_rows_and_history_intact(collection):
    from chinese_frontier.french.collection import SETTINGS, admit
    from chinese_frontier.french.generation import Draft, candidate_hash

    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS, settings)
    for word in ("je", "le", "train", "regarde", "parle"):
        add_cloze(collection, "{{c1::" + word + "}}", learned=True)
    add_cloze(collection, "Je {{c1::prends}} le train.", suspended=True)
    before = {
        table: collection.db.all("select * from " + table)
        for table in ("notes", "cards", "revlog", "notetypes")
    }
    c = candidate()
    t = target()
    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "Clear translation",
        "search_summary": "Checked alternatives",
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "rent is different from borrow",
            }
        ],
    }
    draft = Draft(
        t,
        c,
        {
            "policy": POLICY,
            "target_key": t.key,
            "fingerprint": candidate_hash(c),
            "review": review,
            "provider": "codex",
        },
    )
    result = admit(collection, settings, {}, [draft], dictionary())
    assert len(result.admitted) == 1
    for table, rows in before.items():
        after = {r[0]: r for r in collection.db.all("select * from " + table)}
        assert all(after[row[0]] == row for row in rows), table
    assert admit(collection, settings, {}, [draft], dictionary()).admitted == []
    note = collection.get_note(result.admitted[0])
    assert len(note.cards()) == 1
    question = note.cards()[0].question()
    assert c.translation in question and "Checked alternatives" not in question
    assert "Checked alternatives" not in note.cards()[0].answer()
    assert "Checked alternatives" in note["Disambiguation"]


def test_pending_new_card_reserves_the_only_slot(collection):
    from chinese_frontier.french.collection import snapshot

    conf = collection.decks.config_dict_for_deck_id(1)
    conf["new"]["perDay"] = 1
    collection.decks.update_config(conf)
    add_cloze(collection, "{{c1::train}}")
    assert snapshot(collection, {"deck_id": 1}, {}, dictionary()).slots == 0


def test_paused_settings_never_admit(collection):
    from chinese_frontier.french.collection import SETTINGS, admit

    collection.set_config(SETTINGS, {"enabled": False, "deck_id": 1})
    result = admit(collection, {"enabled": True, "deck_id": 1}, {}, [], dictionary())
    assert not result.admitted


def test_edit_cloze_filters_still_establish_tested_words(collection):
    from chinese_frontier.french.collection import snapshot

    model = collection.models.by_name("Cloze")
    model["tmpls"][0]["qfmt"] = "{{edit:cloze:Text}}"
    collection.models.update_dict(model)
    add_cloze(collection, "{{c1::train}}", learned=True)
    assert (
        "train" in snapshot(collection, {"deck_id": 1}, {}, dictionary()).known.surfaces
    )


def test_subject_resolves_person_without_establishing_other_persons(collection):
    from chinese_frontier.french.collection import snapshot

    lex = dictionary()
    lex = Lexicon(
        [f for fs in lex.forms.values() for f in fs]
        + [Form("prends", "prendre", "VER", "ind:pre:2:s")],
        [],
    )
    add_cloze(collection, "Je {{c1::prends}} le train.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert "prends|prendre|VER|ind:pre:1:s" in known.forms
    assert "prends|prendre|VER|ind:pre:2:s" not in known.forms


def test_same_person_with_multiple_tenses_is_not_generalized(collection):
    from chinese_frontier.french.collection import snapshot

    lex = Lexicon(
        [
            Form("finit", "finir", "VER", "ind:pre:3:s"),
            Form("finit", "finir", "VER", "ind:pas:3:s"),
        ],
        [],
    )
    add_cloze(collection, "Il {{c1::finit}}.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert "finit" in known.surfaces and not known.forms


def test_real_resource_morphology_splits_syncretic_codes():
    from chinese_frontier.french.lexicon import verb_features

    assert verb_features("ind:pre:1,ind:pre:2,imp:pre:2", "", "s") == [
        "ind:pre:1:s",
        "ind:pre:2:s",
        "imp:pre:2:s",
    ]
    assert verb_features("par:pas:", "f", "p") == ["par:pas:f:p"]


def test_repair_api_cannot_touch_a_legacy_note(collection):
    from chinese_frontier.french.collection import request_repair

    note = add_cloze(collection, "{{c1::train}}")
    before = collection.db.all("select * from notes")
    with pytest.raises(ValueError, match="generated"):
        request_repair(collection, int(note.id))
    assert collection.db.all("select * from notes") == before


def test_lefff_blocks_a_bogus_avoir_participle():
    from chinese_frontier.french.lexicon import lefff_verbs

    forms = lefff_verbs(
        "a\tv\tavoir\tP3s\neu\tv\tavoir\tKms\nemprunte\tv\temprunter\tPS13s"
    )
    assert ("a", "avoir", "ind:pre:3:s") in forms
    assert ("a", "avoir", "par:pas:m:s") not in forms
    assert ("eu", "avoir", "par:pas:m:s") in forms
    assert ("emprunte", "emprunter", "sub:pre:1:s") in forms


def test_lefff_supplies_verb_readings_lexique_leaves_incomplete():
    # Lexique 4.1 leaves 8_Nombre empty for many common forms, lists bogus
    # readings (vais as "inf") and misses syncretism (allions as sub:pre).
    header = "1_Mot\t3_Phono_IPA\t4_Lemme\t5_Cgram\t7_Genre\t8_Nombre\t9_InfoVER\t10_FreqMot\t12_FreqLemme"
    rows = [
        "vais\tvɛ\taller\tVER\t\t\tind:pre:1,inf\t300\t900",
        "allons\talɔ̃\taller\tVER\t\t\tind:pre:1,imp:pre:1,inf\t200\t900",
        "ira\tiʁa\taller\tVER\t\t\tind:fut:3\t50\t900",
        "allions\taljɔ̃\taller\tVER\t\tp\tind:imp:1\t5\t900",
        "allé\tale\taller\tVER\tm\ts\tpar:pas\t40\t900",
        "allé\tale\taller\tVER\tm\ts\tpar:pas\t1\t900",
    ] + [f"mot{i}\t\tmot{i}\tNOM\tm\ts\t\t1\t1" for i in range(10000)]
    lefff = "\n".join(
        [
            "vais\tv\taller\tP1s",
            "allons\tv\taller\tP1p",
            "allons\tv\taller\tY1p",
            "ira\tv\taller\tF3s",
            "allions\tv\taller\tI1p",
            "allions\tv\taller\tS1p",
            "allé\tv\taller\tKms",
        ]
        + [f"pad{i}\tv\tpad\tW" for i in range(10000)]
    )
    graded = "word\ttag\n" + "\n".join(f"mot{i}\tN" for i in range(1000))
    lex = Lexicon.parse("\n".join([header] + rows), graded, "", lefff)

    def readings(surface):
        return sorted(f.features for f in lex.analyses(surface))

    assert readings("vais") == ["ind:pre:1:s"]
    assert readings("allons") == ["imp:pre:1:p", "ind:pre:1:p"]
    assert readings("ira") == ["ind:fut:3:s"]
    assert readings("allions") == ["ind:imp:1:p", "sub:pre:1:p"]
    # Duplicate Lexique rows collapse into one form, keeping the larger frequency.
    assert [(f.features, f.frequency, f.ipa) for f in lex.analyses("allé")] == [
        ("par:pas:m:s", 40, "ale")
    ]
    assert lex.analyses("vais")[0].frequency == 300


def test_verb_features_without_lefff_infer_a_missing_number():
    from chinese_frontier.french.lexicon import verb_features

    assert verb_features("ind:pre:1,imp:pre:1", "", "") == [
        "ind:pre:1:s",
        "ind:pre:1:p",
        "imp:pre:1:s",
        "imp:pre:1:p",
    ]


@pytest.mark.parametrize("outdated_policy", [False, True])
def test_generated_repair_preserves_card_ids_and_scheduling(
    collection, outdated_policy
):
    from dataclasses import replace

    from chinese_frontier.french.collection import (
        SETTINGS,
        admit,
        request_repair,
        snapshot,
    )
    from chinese_frontier.french.generation import Draft, candidate_hash

    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS, settings)
    for word in ("je", "le", "train", "regarde", "parle"):
        add_cloze(collection, "{{c1::" + word + "}}", learned=True)
    c = candidate()
    t = target()
    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "clear",
        "search_summary": "Checked rent",
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "rent differs",
            }
        ],
    }
    d = Draft(
        t,
        c,
        {
            "policy": POLICY,
            "target_key": t.key,
            "fingerprint": candidate_hash(c),
            "review": review,
        },
    )
    nid = admit(collection, settings, {}, [d], dictionary()).admitted[0]
    before = collection.db.all("select * from cards where nid=?", nid)
    if outdated_policy:
        import html
        import json

        note = collection.get_note(nid)
        metadata = json.loads(html.unescape(note["Source"]))
        metadata["policy"] = POLICY - 1
        note["Source"] = html.escape(json.dumps(metadata))
        collection.update_note(note)
    else:
        request_repair(collection, nid)
    current = snapshot(collection, settings, {}, dictionary())
    repair = current.repairs[t.key]
    assert admit(
        collection,
        settings,
        {},
        [replace(d, repair_note=nid, old_fingerprint=repair[1])],
        dictionary(),
    ).admitted == [nid]
    assert collection.db.all("select * from cards where nid=?", nid) == before
    assert not snapshot(collection, settings, {}, dictionary()).repairs


def test_repair_does_not_overwrite_a_concurrent_edit(collection):
    from dataclasses import replace

    from chinese_frontier.french.collection import (
        SETTINGS,
        admit,
        request_repair,
        snapshot,
    )
    from chinese_frontier.french.generation import Draft, candidate_hash

    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS, settings)
    for word in ("je", "le", "train", "regarde", "parle"):
        add_cloze(collection, "{{c1::" + word + "}}", learned=True)
    c = candidate()
    t = target()
    r = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "clear",
        "search_summary": "checked",
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "different",
            }
        ],
    }
    d = Draft(
        t,
        c,
        {
            "policy": POLICY,
            "target_key": t.key,
            "fingerprint": candidate_hash(c),
            "review": r,
        },
    )
    nid = admit(collection, settings, {}, [d], dictionary()).admitted[0]
    request_repair(collection, nid)
    old = snapshot(collection, settings, {}, dictionary()).repairs[t.key][1]
    note = collection.get_note(nid)
    note["Meaning"] = "user edit"
    collection.update_note(note)
    result = admit(
        collection,
        settings,
        {},
        [replace(d, repair_note=nid, old_fingerprint=old)],
        dictionary(),
    )
    assert not result.admitted and collection.get_note(nid)["Meaning"] == "user edit"


def test_standalone_subject_establishes_present_not_subjunctive(collection):
    from chinese_frontier.french.collection import snapshot

    lex = Lexicon(
        [
            Form("parle", "parler", "VER", "ind:pre:1:s"),
            Form("parle", "parler", "VER", "sub:pre:1:s"),
        ],
        [],
    )
    add_cloze(collection, "Je {{c1::parle}}.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert "parle|parler|VER|ind:pre:1:s" in known.forms
    assert "parle|parler|VER|sub:pre:1:s" not in known.forms


def test_contrast_uses_one_card_and_labels_both_sides(collection):
    from chinese_frontier.french.collection import SETTINGS, admit
    from chinese_frontier.french.generation import Draft, candidate_hash

    lex = Lexicon(
        [
            Form("un", "un", "ART", "m:s"),
            Form("une", "un", "ART", "f:s"),
            Form("homme", "homme", "NOM", "m:s"),
            Form("femme", "femme", "NOM", "f:s"),
            Form("arrive", "arriver", "VER", "ind:pre:3:s"),
        ],
        [],
    )
    for word in ("un", "une", "femme", "arrive"):
        add_cloze(collection, "{{c1::" + word + "}}", learned=True)
    settings = {"enabled": True, "deck_id": 1}
    collection.set_config(SETTINGS, settings)
    other = {
        "sentence": "Une femme arrive.",
        "answer": "femme",
        "translation": "A woman arrives.",
        "meaning": "woman",
        "mode": "word",
        "cue": "",
        "uses": [
            {
                "surface": "arrive",
                "lemma": "arriver",
                "pos": "VER",
                "features": "ind:pre:3:s",
            }
        ],
        "grammar": ["ind:pre:3:s"],
        "constructions": [],
        "contrast": None,
        "explanation": "",
    }
    c = Candidate(
        "Un homme arrive.",
        "homme",
        "A man arrives.",
        "man",
        "contrast",
        "",
        tuple(other["uses"]),
        ("ind:pre:3:s",),
        (),
        other,
    )
    t = Target("homme", "NOM", "adult-male", "vocabulary", "man", 1, 10)
    alternatives = [
        {
            "example": i,
            "answer": "fille",
            "fits_context": False,
            "fits_cloze": True,
            "reason": "A girl does not match the man/woman translation.",
        }
        for i in (0, 1)
    ]
    audit = {
        "policy": POLICY,
        "target_key": t.key,
        "fingerprint": candidate_hash(c),
        "review": {
            "approved": True,
            "reason": "clear",
            "search_summary": "checked counterparts",
            "context_checks": context_checks("arrive", "arrive"),
            "alternatives": alternatives,
        },
    }
    result = admit(collection, settings, {}, [Draft(t, c, audit)], lex)
    assert len(result.admitted) == 1
    note = collection.get_note(result.admitted[0])
    cards = note.cards()
    assert len(cards) == 1
    assert (
        "Disambiguation" in cards[0].question()
        and "Disambiguation" in cards[0].answer()
    )
    assert (
        "A man arrives." in cards[0].question()
        and "A woman arrives." in cards[0].question()
    )
    assert "checked counterparts" not in cards[0].question()


def test_review_requires_a_target_binding():
    from chinese_frontier.french.generation import Draft, candidate_hash

    c = candidate()
    t = target()
    with pytest.raises(ValidationError, match="matching"):
        Draft(
            t,
            c,
            {
                "policy": POLICY,
                "target_key": "different",
                "fingerprint": candidate_hash(c),
                "review": {},
            },
        ).verify()


def test_deferred_targets_do_not_hide_the_rest_of_the_frontier(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets
    from chinese_frontier.french.lexicon import Entry

    entries = [Entry(f"mot{i}", "NOM", 1, 100 - i) for i in range(60)]
    lex = Lexicon([], entries)
    lex.definitions = lambda entry, folder: [
        {"sense": entry.lemma, "definition": "A source sense."}
    ]
    deferred = {
        Target(
            e.lemma, e.pos, e.lemma, "vocabulary", "A source sense.", 1, e.frequency
        ).key
        for e in entries[:55]
    }
    selected = choose_targets(
        lex, Knowledge(), set(), set(), tmp_path, 2, deferred, Event()
    )
    assert selected and selected[0].lemma == "mot55"


def test_contrast_selects_the_declared_verb_not_a_homograph():
    import json
    from dataclasses import asdict

    present = Form("est", "être", "VER", "ind:pre:3:s")
    past = Form("était", "être", "VER", "ind:imp:3:s")
    lex = Lexicon([Form("est", "est", "NOM", "m:s"), present, past], [])
    known = Knowledge({"il": [], "est": []}, {present.key}, {("être", "VER")})
    other = Candidate(
        "Il est ici.",
        "est",
        "He is here.",
        "is",
        "word",
        "être",
        (
            {
                "surface": "est",
                "lemma": "être",
                "pos": "VER",
                "features": present.features,
            },
        ),
        (),
        (),
    )
    known.surfaces["ici"] = []
    c = Candidate(
        "Il était ici.",
        "était",
        "He was here.",
        "was",
        "contrast",
        "être",
        (),
        (),
        (),
        json.loads(json.dumps(asdict(other))),
    )
    t = Target("être", "VER", "established", "conjugation", "be", 1, 10, past.key)
    assert "{{c1::est}}" in validate(c, t, known, lex).text


def test_early_stem_is_saved_for_the_final_fallback():
    import json
    from dataclasses import asdict
    from threading import Event

    from chinese_frontier.french.generation import candidate_hash, generate

    whole = candidate()
    stem = Candidate(**{**asdict(whole), "mode": "stem", "explanation": "empr"})
    t = target()

    class Client:
        count = 0

        def check_login(self):
            pass

        def request(self, prompt, schema):
            self.count += 1
            if self.count in (1, 3):
                c = whole if self.count == 1 else stem
                return json.loads(
                    json.dumps(
                        {"proposals": [{"key": t.key, "candidates": [asdict(c)]}]}
                    )
                )
            c = whole if self.count == 2 else stem
            return {
                "reviews": [
                    {
                        "key": t.key,
                        "fingerprint": candidate_hash(c),
                        "approved": self.count == 4,
                        "context_checks": context_checks(),
                        "reason": "checked",
                        "search_summary": "checked takes versus borrows",
                        "alternatives": [
                            {
                                "example": 0,
                                "answer": "prends",
                                "fits_context": True,
                                "fits_cloze": self.count == 2,
                                "reason": "The competing verb does not match the visible prefix.",
                            }
                        ],
                    }
                ]
            }

    drafts, failures = generate(
        [t],
        learned("je", "le", "train", "regarde", "parle"),
        dictionary(),
        {},
        Event(),
        Client(),
    )
    assert not failures
    assert len(drafts) == 1 and drafts[0].candidate.mode == "stem"


def test_automatic_refill_continues_after_the_old_daily_cap(monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from chinese_frontier.french import runtime

    scheduled = []
    monkeypatch.setattr(
        runtime,
        "mw",
        SimpleNamespace(
            taskman=SimpleNamespace(
                run_in_background=lambda work, done, **kwargs: scheduled.append(
                    (work, done)
                )
            )
        ),
    )
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save = lambda: None
    controller.state = {"day": 1, "batches": 8}
    controller.busy = True
    current = SimpleNamespace(
        day=1,
        slots=3,
        repairs={},
        known=SimpleNamespace(version="known", surfaces={"je": []}),
    )
    controller.scanned(
        current, {}, {"auto_fill": True, "max_batches_per_day": 8}, None, Event(), 0
    )
    assert len(scheduled) == 1
    assert controller.state["batches"] == 9


def test_refill_budget_adapts_to_approval_yield():
    from chinese_frontier.refill import refill_ready

    high = {"day": 1, "attempted": 100, "approved": 80}
    low = {"day": 1, "attempted": 100, "approved": 5}
    assert refill_ready(high, 1, 10, 0, {}, 100)
    assert refill_ready(low, 1, 10, 0, {}, 100)
    assert low["cycle_limit"] > high["cycle_limit"]
    assert low["cycle_limit"] == 32


def test_refill_retries_automatically_on_the_same_day_after_cooldown():
    import json

    from chinese_frontier.refill import refill_ready

    state = {"day": 1, "batches": 32, "cycle_batches": 4, "cycle_limit": 4}
    config = {"refill_retry_seconds": 60}
    assert not refill_ready(state, 1, 3, 0, config, 100)
    state = json.loads(json.dumps(state))  # Profile reopen keeps the cooldown.
    assert not refill_ready(state, 1, 3, 0, config, 159)
    assert refill_ready(state, 1, 3, 0, config, 160)
    assert state["batches"] == 32
    assert state["cycle_batches"] == 0 and state["refill_at"] == 0


def test_refill_budget_shrinks_when_fewer_slots_need_filling():
    from chinese_frontier.refill import refill_ready

    one = {"day": 1, "attempted": 100, "approved": 80}
    ten = dict(one)
    assert refill_ready(one, 1, 1, 0, {}, 100)
    assert refill_ready(ten, 1, 10, 0, {}, 100)
    assert one["cycle_limit"] < ten["cycle_limit"]


def test_no_slots_or_repairs_never_requests_a_refill():
    from chinese_frontier.refill import refill_ready

    state = {"day": 1, "batches": 8}
    assert not refill_ready(state, 1, 0, 0, {}, 100)
    assert "cycle_limit" not in state


def test_new_anki_day_clears_refill_cooldown_and_yield_statistics():
    from chinese_frontier.refill import refill_ready

    state = {
        "day": 1,
        "batches": 32,
        "attempted": 100,
        "approved": 1,
        "cycle_batches": 32,
        "cycle_limit": 32,
        "refill_at": 1000,
    }
    assert refill_ready(state, 2, 3, 0, {}, 100)
    assert state["batches"] == state["attempted"] == state["approved"] == 0
    assert state["refill_at"] == 0


def test_no_eligible_targets_do_not_consume_model_batches_or_spin():
    from chinese_frontier.refill import record_batch, refill_ready, reserve_batch

    state = {"day": 1, "batches": 8}
    assert refill_ready(state, 1, 3, 0, {}, 100)
    reserve_batch(state)
    record_batch(state, 0, 0, {}, 100)
    assert state["batches"] == 8 and state["cycle_batches"] == 0
    assert not refill_ready(state, 1, 3, 0, {}, 101)


def test_explicit_daily_budget_is_respected_when_auto_fill_is_disabled():
    from chinese_frontier.refill import refill_ready

    state = {"day": 1, "batches": 8}
    assert not refill_ready(
        state, 1, 3, 0, {"auto_fill": False, "max_batches_per_day": 8}, 100
    )


def test_a_later_timeout_keeps_earlier_independent_approvals():
    import json
    from dataclasses import asdict
    from threading import Event

    from chinese_frontier.french.generation import candidate_hash, generate

    c, t = candidate(), target()
    other = Target("chaise", "NOM", "chair", "vocabulary", "chair", 1, 10)

    class Client:
        calls = 0

        def check_login(self):
            pass

        def request(self, prompt, schema):
            self.calls += 1
            if self.calls == 1:
                return json.loads(
                    json.dumps(
                        {"proposals": [{"key": t.key, "candidates": [asdict(c)]}]}
                    )
                )
            if self.calls == 2:
                return {
                    "reviews": [
                        {
                            "key": t.key,
                            "fingerprint": candidate_hash(c),
                            "approved": True,
                            "context_checks": context_checks(),
                            "reason": "checked",
                            "search_summary": "checked alternatives",
                            "alternatives": [
                                {
                                    "example": 0,
                                    "answer": "prends",
                                    "fits_context": False,
                                    "fits_cloze": False,
                                    "reason": "different meaning",
                                }
                            ],
                        }
                    ]
                }
            raise TimeoutError("codex generation timed out")

    saved = []
    events = []
    with pytest.raises(TimeoutError) as error:
        generate(
            [t, other],
            learned("je", "le", "train", "regarde", "parle"),
            dictionary(),
            {},
            Event(),
            Client(),
            on_approved=saved.extend,
            progress=events.append,
        )
    assert len(getattr(error.value, "drafts", [])) == 1
    error.value.drafts[0].verify()
    assert len(saved) == 1
    assert events[-1]["finished"] and "timed out" in events[-1]["error"]
    assert events[-1]["target_count"] == 1
    assert events[-1]["targets"][0]["key"] == other.key
    assert events[-1]["elapsed_seconds"] >= 0


def test_independent_review_omits_irrelevant_vocabulary():
    import json
    from dataclasses import asdict
    from threading import Event

    from chinese_frontier.french.generation import candidate_hash, generate

    c, t = candidate(), target()
    known = learned("je", "le", "train", "regarde", "parle")
    known.surfaces["unrelatedword"] = [{"card": 999, "context": "An unrelated source."}]
    prompts = []

    class Client:
        def check_login(self):
            pass

        def request(self, prompt, schema):
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.loads(
                    json.dumps(
                        {"proposals": [{"key": t.key, "candidates": [asdict(c)]}]}
                    )
                )
            return {
                "reviews": [
                    {
                        "key": t.key,
                        "fingerprint": candidate_hash(c),
                        "approved": True,
                        "context_checks": context_checks(),
                        "reason": "checked",
                        "search_summary": "checked alternatives",
                        "alternatives": [
                            {
                                "example": 0,
                                "answer": "prends",
                                "fits_context": False,
                                "fits_cloze": False,
                                "reason": "different meaning",
                            }
                        ],
                    }
                ]
            }

    drafts, failures = generate([t], known, dictionary(), {}, Event(), Client())
    assert drafts and not failures
    assert "unrelatedword" not in prompts[1]
    assert '"train"' in prompts[1]


def test_reviewed_source_selection_is_relevant_and_does_not_invent_evidence():
    from chinese_frontier.french.generation import source_evidence

    records = [
        {"card": 1, "context": "Il est gentil."},
        {"card": 2, "context": "Le déjeuner est délicieux."},
    ]
    known = Knowledge(surfaces={"est": records})
    selected = source_evidence(
        known, {"est", "unknown"}, "Le déjeuner était délicieux.", 1
    )
    assert selected == {"est": [records[1]]}


def test_conjugation_review_receives_tested_meaning_evidence():
    from chinese_frontier.french.generation import verb_evidence

    lex = dictionary()
    known = learned("prends")
    t = Target(
        "prendre",
        "VER",
        "established",
        "conjugation",
        "take",
        1,
        10,
        "prenions|prendre|VER|ind:imp:1:p",
    )
    assert verb_evidence(t, known, lex) == {"prends": known.surfaces["prends"]}


def test_timeout_automatically_retries_soon_with_a_smaller_batch(monkeypatch):
    from types import SimpleNamespace

    from chinese_frontier.french import runtime

    scheduled = []
    monkeypatch.setattr(
        runtime,
        "mw",
        SimpleNamespace(
            progress=SimpleNamespace(
                single_shot=lambda delay, callback: scheduled.append((delay, callback))
            )
        ),
    )
    monkeypatch.setattr(runtime, "tooltip", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.time, "time", lambda: 100)
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save = lambda: None
    controller.state = {"active_batch_size": 3}
    controller.busy = True
    controller.failed(TimeoutError("codex generation timed out"), 0)
    assert not controller.busy
    assert controller.state["retry_after"] == 220
    assert controller.state["retry_batch_size"] == 1
    assert "Automatic retry" in controller.status
    assert scheduled[0][0] == 120000


def test_non_timeout_account_errors_keep_the_longer_backoff(monkeypatch):
    from chinese_frontier.french import runtime

    monkeypatch.setattr(runtime, "tooltip", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.time, "time", lambda: 100)
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save = lambda: None
    controller.state = {"active_batch_size": 3}
    controller.failed(RuntimeError("subscription unavailable"), 0)
    assert controller.state["retry_after"] == 1900
    assert "retry_batch_size" not in controller.state


def test_settings_status_shows_active_phase_elapsed_time_and_saved_work(monkeypatch):
    from chinese_frontier.french import runtime

    monkeypatch.setattr(runtime.time, "time", lambda: 115)
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.status = "Checking alternative answers (attempt 2/3)"
    controller.request_started_at = 100
    controller.busy = True
    controller.state = {"drafts": [{}]}
    text = controller.status_text()
    assert "Checking alternative answers" in text
    assert "15s elapsed" in text and "approved card" in text


def test_infinitive_complement_recognizes_reviewed_verb_despite_noun_homograph(
    collection,
):
    from chinese_frontier.french.collection import snapshot

    lex = Lexicon(
        [
            Form("doit", "devoir", "VER", "ind:pre:3:s"),
            Form("doit", "doit", "NOM", "m:s"),
            Form("être", "être", "VER", "inf"),
        ],
        [],
    )
    add_cloze(collection, "Ce document {{c1::doit}} être signé.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert "doit|devoir|VER|ind:pre:3:s" in known.forms
    assert ("devoir", "VER") in known.meanings
    assert ("doit", "NOM") not in known.meanings


def test_infinitive_complement_does_not_resolve_competing_verb_readings(collection):
    from chinese_frontier.french.collection import snapshot

    lex = Lexicon(
        [
            Form("vit", "vivre", "VER", "ind:pre:3:s"),
            Form("vit", "voir", "VER", "ind:pas:3:s"),
            Form("vit", "vit", "NOM", "m:s"),
            Form("venir", "venir", "VER", "inf"),
        ],
        [],
    )
    add_cloze(collection, "Cet homme {{c1::vit}} venir le train.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert not known.forms and not known.meanings


def test_noun_homograph_without_verb_context_remains_unresolved(collection):
    from chinese_frontier.french.collection import snapshot

    lex = Lexicon(
        [
            Form("doit", "devoir", "VER", "ind:pre:3:s"),
            Form("doit", "doit", "NOM", "m:s"),
        ],
        [],
    )
    add_cloze(collection, "Le {{c1::doit}}.", learned=True)
    known = snapshot(collection, {"deck_id": 1}, {}, lex).known
    assert not known.forms and not known.meanings


def test_unseen_live_target_is_covered_without_becoming_known_context(collection):
    from chinese_frontier.french.collection import snapshot

    add_cloze(collection, "Je {{c1::prends}} le train.")
    current = snapshot(collection, {"deck_id": 1}, {}, dictionary())
    assert ("prendre", "VER") in current.covered
    assert "prends|prendre|VER|ind:pre:1:s" in current.covered_forms
    assert not current.known.surfaces and not current.known.forms


def test_suspended_targets_do_not_establish_coverage(collection):
    from chinese_frontier.french.collection import snapshot

    add_cloze(collection, "Je {{c1::prends}} le train.", suspended=True)
    current = snapshot(collection, {"deck_id": 1}, {}, dictionary())
    assert not current.covered and not current.covered_forms


def test_selection_skips_covered_lemma_even_when_morphology_is_unresolved(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets
    from chinese_frontier.french.lexicon import Entry

    lex = Lexicon([], [Entry("homme", "NOM", 1, 100), Entry("enjeu", "NOM", 4, 10)])
    lex.definitions = lambda entry, folder: [
        {"sense": entry.lemma, "definition": "sense"}
    ]
    selected = choose_targets(
        lex,
        Knowledge(),
        set(),
        set(),
        tmp_path,
        1,
        set(),
        Event(),
        covered={("homme", "NOM")},
    )
    assert [t.lemma for t in selected] == ["enjeu"]


def test_selection_calibrates_to_upper_range_of_reviewed_content_words(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets
    from chinese_frontier.french.lexicon import Entry

    entries = [Entry(f"known{i}", "NOM", 4, 10) for i in range(30)]
    entries += [Entry("papa", "NOM", 1, 100), Entry("enjeu", "NOM", 4, 10)]
    lex = Lexicon([], entries)
    lex.definitions = lambda entry, folder: [
        {"sense": entry.lemma, "definition": "sense"}
    ]
    known = Knowledge(meanings={(f"known{i}", "NOM") for i in range(30)})
    selected = choose_targets(lex, known, set(), set(), tmp_path, 1, set(), Event())
    assert [t.lemma for t in selected] == ["enjeu"]


def test_covered_conjugation_is_not_reintroduced(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets

    f = Form("doit", "devoir", "VER", "ind:pre:3:s", frequency=100)
    lex = Lexicon([f], [])
    known = Knowledge(meanings={("devoir", "VER")})
    assert not choose_targets(
        lex, known, set(), set(), tmp_path, 1, set(), Event(), covered_forms={f.key}
    )


def test_intermediate_selection_does_not_fall_back_to_elementary_conjugations(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets

    lex = Lexicon(
        [
            Form("doit", "devoir", "VER", "ind:pre:3:s", frequency=100),
            Form("devrait", "devoir", "VER", "cnd:pre:3:s", frequency=10),
        ],
        [],
    )
    known = Knowledge(meanings={("devoir", "VER")})
    selected = choose_targets(
        lex, known, set(), set(), tmp_path, 1, set(), Event(), minimum_level=4
    )
    assert [t.form_key for t in selected] == ["devrait|devoir|VER|cnd:pre:3:s"]


def test_calibrated_selection_skips_very_common_words_in_high_source_levels(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets
    from chinese_frontier.french.lexicon import Entry

    entries = [Entry(f"known{i}", "NOM", 4, 20) for i in range(30)]
    entries += [Entry("parent", "NOM", 4, 100), Entry("contrainte", "NOM", 4, 15)]
    lex = Lexicon([], entries)
    lex.definitions = lambda entry, folder: [
        {"sense": entry.lemma, "definition": "sense"}
    ]
    known = Knowledge(meanings={(f"known{i}", "NOM") for i in range(30)})
    selected = choose_targets(lex, known, set(), set(), tmp_path, 1, set(), Event())
    assert [t.lemma for t in selected] == ["contrainte"]


def test_selection_uses_current_sense_instead_of_obsolete_first_result(tmp_path):
    from threading import Event

    from chinese_frontier.french.generation import choose_targets
    from chinese_frontier.french.lexicon import Entry

    lex = Lexicon([], [Entry("vaisseau", "NOM", 4, 10)])
    lex.definitions = lambda entry, folder: [
        {"sense": "obsolete", "definition": "(Vieilli) Vase."},
        {"sense": "current", "definition": "Navire."},
    ]
    selected = choose_targets(
        lex, Knowledge(), set(), set(), tmp_path, 1, set(), Event()
    )
    assert [t.sense for t in selected] == ["current"]


def test_target_ranker_cannot_introduce_a_target_outside_the_verified_pool():
    from threading import Event

    from chinese_frontier.french.generation import rank_targets

    class Client:
        def check_login(self):
            pass

        def request(self, prompt, schema):
            return {"selected": [{"key": "invented", "reason": "advanced"}]}

    with pytest.raises(ValidationError, match="supplied targets"):
        rank_targets([target()], 1, learned(), dictionary(), {}, Event(), Client())


def test_target_ranker_uses_reviewed_examples_and_can_choose_fewer_targets():
    from threading import Event

    from chinese_frontier.french.generation import rank_targets
    from chinese_frontier.french.lexicon import Entry

    t = target()
    lex = Lexicon([Form("preuve", "preuve", "NOM")], [Entry("preuve", "NOM", 4, 10)])
    known = Knowledge()
    known.learn("preuve", lex, 123, "Le juge demande une preuve.")

    class Client:
        def check_login(self):
            pass

        def request(self, prompt, schema):
            assert "Le juge demande une preuve." in prompt
            assert "longer example sentence" in prompt
            return {"selected": [{"key": t.key, "reason": "Useful new meaning."}]}

    assert rank_targets([t], 4, known, lex, {}, Event(), Client()) == [t]


def test_vocabulary_uses_declared_form_instead_of_first_dictionary_reading():
    from dataclasses import replace

    lex = dictionary()
    lex = Lexicon(
        [f for forms in lex.forms.values() for f in forms]
        + [
            Form("cherche", "chercher", "VER", "imp:pre:2:s"),
            Form("cherche", "chercher", "VER", "ind:pre:1:s"),
        ],
        [],
    )
    known = learned("je", "le", "train", "regarde", "parle")
    c = replace(
        candidate(),
        sentence="Je cherche le train.",
        answer="cherche",
        translation="I'm looking for the train.",
    )
    t = Target("chercher", "VER", "look-for", "vocabulary", "look for", 1, 10)
    valid = validate(c, t, known, lex)
    assert valid.form_key == "cherche|chercher|VER|ind:pre:1:s"


def test_vocabulary_target_form_is_separate_from_context_verb_form():
    from dataclasses import replace

    lex = Lexicon(
        [f for forms in dictionary().forms.values() for f in forms]
        + [
            Form("cherche", "chercher", "VER", "ind:pre:1:s"),
            Form("cherche", "chercher", "VER", "ind:pre:3:s"),
            Form("regarde", "regarder", "VER", "ind:pre:3:s"),
            Form("parle", "parler", "VER", "ind:pre:3:s"),
            Form("pense", "penser", "VER", "ind:pre:1:s"),
            Form("qu'", "que", "CON"),
            Form("il", "il", "PRO"),
        ],
        [],
    )
    known = learned("je", "le", "train", "regarde", "parle")
    for word in ("pense", "qu'", "il"):
        known.learn(word, lex, 1, word)
    for word, lemma in (("regarde", "regarder"), ("parle", "parler")):
        known.learn(
            word, lex, 2, "Il " + word, form_key=word + "|" + lemma + "|VER|ind:pre:3:s"
        )
    c = replace(
        candidate(),
        sentence="Je pense qu'il cherche le train.",
        answer="cherche",
        grammar=("ind:pre:1:s", "ind:pre:3:s"),
        uses=(
            {
                "surface": "pense",
                "lemma": "penser",
                "pos": "VER",
                "features": "ind:pre:1:s",
            },
        ),
    )
    t = Target("chercher", "VER", "look-for", "vocabulary", "look for", 1, 10)
    assert validate(c, t, known, lex).form_key == "cherche|chercher|VER|ind:pre:3:s"


def test_words_inside_a_live_phrase_cloze_are_covered_without_granting_context(
    collection,
):
    from chinese_frontier.french.collection import snapshot

    add_cloze(collection, "Je {{c1::prends le train}}.")
    current = snapshot(collection, {"deck_id": 1}, {}, dictionary())
    assert ("prendre", "VER") in current.covered and ("train", "NOM") in current.covered
    assert not current.known.surfaces


def test_vocabulary_answer_is_not_revealed_verbatim_by_english_translation():
    c = Candidate(
        "Je prends le train.",
        "train",
        "I take the train.",
        "train",
        "word",
        "",
        (
            {
                "surface": "prends",
                "lemma": "prendre",
                "pos": "VER",
                "features": "ind:pre:1:s",
            },
        ),
        ("ind:pre:1:s",),
        (),
    )
    t = Target("train", "NOM", "train", "vocabulary", "train", 1, 10)
    with pytest.raises(ValidationError, match="reveals the French answer"):
        validate(c, t, learned("je", "le", "prends"), dictionary())


def test_review_alternatives_must_be_concrete_cloze_substitutions():
    from chinese_frontier.french.generation import validate_review

    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "correct",
        "search_summary": "checked",
        "alternatives": [
            {
                "example": 0,
                "answer": "explique: indicative / subjunctive",
                "fits_context": True,
                "fits_cloze": True,
                "reason": "Context verb analysis.",
            }
        ],
    }
    with pytest.raises(ValidationError, match="concrete cloze substitution"):
        validate_review(review, candidate())


def test_legacy_context_commentary_cannot_replace_checked_cloze_alternatives():
    from chinese_frontier.french.generation import validate_review

    comment = {
        "example": 0,
        "answer": "context: indicative",
        "fits_context": True,
        "fits_cloze": False,
        "reason": "Not a cloze completion.",
    }
    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "correct",
        "search_summary": "checked",
        "alternatives": [comment],
    }
    with pytest.raises(ValidationError, match="Missing alternatives"):
        validate_review(review, candidate())
    review["alternatives"].append(
        {
            "example": 0,
            "answer": "loue",
            "fits_context": False,
            "fits_cloze": False,
            "reason": "rent is not borrow",
        }
    )
    assert validate_review(review, candidate())


def context_checks(*clues):
    return [
        {
            "example": i,
            "sufficient": True,
            "clues": [clue],
            "reason": "French situational clue.",
        }
        for i, clue in enumerate(clues or ("le train",))
    ]


def test_translation_and_prefix_cannot_override_insufficient_french_context():
    from chinese_frontier.french.generation import validate_review

    c = Candidate(
        "Il explique sa méfiance.",
        "méfiance",
        "He explains his distrust.",
        "distrust",
        "stem",
        "",
        (),
        (),
        (),
        explanation="mé",
    )
    review = {
        "approved": True,
        "reason": "Translation and prefix identify the answer.",
        "search_summary": "Checked défiance.",
        "context_checks": [
            {
                "example": 0,
                "sufficient": False,
                "clues": [],
                "reason": "Explains his could concern anything.",
            }
        ],
        "alternatives": [
            {
                "example": 0,
                "answer": "défiance",
                "fits_context": True,
                "fits_cloze": False,
                "reason": "Different prefix.",
            }
        ],
    }
    assert not validate_review(review, c)


def test_context_evidence_cannot_quote_the_hidden_answer_or_translation():
    from chinese_frontier.french.generation import validate_review

    review = {
        "approved": True,
        "reason": "clear",
        "search_summary": "checked",
        "context_checks": context_checks("borrow"),
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "Different meaning.",
            }
        ],
    }
    with pytest.raises(ValidationError, match="visible French"):
        validate_review(review, candidate())
    review["context_checks"] = context_checks("emprunte")
    with pytest.raises(ValidationError, match="visible French"):
        validate_review(review, candidate())


def test_french_context_view_removes_the_whole_target_in_stem_and_contrast_modes():
    from dataclasses import asdict, replace

    from chinese_frontier.french.generation import french_contexts

    stem = replace(candidate(), mode="stem", explanation="empr")
    assert french_contexts(stem) == ["Je […] le train."]
    contrast = replace(
        candidate(),
        mode="contrast",
        contrast=asdict(candidate("loue")) | {"sentence": "Je loue le train."},
    )
    assert french_contexts(contrast) == ["Je […] le train.", "Je […] le train."]


def test_every_contrast_example_requires_its_own_french_context_check():
    from dataclasses import asdict, replace

    from chinese_frontier.french.generation import validate_review

    c = replace(candidate(), mode="contrast", contrast=asdict(candidate()))
    review = {
        "approved": True,
        "reason": "clear",
        "search_summary": "checked",
        "context_checks": context_checks(),
        "alternatives": [
            {
                "example": i,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "Different meaning.",
            }
            for i in (0, 1)
        ],
    }
    with pytest.raises(ValidationError, match="Missing French-only"):
        validate_review(review, c)
    review["context_checks"] = context_checks("le train", "le train")
    review["context_checks"][1]["sufficient"] = False
    assert not validate_review(review, c)


def test_french_context_view_hides_capitalized_answers_without_matching_inside_words():
    from dataclasses import replace

    from chinese_frontier.french.generation import french_contexts

    c = replace(candidate(), sentence="Emprunte le train.")
    assert french_contexts(c) == ["[…] le train."]
    c = replace(candidate(), sentence="La contrainte du train.", answer="train")
    assert french_contexts(c) == ["La contrainte du […]."]


def test_repair_checkpoints_retain_unadmitted_vocabulary_and_replace_stale_duplicates():
    from dataclasses import replace

    from chinese_frontier.french.generation import Draft, merge_drafts

    queued = Draft(target(), candidate(), {})
    repair = Draft(
        Target("chaise", "NOM", "chair", "vocabulary", "chair", 1, 10),
        candidate(),
        {},
        repair_note=42,
    )
    updated = replace(repair, old_fingerprint="new fingerprint")
    assert merge_drafts([queued, repair], [updated]) == [queued, updated]
    assert merge_drafts([queued], []) == [queued]


def test_approved_french_draft_roundtrips_in_memory_without_a_disk_reload():
    from dataclasses import asdict, replace

    from chinese_frontier.french.generation import Draft, candidate_hash

    c = replace(
        candidate(),
        uses=(
            {
                "surface": "emprunte",
                "lemma": "emprunter",
                "pos": "VER",
                "features": "ind:pre:1:s",
            },
        ),
    )
    t = target()
    review = {
        "approved": True,
        "reason": "clear",
        "search_summary": "checked",
        "context_checks": context_checks(),
        "alternatives": [
            {
                "example": 0,
                "answer": "loue",
                "fits_context": False,
                "fits_cloze": True,
                "reason": "Different meaning.",
            }
        ],
    }
    draft = Draft(
        t,
        c,
        {
            "policy": POLICY,
            "target_key": t.key,
            "fingerprint": candidate_hash(c),
            "review": review,
        },
        repair_note=42,
        old_fingerprint="unchanged",
    )
    draft.verify()
    restored = Draft.read(draft.as_dict())
    restored.verify()
    assert restored == draft
    assert restored.candidate.uses == c.uses
    assert asdict(restored.target) == asdict(t)


def test_cached_french_draft_does_not_abort_a_no_capacity_scan(monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from chinese_frontier.french import runtime
    from chinese_frontier.french.generation import Draft

    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.state = {
        "day": 1,
        "drafts": [Draft(target(), candidate(), {}).as_dict()],
    }
    controller.busy = True
    controller.save = lambda: None
    controller.failed = lambda error, epoch: pytest.fail(str(error))
    monkeypatch.setattr(runtime, "mw", SimpleNamespace())
    current = SimpleNamespace(
        day=1, slots=0, repairs={}, known=SimpleNamespace(version="v1")
    )
    controller.scanned(current, {}, {}, None, Event(), 0)
    assert not controller.busy
    assert "No French admission slots" in controller.status


@pytest.mark.parametrize("field", ["uses", "grammar", "constructions"])
def test_malformed_model_annotations_remain_rejected(field):
    import json
    from dataclasses import asdict

    raw = json.loads(json.dumps(asdict(candidate())))
    raw[field] = "not an annotation array"
    with pytest.raises(ValidationError, match="invalid annotations"):
        Candidate.read(raw)


def test_context_annotation_reuses_only_proven_readings_and_leaves_claims_to_validation():
    from dataclasses import replace

    from chinese_frontier.french.generation import annotate_context

    lex = dictionary()
    lex = Lexicon(
        [f for forms in lex.forms.values() for f in forms]
        + [
            Form("pages", "page", "NOM", "f:p"),
            Form("pages", "pager", "VER", "ind:pre:2:s"),
        ],
        [],
    )
    known = learned("je", "regarde", "parle")
    known.learn("pages", lex, 1, "Deux cents pages.", form_key="pages|page|NOM|f:p")
    c = replace(candidate(), sentence="Je emprunte 200 pages.")
    fixed = annotate_context(c, known, lex)
    assert fixed.uses == (
        {"surface": "pages", "lemma": "page", "pos": "NOM", "features": "f:p"},
    )
    validate(fixed, target(), known, lex)
    wrong = replace(
        c,
        uses=(
            {
                "surface": "pages",
                "lemma": "pager",
                "pos": "VER",
                "features": "ind:pre:2:s",
            },
        ),
    )
    with pytest.raises(ValidationError, match="Unlearned context verb"):
        validate(annotate_context(wrong, known, lex), target(), known, lex)


def test_french_writer_receives_proven_usage_and_previous_lessons():
    import json
    from threading import Event

    from chinese_frontier.french.generation import generate

    known = learned("je", "le", "train", "regarde", "parle")
    known.surfaces["train"] = [
        {"card": 99, "context": "Je prends le train. Railway travel."}
    ]
    for i in range(1000):
        known.surfaces[f"irrelevant{i}"] = [
            {"card": i, "context": "Unrelated example."}
        ]
    payloads = []

    class Client:
        def check_login(self):
            pass

        def request(self, prompt, schema):
            payloads.append(json.loads(prompt[prompt.index('{"targets"') :]))
            return {"proposals": []}

    drafts, failures = generate(
        [target()],
        known,
        dictionary(),
        {},
        Event(),
        Client(),
        lessons={target().key: {"reason": "Use railway context."}},
    )
    assert not drafts
    assert len(failures) == 1
    assert max(len(p["knowledge"]["established_context"]) for p in payloads) <= 160
    assert all(p["context_evidence"]["train"][0]["card"] == 99 for p in payloads)
    assert all(
        p["previous_lessons"][target().key]["reason"] == "Use railway context."
        for p in payloads
    )


def test_french_buffer_prepares_at_zero_capacity_and_stops_when_full(
    monkeypatch,
):
    from threading import Event
    from types import SimpleNamespace

    from chinese_frontier.french import runtime
    from chinese_frontier.french.generation import Draft

    scheduled = []
    monkeypatch.setattr(
        runtime,
        "mw",
        SimpleNamespace(
            taskman=SimpleNamespace(
                run_in_background=lambda work, done, **kwargs: scheduled.append(work)
            )
        ),
    )
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save = lambda: None
    controller.state = {"day": 1, "drafts": []}
    controller.folder = Path("/tmp")
    controller.busy = True
    known = learned("je")
    current = SimpleNamespace(
        day=1,
        slots=0,
        repairs={},
        known=known,
        represented=set(),
        keys=set(),
        covered=set(),
        covered_forms=set(),
    )
    controller.scanned(current, {}, {"buffer_notes": 1}, dictionary(), Event(), 0)
    assert len(scheduled) == 1 and current.slots == 0
    controller.state["drafts"] = [Draft(target(), candidate(), {}).as_dict()]
    scheduled.clear()
    controller.scanned(current, {}, {"buffer_notes": 1}, dictionary(), Event(), 0)
    assert not scheduled and not controller.busy


def test_context_annotation_never_guesses_between_two_proven_readings():
    from chinese_frontier.french.generation import annotate_context

    lex = Lexicon(
        [Form("est", "être", "VER", "ind:pre:3:s"), Form("est", "est", "NOM", "m:s")],
        [],
    )
    known = Knowledge()
    known.learn("est", lex, 1, "Il est ici.")
    c = Candidate("Il est ici.", "ici", "He is here.", "here", "word", "", (), (), ())
    assert annotate_context(c, known, lex).uses == ()


def test_french_empty_proposal_reason_guides_next_retrieval_and_is_persisted():
    import json
    from threading import Event

    from chinese_frontier.french.generation import generate

    known = learned("je")
    known.surfaces.update(
        {f"a{i}": [{"card": i, "context": "ordinary"}] for i in range(400)}
    )
    known.surfaces["paiement"] = [{"card": 999, "context": "payment"}]
    payloads = []

    class Client:
        def check_login(self):
            pass

        def request(self, prompt, schema):
            payloads.append(json.loads(prompt[prompt.index('{"targets"') :]))
            return {
                "proposals": [
                    {
                        "key": target().key,
                        "candidates": [],
                        "reason": "Need payment context.",
                    }
                ]
            }

    drafts, failures = generate([target()], known, dictionary(), {}, Event(), Client())
    assert not drafts
    assert "paiement" not in payloads[0]["knowledge"]["established_context"]
    assert "paiement" in payloads[1]["knowledge"]["established_context"]
    assert json.loads(failures[target().key])["reason"] == "Need payment context."


def test_review_listing_the_answer_itself_is_not_a_failed_review():
    # Claude often reports the intended answer among checked alternatives.
    from chinese_frontier.french.generation import validate_review

    def alternative(answer, reason):
        return {
            "example": 0,
            "answer": answer,
            "fits_context": answer == "emprunte",
            "fits_cloze": answer == "emprunte",
            "reason": reason,
        }

    review = {
        "approved": True,
        "context_checks": context_checks(),
        "reason": "correct",
        "search_summary": "checked",
        "alternatives": [
            alternative("emprunte", "The intended answer."),
            alternative("loue", "rent is not borrow"),
        ],
    }
    assert validate_review(review, candidate())
    review["alternatives"] = review["alternatives"][:1]
    with pytest.raises(ValidationError, match="Missing alternatives"):
        validate_review(review, candidate())


def test_runtime_errors_record_where_they_were_raised(monkeypatch):
    from chinese_frontier.french import runtime

    monkeypatch.setattr(runtime, "tooltip", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "mw", None)
    controller = runtime.Controller.__new__(runtime.Controller)
    controller.valid = lambda epoch: True
    controller.save = lambda: None
    controller.state = {"day": 1}

    def explode():
        return [].items()

    try:
        explode()
    except AttributeError as exc:
        controller.failed(exc, 0)
    event = controller.state["telemetry"]["events"][-1]
    assert event["stage"] == "runtime"
    assert "explode" in event["where"]
    assert "test_french_frontier.py" in event["where"]
