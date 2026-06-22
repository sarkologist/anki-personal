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

CLAUDE_EFFORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Claude default", ""),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("XHigh", "xhigh"),
    ("Max", "max"),
)


def effort_options_with_legacy(
    effort: object,
    options: tuple[tuple[str, str], ...] = EFFORT_OPTIONS,
) -> tuple[tuple[str, str], ...]:
    value = _effort_value(effort)
    known = {option_value for _label, option_value in options}
    if value and value not in known:
        return (*options, (value, value))
    return options


def effort_option_index(
    effort: object,
    options: tuple[tuple[str, str], ...] = EFFORT_OPTIONS,
) -> int:
    value = _effort_value(effort)
    for index, (_label, option_value) in enumerate(
        effort_options_with_legacy(value, options)
    ):
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
