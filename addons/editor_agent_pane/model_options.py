# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

PROVIDER_CODEX = "codex"
PROVIDER_OLLAMA = "ollama"
PROVIDER_CLAUDE = "claude"
DEFAULT_PROVIDER = PROVIDER_CODEX

KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {PROVIDER_CODEX, PROVIDER_OLLAMA, PROVIDER_CLAUDE}
)

PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Codex", PROVIDER_CODEX),
    ("Ollama", PROVIDER_OLLAMA),
    ("Claude", PROVIDER_CLAUDE),
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

CLAUDE_MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Claude default", ""),
    ("Opus", "opus"),
    ("Sonnet", "sonnet"),
    ("Haiku", "haiku"),
)


def provider_value(provider: object) -> str:
    value = str(provider).strip()
    if value in KNOWN_PROVIDERS:
        return value
    return DEFAULT_PROVIDER


def provider_option_index(provider: object) -> int:
    value = provider_value(provider)
    for index, (_label, option_value) in enumerate(PROVIDER_OPTIONS):
        if option_value == value:
            return index
    return 0


def model_options_with_legacy(
    model: object,
    options: tuple[tuple[str, str], ...] = MODEL_OPTIONS,
) -> tuple[tuple[str, str], ...]:
    value = _model_value(model)
    known = {option_value for _label, option_value in options}
    if value and value not in known:
        return (*options, (value, value))
    return options


def model_option_index(
    model: object,
    options: tuple[tuple[str, str], ...] = MODEL_OPTIONS,
) -> int:
    value = _model_value(model)
    for index, (_label, option_value) in enumerate(
        model_options_with_legacy(value, options)
    ):
        if option_value == value:
            return index
    return 0


def _model_value(model: object) -> str:
    return str(model).strip()


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
