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
    ("Max", "max"),
    ("Ultra", "ultra"),
)

CLAUDE_EFFORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Claude default", ""),
    ("Low", "low"),
    ("Medium", "medium"),
    ("High", "high"),
    ("XHigh", "xhigh"),
    ("Max", "max"),
)

# Reasoning levels are per-model, not per-provider: a request with a level the
# model does not implement is rejected outright (Codex answers with a 400 that
# lists the levels the model does take). Both tables mirror the model catalogue
# each CLI ships with; a model missing from a table is left unrestricted, since
# an unknown or empty selection means the CLI resolves the model from its own
# config and we cannot tell what it will pick.
_CODEX_THROUGH_XHIGH = frozenset({"none", "low", "medium", "high", "xhigh"})
CODEX_MODEL_EFFORT_LEVELS: dict[str, frozenset[str]] = {
    "gpt-5.6-sol": _CODEX_THROUGH_XHIGH | {"max", "ultra"},
    "gpt-5.6-terra": _CODEX_THROUGH_XHIGH | {"max", "ultra"},
    "gpt-5.6-luna": _CODEX_THROUGH_XHIGH | {"max"},
    "gpt-5.5": _CODEX_THROUGH_XHIGH,
    "gpt-5.4": _CODEX_THROUGH_XHIGH,
    "gpt-5.4-mini": _CODEX_THROUGH_XHIGH,
    "gpt-5.2": _CODEX_THROUGH_XHIGH,
}

_CLAUDE_THROUGH_MAX = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_MODEL_EFFORT_LEVELS: dict[str, frozenset[str]] = {
    "fable": _CLAUDE_THROUGH_MAX,
    "opus": _CLAUDE_THROUGH_MAX,
    "sonnet": _CLAUDE_THROUGH_MAX,
    # Haiku 4.5 has no xhigh/max effort.
    "haiku": frozenset({"low", "medium", "high"}),
}


# Every level either CLI names. An effort outside this set came from a build we
# do not know, so it is kept rather than silently reset.
KNOWN_EFFORT_VALUES: frozenset[str] = frozenset(
    value for _label, value in (*EFFORT_OPTIONS, *CLAUDE_EFFORT_OPTIONS) if value
)


def codex_effort_options(model: object = "") -> tuple[tuple[str, str], ...]:
    return _options_for_model(EFFORT_OPTIONS, CODEX_MODEL_EFFORT_LEVELS, model)


def claude_effort_options(model: object = "") -> tuple[tuple[str, str], ...]:
    return _options_for_model(CLAUDE_EFFORT_OPTIONS, CLAUDE_MODEL_EFFORT_LEVELS, model)


def _options_for_model(
    options: tuple[tuple[str, str], ...],
    levels_by_model: dict[str, frozenset[str]],
    model: object,
) -> tuple[tuple[str, str], ...]:
    levels = levels_by_model.get(str(model).strip())
    if levels is None:
        return options
    # The provider default ("") always stays: it defers to the CLI.
    return tuple(
        (label, value) for label, value in options if not value or value in levels
    )


def effort_supported_by_options(
    effort: object,
    options: tuple[tuple[str, str], ...],
) -> str:
    """Return ``effort`` when ``options`` offer it, otherwise the default."""

    value = _effort_value(effort)
    return value if value in _option_values(options) else ""


def effort_choice_for_options(
    effort: object,
    options: tuple[tuple[str, str], ...],
) -> str:
    """Pick the effort to show in a dropdown offering ``options``.

    A known level that the selected provider/model pair does not take falls back
    to the default. An effort this addon has never heard of is left alone, so a
    config written by a newer build still round-trips through the dropdown.
    """

    value = _effort_value(effort)
    if not value or value in _option_values(options):
        return value
    return "" if value in KNOWN_EFFORT_VALUES else value


def _option_values(options: tuple[tuple[str, str], ...]) -> frozenset[str]:
    return frozenset(value for _label, value in options)


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
