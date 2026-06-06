# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

PROVIDER_CODEX = "codex"
PROVIDER_OLLAMA = "ollama"
DEFAULT_PROVIDER = PROVIDER_CODEX

PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Codex", PROVIDER_CODEX),
    ("Ollama", PROVIDER_OLLAMA),
)

MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Codex default", ""),
    ("gpt-5.5", "gpt-5.5"),
    ("gpt-5.4", "gpt-5.4"),
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5.3-codex", "gpt-5.3-codex"),
    ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
    ("gpt-5.2", "gpt-5.2"),
)


def provider_value(provider: object) -> str:
    value = str(provider).strip()
    if value in {PROVIDER_CODEX, PROVIDER_OLLAMA}:
        return value
    return DEFAULT_PROVIDER


def provider_option_index(provider: object) -> int:
    value = provider_value(provider)
    for index, (_label, option_value) in enumerate(PROVIDER_OPTIONS):
        if option_value == value:
            return index
    return 0


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


def ollama_model_options_with_legacy(
    model: object,
    discovered_models: tuple[str, ...],
    *,
    unavailable: bool = False,
) -> tuple[tuple[str, str], ...]:
    value = _model_value(model)
    options = tuple((name, name) for name in discovered_models if name.strip())
    if value and value not in {option_value for _label, option_value in options}:
        return (*options, (value, value))
    if options:
        return options
    if value:
        return ((value, value),)
    if unavailable:
        return (("Ollama unavailable", ""),)
    return (("No Ollama models installed", ""),)


def ollama_model_option_index(
    model: object,
    discovered_models: tuple[str, ...],
    *,
    unavailable: bool = False,
) -> int:
    value = _model_value(model)
    for index, (_label, option_value) in enumerate(
        ollama_model_options_with_legacy(
            value,
            discovered_models,
            unavailable=unavailable,
        )
    ):
        if option_value == value:
            return index
    return 0
