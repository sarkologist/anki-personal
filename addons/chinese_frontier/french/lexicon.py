# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import senses

FLELEX = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_CRF.csv"
LEVELS = "https://cental.uclouvain.be/cefrlex/static/resources/fr/FleLex_TT_Beacco.tsv"
LEXIQUE = "https://www.lexique.org/databases/Lexique410/Lexique4.1.zip"
LETTERS = r"[a-zàâäçéèêëîïôöùûüÿœæ]"
TOKEN = re.compile(LETTERS + r"+(?:-" + LETTERS + r"+)*\'?|\d+", re.I)

SOURCE_RETRY_SECONDS = 1800
# Wikimedia's User-Agent policy asks for a way to identify the client.
USER_AGENT = "Anki-French-Frontier/1.0 (https://github.com/sarkologist/anki-personal)"
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
            # Lexique often leaves the number empty; without Lefff, keep both.
            for n in [number] if number in ("s", "p") else ["s", "p"]:
                result.append(base + ":" + n)
    return result


def lefff_verbs(text: str) -> set[tuple[str, str, str]]:
    return {
        (surface, lemma, f)
        for (surface, lemma), features in lefff_readings(text).items()
        for f in features
    }


def lefff_readings(text: str) -> dict[tuple[str, str], list[str]]:
    readings: dict[tuple[str, str], list[str]] = {}
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
        elif code == "K":
            # Invariable participles (été, pu, fallu) carry no agreement.
            features = ["par:pas:m:s"]
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
        known = readings.setdefault((normalize(surface), normalize(lemma)), [])
        known.extend(f for f in features if f not in known)
    return readings


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
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise senses.TooLarge("French resource exceeds download size limit")
    return data


def retry_delay(error: Exception) -> float:
    if getattr(error, "code", None) == 429:
        # Rate limited, not down: honour Retry-After rather than 30 minutes.
        headers = getattr(error, "headers", None) or {}
        try:
            return max(1.0, min(300.0, float(headers.get("Retry-After", 10))))
        except (TypeError, ValueError):
            return 10.0
    return SOURCE_RETRY_SECONDS


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
        self.sources_down: dict[str, float] = {}
        self.last_request: dict[str, float] = {}
        # Uncached answers, remembered for this session so batches don't
        # re-download them while a better dictionary is unavailable.
        self.recent: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self.sense_sources = [
            senses.SenseSource(
                "dbnary", lambda e: senses.dbnary(e.lemma, e.pos, fetch)
            ),
            senses.SenseSource(
                "wiktionary",
                lambda e: senses.wiktionary(e.lemma, e.pos, fetch),
                interval=1.0,
            ),
            senses.SenseSource(
                "kaikki",
                lambda e: senses.kaikki(e.lemma, e.pos, fetch),
                equivalent=False,
            ),
        ]

    def analyses(self, surface: str) -> list[Form]:
        return self.forms.get(normalize(surface), [])

    @classmethod
    def parse(
        cls, lexical: str, graded: str, levels: str, morphological: str = ""
    ) -> Lexicon:
        forms = []
        freq: dict[tuple[str, str], float] = {}
        readings = lefff_readings(morphological) if morphological else None
        if readings is not None and sum(map(len, readings.values())) < 10000:
            raise ValueError("Incomplete Lefff morphology resource")
        # Lexique's verb readings are incomplete (empty numbers, missing
        # syncretism), so with Lefff it only supplies IPA and frequency.
        verbs: dict[tuple[str, str], tuple[str, float]] = {}
        for row in csv.DictReader(io.StringIO(lexical), delimiter="\t"):
            surface, lemma, cgram = (
                normalize(row.get(k, "")) for k in ("1_Mot", "4_Lemme", "5_Cgram")
            )
            # Closed classes carry subtypes (ART:def, PRO:per, ADJ:pos); the
            # subtype is dropped so annotations name only the part of speech.
            pos = cgram.split(":")[0].upper()
            pos = "VER" if pos == "AUX" else pos
            if (
                not tokens(surface)
                or not tokens(lemma)
                or pos
                not in ("VER", "NOM", "ADJ", "ADV", "ART", "PRO", "PRE", "CON", "ONO")
            ):
                continue
            frequency = float(row.get("12_FreqLemme") or 0)
            freq[lemma, pos] = max(frequency, freq.get((lemma, pos), 0))
            info = row.get("9_InfoVER", "").strip(";")
            if pos == "VER" and readings is not None:
                ipa, frequency = verbs.get((surface, lemma), ("", 0))
                verbs[surface, lemma] = (
                    ipa or row.get("3_Phono_IPA", ""),
                    max(frequency, float(row.get("10_FreqMot") or 0)),
                )
                continue
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
        for (surface, lemma), (ipa, frequency) in verbs.items():
            for feature in (readings or {}).get((surface, lemma), []):
                forms.append(Form(surface, lemma, "VER", feature, ipa, frequency))
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
        path = folder / "lexicon-v4.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf8"))
            if data.get("version") != 4 or len(data.get("forms", [])) < 10000:
                raise ValueError("Invalid French dictionary cache")
            lexicon = cls(
                [Form(**f) for f in data["forms"]],
                [Entry(**e) for e in data["entries"]],
            )
            try:
                lexicon.paradigms = load_paradigms(folder, lexicon)
            except (OSError, ValueError):
                pass  # Tables are display-only; never block generation on them.
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
                "version": 4,
                "forms": [asdict(f) for items in lexicon.forms.values() for f in items],
                "entries": [asdict(e) for e in lexicon.entries],
                "sources": [LEXIQUE, FLELEX, LEVELS, LEFFF],
            },
        )
        return lexicon

    def _senses_path(self, entry: Entry, folder: Path) -> Path:
        key = hashlib.sha256((entry.lemma + "|" + entry.pos).encode()).hexdigest()
        return folder / "senses" / (key + ".json")

    def has_definitions(self, entry: Entry, folder: Path) -> bool:
        return self._senses_path(entry, folder).exists()

    def definitions(self, entry: Entry, folder: Path) -> list[dict[str, str]]:
        path = self._senses_path(entry, folder)
        if path.exists():
            return json.loads(path.read_text(encoding="utf8"))
        expires, remembered = self.recent.get(path.name, (0.0, []))
        if time.monotonic() < expires:
            return remembered
        failure: Exception | None = None
        answered = equivalent_empty = equivalent_failed = other_failed = False
        for source in self.sense_sources:
            now = time.monotonic()
            if now < self.sources_down.get(source.name, 0):
                exc: Exception = OSError(f"{source.name} dictionary recently failed")
            else:
                wait = self.last_request.get(source.name, -1e9) + source.interval - now
                if wait > 0:
                    time.sleep(wait)
                self.last_request[source.name] = time.monotonic()
                try:
                    results = source.lookup(entry)
                except (
                    OSError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    senses.TooLarge,
                ) as error:
                    # Outages also answer with HTML or truncated JSON.
                    self.sources_down[source.name] = now + retry_delay(error)
                    exc = error
                else:
                    answered = True
                    if results:
                        # Glosses from a lesser source are not kept forever
                        # while an equivalent French source was unavailable.
                        if source.equivalent or not equivalent_failed:
                            atomic_json(path, results)
                        else:
                            self.remember(path, results)
                        return results
                    equivalent_empty |= source.equivalent
                    continue
            failure = failure or exc
            if source.equivalent:
                equivalent_failed = True
            else:
                other_failed = True
        if not answered:
            raise OSError(f"No sense dictionary answered: {failure}") from failure
        if not failure or (equivalent_empty and not other_failed):
            atomic_json(path, [])
        else:
            # A source that was down may still know the word.
            self.remember(path, [])
        return []

    def remember(self, path: Path, results: list[dict[str, str]]) -> None:
        self.recent[path.name] = (time.monotonic() + SOURCE_RETRY_SECONDS, results)
