# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.core import ValidationError  # noqa: E402
from chinese_frontier.french.core import (  # noqa: E402
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
            "policy": 1,
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


def test_generated_repair_preserves_card_ids_and_scheduling(collection):
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
            "policy": 1,
            "target_key": t.key,
            "fingerprint": candidate_hash(c),
            "review": review,
        },
    )
    nid = admit(collection, settings, {}, [d], dictionary()).admitted[0]
    before = collection.db.all("select * from cards where nid=?", nid)
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
            "policy": 1,
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
        "policy": 1,
        "target_key": t.key,
        "fingerprint": candidate_hash(c),
        "review": {
            "approved": True,
            "reason": "clear",
            "search_summary": "checked counterparts",
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
                "policy": 1,
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
