# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

EFFORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Codex default", ""),
    ("None", "none"),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("XHigh", "xhigh"),
)


def effort_options_with_legacy(effort: object) -> tuple[tuple[str, str], ...]:
    value = _effort_value(effort)
    if value and value not in _effort_values():
        return (*EFFORT_OPTIONS, (value, value))
    return EFFORT_OPTIONS


def effort_option_index(effort: object) -> int:
    value = _effort_value(effort)
    for index, (_label, option_value) in enumerate(effort_options_with_legacy(value)):
        if option_value == value:
            return index
    return 0


def effort_value(effort: object) -> str:
    return _effort_value(effort)


def _effort_value(effort: object) -> str:
    value = str(effort).strip()
    if value == "minimal":
        return ""
    return value


def _effort_values() -> set[str]:
    return {value for _label, value in EFFORT_OPTIONS}
