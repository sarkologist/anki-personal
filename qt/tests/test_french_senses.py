# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.french.lexicon import Entry, Lexicon  # noqa: E402
from chinese_frontier.french.senses import (  # noqa: E402
    kaikki_senses,
    wiktionary_senses,
)

WIKITEXT = """== {{langue|fr}} ==
=== {{S|nom|fr}} ===
'''enjeu''' {{pron|ɑ̃.ʒø|fr}} {{m}}
# {{lexique|jeux|fr}} [[somme|Somme]] [[miser|misée]] par le [[joueur]].
#* {{exemple | lang=fr | Chacun déposa son '''enjeu'''.}}
# {{figuré|fr}} Ce que l'on [[risquer|risque]] de {{lien|gagner|fr}}.
# {{vieilli|fr}} Ancien sens.
=== {{S|verbe|fr|flexion}} ===
# ''Troisième personne de'' [[enjeuer]].
== {{langue|en}} ==
=== {{S|nom|en}} ===
# English sense.
"""


def test_french_wiktionary_definitions_are_cleaned_and_filtered():
    senses = wiktionary_senses("enjeu", "NOM", WIKITEXT)
    assert [s["definition"] for s in senses] == [
        "(Jeux) Somme misée par le joueur.",
        "(Figuré) Ce que l'on risque de gagner.",
        "(Vieilli) Ancien sens.",
    ]
    assert len({s["sense"] for s in senses}) == 3
    assert wiktionary_senses("enjeu", "VER", WIKITEXT) == []


def test_kaikki_glosses_skip_forms_and_keep_archaic_labels():
    lines = [
        {
            "pos": "noun",
            "senses": [
                {"glosses": ["stake (what is at stake)"]},
                {"glosses": ["wager"], "tags": ["obsolete"]},
                {"glosses": ["plural of enjeu"], "form_of": [{"word": "enjeu"}]},
            ],
        },
        {"pos": "verb", "senses": [{"glosses": ["to bet"]}]},
    ]
    text = "\n".join(json.dumps(line) for line in lines)
    senses = kaikki_senses("enjeu", "NOM", text)
    assert [s["definition"] for s in senses] == [
        "stake (what is at stake)",
        "(obsolète) wager",
    ]
    assert len({s["sense"] for s in senses}) == 2


def lexicon_with(*sources):
    lex = Lexicon([], [Entry("enjeu", "NOM", 1, 10)])
    lex.sense_sources = list(sources)
    return lex, lex.entries[0]


def test_definitions_fall_back_when_the_primary_dictionary_is_down(tmp_path):
    calls = []

    def down(entry):
        calls.append("dbnary")
        raise OSError("Connection reset by peer")

    def fallback(entry):
        calls.append("wiktionary")
        return [{"sense": "w1", "definition": "stake"}]

    lex, entry = lexicon_with(("dbnary", down), ("wiktionary", fallback))
    assert lex.definitions(entry, tmp_path) == [{"sense": "w1", "definition": "stake"}]
    assert lex.has_definitions(entry, tmp_path)
    other = Entry("atout", "NOM", 1, 10)
    lex.definitions(other, tmp_path)
    # A failed source is not retried for every word in the batch.
    assert calls == ["dbnary", "wiktionary", "wiktionary"]


def test_an_empty_answer_tries_the_next_dictionary(tmp_path):
    lex, entry = lexicon_with(
        ("dbnary", lambda e: []),
        ("wiktionary", lambda e: [{"sense": "w1", "definition": "stake"}]),
    )
    assert lex.definitions(entry, tmp_path)[0]["sense"] == "w1"


def test_no_result_is_cached_only_when_every_dictionary_answered(tmp_path):
    def down(entry):
        raise ValueError("<html>Service unavailable")

    lex, entry = lexicon_with(("dbnary", down), ("kaikki", down))
    with pytest.raises(OSError, match="Service unavailable"):
        lex.definitions(entry, tmp_path)
    # A fallback that answers "no such word" is not an outage, but the
    # primary may still know the word, so the empty answer is not cached.
    lex, entry = lexicon_with(("dbnary", down), ("kaikki", lambda e: []))
    assert lex.definitions(entry, tmp_path) == []
    assert not lex.has_definitions(entry, tmp_path)
    lex, entry = lexicon_with(("dbnary", lambda e: []), ("kaikki", lambda e: []))
    assert lex.definitions(entry, tmp_path) == []
    assert lex.has_definitions(entry, tmp_path)
