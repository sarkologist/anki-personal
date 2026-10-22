# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Dictionary senses for target selection, tried in order of preference.

DBnary and fr.wiktionary carry the same French definitions; kaikki.org is an
English-language extract of en.wiktionary. Senses only inform model prompts,
so a fallback in another language is acceptable when the others are down.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable

Sense = dict[str, str]
Fetch = Callable[[str, int], bytes]

DBNARY = "https://kaiko.getalp.org/sparql"
WIKTIONARY = "https://fr.wiktionary.org/w/api.php"
KAIKKI = "https://kaikki.org/dictionary/French/meaning"

DBNARY_POS = {
    "VER": "verb",
    "NOM": "noun",
    "ADJ": "adjective",
    "ADV": "adverb",
    "PRE": "preposition",
    "CON": "conjunction",
    "PRO": "pronoun",
    "ART": "determiner",
}
WIKTIONARY_POS = {
    "VER": ("verbe",),
    "NOM": ("nom",),
    "ADJ": ("adjectif",),
    "ADV": ("adverbe",),
    "PRE": ("préposition",),
    "CON": ("conjonction",),
    "PRO": ("pronom",),
    "ART": ("article",),
}
KAIKKI_POS = {
    "VER": ("verb",),
    "NOM": ("noun",),
    "ADJ": ("adj",),
    "ADV": ("adv",),
    "PRE": ("prep",),
    "CON": ("conj",),
    "PRO": ("pron",),
    "ART": ("det", "article"),
}
# choose_targets drops senses whose text carries these French labels.
KAIKKI_LABELS = {"obsolete": "obsolète", "archaic": "archaïque", "dated": "vieilli"}


def _accept(definition: str) -> bool:
    return bool(definition) and len(definition) <= 1000


def dbnary(lemma: str, pos: str, fetch: Fetch) -> list[Sense]:
    literal = json.dumps(lemma, ensure_ascii=False)
    query = (
        """PREFIX ontolex: <http://www.w3.org/ns/lemon/ontolex#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?sense ?definition ?pos WHERE {
?entry ontolex:canonicalForm/ontolex:writtenRep """
        + literal
        + """@fr ; ontolex:sense ?sense .
FILTER(STRSTARTS(STR(?entry), "http://kaiko.getalp.org/dbnary/fra/"))
?sense skos:definition/rdf:value ?definition .
OPTIONAL { ?entry <http://www.lexinfo.net/ontology/2.0/lexinfo#partOfSpeech> ?pos }
} LIMIT 24"""
    )
    data = json.loads(
        fetch(
            DBNARY
            + "?"
            + urllib.parse.urlencode(
                {"query": query, "format": "application/sparql-results+json"}
            ),
            1024 * 1024,
        )
    )
    expected = DBNARY_POS[pos]
    results = []
    for row in data["results"]["bindings"]:
        found = row.get("pos", {}).get("value", "").rsplit("#", 1)[-1]
        if found and found != expected:
            continue
        definition = row["definition"]["value"].strip()
        if _accept(definition):
            results.append({"sense": row["sense"]["value"], "definition": definition})
    return results


TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
LINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]")


def _template(match: re.Match[str]) -> str:
    parts = [p.strip() for p in match.group(1).split("|")]
    name, args = parts[0], [p for p in parts[1:] if "=" not in p]
    if name in ("lien", "l", "w", "nom w pc") and args:
        return args[0]
    if name == "lexique" and args:
        return f"({args[0].capitalize()})"
    if all(a == "fr" for a in args):
        # Usage labels such as {{figuré|fr}} or {{vieilli|fr}}.
        return f"({name.capitalize()})"
    return ""


def clean_wikitext(text: str) -> str:
    previous = None
    while previous != text:
        previous, text = text, TEMPLATE.sub(_template, text)
    text = LINK.sub(r"\1", text).replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", text).strip()


def wiktionary_senses(lemma: str, pos: str, wikitext: str) -> list[Sense]:
    wanted = WIKTIONARY_POS[pos]
    results = []
    french = section = False
    for line in wikitext.splitlines():
        if heading := re.match(r"^==\s*\{\{langue\|([^}|]+)", line):
            french = heading.group(1) == "fr"
            section = False
            continue
        if heading := re.match(r"^===+\s*\{\{S\|([^}]*)\}\}", line):
            parts = [p.strip() for p in heading.group(1).split("|")]
            section = (
                french
                and parts[0].startswith(wanted)
                and "fr" in parts[1:]
                and "flexion" not in parts
            )
            continue
        if section and re.match(r"^#(?![*:#])", line):
            definition = clean_wikitext(line[1:])
            if _accept(definition):
                results.append(
                    {
                        "sense": f"wiktionary:fr:{lemma}:{pos}:{len(results) + 1}",
                        "definition": definition,
                    }
                )
    return results


def wiktionary(lemma: str, pos: str, fetch: Fetch) -> list[Sense]:
    data = json.loads(
        fetch(
            WIKTIONARY
            + "?"
            + urllib.parse.urlencode(
                {
                    "action": "parse",
                    "page": lemma,
                    "prop": "wikitext",
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                }
            ),
            4 * 1024 * 1024,
        )
    )
    if "error" in data:
        if data["error"].get("code") == "missingtitle":
            return []
        raise ValueError(f"fr.wiktionary error: {data['error'].get('code')}")
    return wiktionary_senses(lemma, pos, data["parse"]["wikitext"])


def kaikki_senses(lemma: str, pos: str, text: str) -> list[Sense]:
    wanted = KAIKKI_POS[pos]
    results = []
    for line in text.splitlines():
        if not line.strip():
            continue
        word: dict[str, Any] = json.loads(line)
        if word.get("pos") not in wanted:
            continue
        for sense in word.get("senses", []):
            if sense.get("form_of") or sense.get("alt_of"):
                continue
            glosses = sense.get("glosses") or []
            labels = [
                KAIKKI_LABELS[t] for t in sense.get("tags", []) if t in KAIKKI_LABELS
            ]
            definition = "; ".join(glosses).strip()
            if labels:
                definition = f"({', '.join(labels)}) {definition}"
            if glosses and _accept(definition):
                results.append(
                    {
                        "sense": f"kaikki:{lemma}:{pos}:{len(results) + 1}",
                        "definition": definition,
                    }
                )
    return results


def kaikki(lemma: str, pos: str, fetch: Fetch) -> list[Sense]:
    parts = [lemma[:1], lemma[:2], lemma + ".jsonl"]
    url = KAIKKI + "/" + "/".join(urllib.parse.quote(p) for p in parts)
    try:
        data = fetch(url, 4 * 1024 * 1024)
    except OSError as exc:
        # kaikki has no page for words absent from en.wiktionary.
        if getattr(exc, "code", None) == 404:
            return []
        raise
    return kaikki_senses(lemma, pos, data.decode("utf8"))
