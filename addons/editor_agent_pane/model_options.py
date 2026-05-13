# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Codex default", ""),
    ("gpt-5.5", "gpt-5.5"),
    ("gpt-5.4", "gpt-5.4"),
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
    ("gpt-5.2", "gpt-5.2"),
)


def model_options_with_legacy(model: object) -> tuple[tuple[str, str], ...]:
    value = _model_value(model)
    if value and value not in _model_values():
        return (*MODEL_OPTIONS, (value, value))
    return MODEL_OPTIONS


def model_option_index(model: object) -> int:
    value = _model_value(model)
    for index, (_label, option_value) in enumerate(model_options_with_legacy(value)):
        if option_value == value:
            return index
    return 0


def _model_value(model: object) -> str:
    return str(model).strip()


def _model_values() -> set[str]:
    return {value for _label, value in MODEL_OPTIONS}
