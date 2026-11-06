# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""aqt.browser.table for the plugin host: what the browser hooks pass."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from anki.collection import BrowserColumns as Columns
from anki.collection import BrowserRow

Column = Columns.Column

ItemId = int
ItemList = Sequence[int]


@dataclass
class SearchContext:
    search: str
    browser: Any
    order: bool | str | Column = True
    reverse: bool = False
    addon_metadata: dict | None = None
    # if set, provided ids will be used instead of the regular search
    ids: Sequence[ItemId] | None = None


__all__ = ["BrowserRow", "Column", "Columns", "ItemId", "ItemList", "SearchContext"]
