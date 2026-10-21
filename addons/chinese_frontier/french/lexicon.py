# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

FLELEX = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_CRF.csv"
LEVELS = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_TT_Beacco.tsv"
LEXIQUE = "https://www.lexique.org/databases/Lexique410/Lexique4.1.zip"
DICTIONARY = "https://kaiko.getalp.org/sparql"
LETTERS = r"[a-zàâäçéèêëîïôöùûüÿœæ]"
TOKEN = re.compile(LETTERS + r"+(?:-" + LETTERS + r"+)*\'?|\d+", re.I)

LEFFF = "https://raw.githubusercontent.com/ClaudeCoulombe/FrenchLefffLemmatizer/bc0ebd0135a6cc78f48ddf184069b4c0b9c017d8/french_lefff_lemmatizer/data/lefff-3.4.mlex"


def verb_features(info: str, gender: str, number: str) -> list[str]:
    result = []
    for item in re.split("[,;]", info.strip(";")):
        if not item:
            continue
        base = item.strip(":")
        if item.startswith(("inf", "par:pre")):
            result.append(base)
        elif item.startswith("par:pas"):
            for n in [number] if number in ("s", "p") else ["s", "p"]:
                result.append(":".join(filter(None, (base, gender, n))))
        else:
            result.append(base + ":" + number)
    return result


def lefff_verbs(text: str) -> set[tuple[str, str, str]]:
    allowed = set()
    moods = {
        "P": "ind:pre",
        "I": "ind:imp",
        "F": "ind:fut",
        "J": "ind:pas",
        "S": "sub:pre",
        "T": "sub:imp",
        "C": "cnd:pre",
        "Y": "imp:pre",
    }
    for line in text.splitlines():
        columns = line.split("\t")
        if len(columns) != 4 or columns[1] not in ("v", "auxAvoir", "auxEtre"):
            continue
        surface, _, lemma, code = columns
        if code == "W":
            features = ["inf"]
        elif code == "G":
            features = ["par:pre"]
        elif code.startswith("K"):
            features = [
                "par:pas:" + g + ":" + n
                for g in ("m", "f")
                if g in code
                for n in ("s", "p")
                if n in code or not any(c in code for c in "sp")
            ]
        else:
            features = [
                mood + ":" + person + ":" + number
                for tag, mood in moods.items()
                if tag in code
                for person in "123"
                if person in code
                for number in "sp"
                if number in code
            ]
        allowed.update((normalize(surface), normalize(lemma), f) for f in features)
    return allowed


def normalize(value: str) -> str:
    import unicodedata

    return " ".join(
        unicodedata.normalize("NFC", value).replace("’", "'").casefold().split()
    )


def tokens(value: str) -> list[str]:
    return TOKEN.findall(normalize(value))


@dataclass(frozen=True)
class Form:
    surface: str
    lemma: str
    pos: str
    features: str = ""
    ipa: str = ""
    frequency: float = 0

    @property
    def key(self) -> str:
        return "|".join((self.surface, self.lemma, self.pos, self.features))


@dataclass(frozen=True)
class Entry:
    lemma: str
    pos: str
    level: int
    frequency: float


def fetch(url: str, maximum: int = 80 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Anki-French-Frontier/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("French resource exceeds download size limit")
    return data


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
    temporary.replace(path)


def load_paradigms(
    folder: Path, lexicon: Lexicon, morphology: str = ""
) -> dict[str, dict[str, list[str]]]:
    path = folder / "paradigms-v1.json"
    if path.exists() and not morphology:
        data = json.loads(path.read_text(encoding="utf8"))
        if data.get("version") == 1:
            return data["paradigms"]
    verbs = {lemma for lemma, pos in lexicon.lemmas if pos == "VER"}
    paradigms: dict[str, dict[str, list[str]]] = {}
    for surface, lemma, features in sorted(
        lefff_verbs(morphology or fetch(LEFFF).decode("utf8"))
    ):
        if lemma in verbs:
            paradigms.setdefault(lemma, {}).setdefault(features, []).append(surface)
    # Lefff lists rare variants (e.g. "st" for "sont"); prefer attested spellings.
    attested = {(f.lemma, f.surface) for forms in lexicon.forms.values() for f in forms}
    for lemma, slots in paradigms.items():
        for features, surfaces in slots.items():
            if len(surfaces) > 1:
                slots[features] = [
                    s for s in surfaces if (lemma, s) in attested
                ] or surfaces
    atomic_json(path, {"version": 1, "paradigms": paradigms, "sources": [LEFFF]})
    return paradigms


class Lexicon:
    def __init__(
        self,
        forms: list[Form],
        entries: list[Entry],
        paradigms: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        # Complete Lefff verb paradigms (lemma -> features -> surfaces), used for
        # display only; Lexique omits or mislabels many inflections.
        self.paradigms = paradigms or {}
        self.forms: dict[str, list[Form]] = {}
        self.lemmas: dict[tuple[str, str], list[Form]] = {}
        for form in forms:
            self.forms.setdefault(form.surface, []).append(form)
            self.lemmas.setdefault((form.lemma, form.pos), []).append(form)
        self.entries = sorted(entries, key=lambda e: (e.level, -e.frequency, e.lemma))

    def analyses(self, surface: str) -> list[Form]:
        return self.forms.get(normalize(surface), [])

    @classmethod
    def parse(
        cls, lexical: str, graded: str, levels: str, morphological: str = ""
    ) -> Lexicon:
        forms = []
        freq: dict[tuple[str, str], float] = {}
        for row in csv.DictReader(io.StringIO(lexical), delimiter="\t"):
            surface, lemma, pos = (
                normalize(row.get(k, "")) for k in ("1_Mot", "4_Lemme", "5_Cgram")
            )
            if (
                not tokens(surface)
                or not tokens(lemma)
                or pos
                not in ("ver", "nom", "adj", "adv", "art", "pro", "pre", "con", "ono")
            ):
                continue
            pos = pos.upper()
            frequency = float(row.get("12_FreqLemme") or 0)
            freq[lemma, pos] = max(frequency, freq.get((lemma, pos), 0))
            info = row.get("9_InfoVER", "").strip(";")
            if pos == "VER":
                features = verb_features(
                    info, row.get("7_Genre", ""), row.get("8_Nombre", "")
                )
            else:
                features = [
                    ":".join(
                        filter(None, (row.get("7_Genre", ""), row.get("8_Nombre", "")))
                    )
                ]
            for feature in features:
                forms.append(
                    Form(
                        surface,
                        lemma,
                        pos,
                        feature,
                        row.get("3_Phono_IPA", ""),
                        float(row.get("10_FreqMot") or 0),
                    )
                )
        if morphological:
            allowed = lefff_verbs(morphological)
            if len(allowed) < 10000:
                raise ValueError("Incomplete Lefff morphology resource")
            forms = [
                f
                for f in forms
                if f.pos != "VER" or (f.surface, f.lemma, f.features) in allowed
            ]
        mapped = {
            "N": "NOM",
            "V": "VER",
            "A": "ADJ",
            "PREP": "PRE",
            "CONJ": "CON",
            "CONJS": "CON",
            "CONJC": "CON",
            "PRO": "PRO",
            "DET": "ART",
            "ADV": "ADV",
        }
        explicit = {}
        for row in csv.DictReader(io.StringIO(levels), delimiter="\t"):
            level = row.get("level", "")
            if level in ("A1", "A2", "B1", "B2", "C1", "C2"):
                explicit[normalize(row["word"]), row["tag"]] = (
                    "A1",
                    "A2",
                    "B1",
                    "B2",
                    "C1",
                    "C2",
                ).index(level) + 1
        entries = []
        seen = set()
        for row in csv.DictReader(io.StringIO(graded), delimiter="\t"):
            lemma, pos = normalize(row["word"]), mapped.get(row["tag"], "")
            if not pos or (lemma, pos) in seen or len(tokens(lemma)) > 5:
                continue
            # Keep only entries backed by morphology, including expressions supplied there.
            if (lemma, pos) not in freq:
                continue
            seen.add((lemma, pos))
            distribution = [
                float(row.get("freq_" + level) or 0)
                for level in ("a1", "a2", "b1", "b2", "c1", "c2")
            ]
            level = explicit.get(
                (lemma, pos),
                next((i + 1 for i, f in enumerate(distribution) if f > 0), 6),
            )
            entries.append(Entry(lemma, pos, level, freq[lemma, pos]))
        if len(forms) < 10000 or len(entries) < 1000:
            raise ValueError("Incomplete French vocabulary resources")
        return cls(forms, entries)

    @classmethod
    def load(cls, folder: Path) -> Lexicon:
        path = folder / "lexicon-v2.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf8"))
            if data.get("version") != 2 or len(data.get("forms", [])) < 10000:
                raise ValueError("Invalid French dictionary cache")
            lexicon = cls(
                [Form(**f) for f in data["forms"]],
                [Entry(**e) for e in data["entries"]],
            )
            lexicon.paradigms = load_paradigms(folder, lexicon)
            return lexicon
        archive = zipfile.ZipFile(io.BytesIO(fetch(LEXIQUE)))
        name = next(
            n
            for n in archive.namelist()
            if n.endswith(".tsv") and not n.startswith("__MACOSX")
        )
        if archive.getinfo(name).file_size > 100 * 1024 * 1024:
            raise ValueError("French lexical resource is too large")
        morphology = fetch(LEFFF).decode("utf8")
        lexicon = cls.parse(
            archive.read(name).decode("utf8"),
            fetch(FLELEX).decode("utf8"),
            fetch(LEVELS).decode("utf8"),
            morphology,
        )
        lexicon.paradigms = load_paradigms(folder, lexicon, morphology)
        atomic_json(
            path,
            {
                "version": 2,
                "forms": [asdict(f) for items in lexicon.forms.values() for f in items],
                "entries": [asdict(e) for e in lexicon.entries],
                "sources": [LEXIQUE, FLELEX, LEVELS, LEFFF],
            },
        )
        return lexicon

    def definitions(self, entry: Entry, folder: Path) -> list[dict[str, str]]:
        key = hashlib.sha256((entry.lemma + "|" + entry.pos).encode()).hexdigest()
        path = folder / "senses" / (key + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf8"))
        literal = json.dumps(entry.lemma, ensure_ascii=False)
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
                DICTIONARY
                + "?"
                + urllib.parse.urlencode(
                    {"query": query, "format": "application/sparql-results+json"}
                ),
                1024 * 1024,
            )
        )
        expected = {
            "VER": "verb",
            "NOM": "noun",
            "ADJ": "adjective",
            "ADV": "adverb",
            "PRE": "preposition",
            "CON": "conjunction",
            "PRO": "pronoun",
            "ART": "determiner",
        }[entry.pos]
        results = []
        for row in data["results"]["bindings"]:
            pos = row.get("pos", {}).get("value", "").rsplit("#", 1)[-1]
            if pos and pos != expected:
                continue
            definition = row["definition"]["value"].strip()
            if definition and len(definition) <= 1000:
                results.append(
                    {"sense": row["sense"]["value"], "definition": definition}
                )
        atomic_json(path, results)
        return results
