# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import gzip
import re
import tempfile
import unicodedata
import urllib.request
from pathlib import Path

CEDICT_URL = (
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
)
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]+\Z")


def is_word(text: str) -> bool:
    return bool(HAN.fullmatch(text)) and len(text) <= 16


class Lexicon:
    """Conservative maximum matching against dictionary words, not characters.

    Segmentation can over-reject ambiguous sentences. Regeneration is preferable
    to silently accepting an unknown compound as familiar individual characters.
    """

    def __init__(self, words: set[str]) -> None:
        self.words = {word for word in words if is_word(word)}
        self.max_length = max(map(len, self.words), default=1)

    def segment(self, text: str, *, extra: set[str] | None = None) -> list[str]:
        words = self.words | (extra or set())
        max_length = max(self.max_length, max(map(len, extra or ()), default=1))
        result: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if (
                char.isspace()
                or unicodedata.category(char).startswith("P")
                or char.isdecimal()
            ):
                index += 1
                continue
            word = next(
                (
                    text[index : index + length]
                    for length in range(min(max_length, len(text) - index), 0, -1)
                    if text[index : index + length] in words
                ),
                char,
            )
            result.append(word)
            index += len(word)
        return result

    @classmethod
    def load(cls, folder: Path) -> Lexicon:
        """Fetch once, cache only headwords; never silently fall back to characters."""
        path = folder / "cedict-headwords.txt"
        if path.exists():
            lexicon = cls(set(path.read_text(encoding="utf-8").splitlines()))
            if len(lexicon.words) < 10000:
                raise ValueError(
                    "Cached dictionary is incomplete; remove cedict-headwords.txt to download it again"
                )
            return lexicon
        folder.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            CEDICT_URL, headers={"User-Agent": "Anki-Chinese-Frontier/1.0"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("CC-CEDICT download is unexpectedly large")
        text = gzip.decompress(data).decode("utf-8")
        words: set[str] = set()
        for line in text.splitlines():
            if line and not line.startswith("#"):
                words.update(word for word in line.split(" ", 2)[:2] if is_word(word))
        if len(words) < 10000:
            raise ValueError("CC-CEDICT download did not contain a complete dictionary")
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder, delete=False
        ) as stream:
            stream.write("# Derived from CC-CEDICT (MDBG), licensed CC BY-SA 4.0\n")
            stream.write("\n".join(sorted(words)))
            temporary = Path(stream.name)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return cls(words)
