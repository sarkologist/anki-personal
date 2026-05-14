# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
from pathlib import Path

import pytest

from aqt.profiles import ProfileManager


def profile_manager(tmp_path: Path) -> ProfileManager:
    manager = ProfileManager(tmp_path)
    manager.meta = {}
    return manager


def test_zoom_factors_default_to_one(tmp_path: Path) -> None:
    manager = profile_manager(tmp_path)

    assert manager.main_window_zoom_factor() == 1.0
    assert manager.browser_editor_zoom_factor() == 1.0


def test_zoom_factors_round_trip_independently(tmp_path: Path) -> None:
    manager = profile_manager(tmp_path)

    manager.set_main_window_zoom_factor(1.3)
    manager.set_browser_editor_zoom_factor(0.8)

    assert manager.main_window_zoom_factor() == 1.3
    assert manager.browser_editor_zoom_factor() == 0.8


def test_reset_zoom_value_persists(tmp_path: Path) -> None:
    manager = profile_manager(tmp_path)

    manager.set_main_window_zoom_factor(1.5)
    manager.set_main_window_zoom_factor(1.0)

    assert manager.main_window_zoom_factor() == 1.0


@pytest.mark.parametrize("invalid_value", [None, True, "1.4", math.nan, math.inf])
def test_invalid_zoom_metadata_falls_back_to_one(
    tmp_path: Path, invalid_value: object
) -> None:
    manager = profile_manager(tmp_path)
    manager.meta["mainWindowZoomFactor"] = invalid_value
    manager.meta["browserEditorZoomFactor"] = invalid_value

    assert manager.main_window_zoom_factor() == 1.0
    assert manager.browser_editor_zoom_factor() == 1.0


def test_zoom_factors_are_clamped(tmp_path: Path) -> None:
    manager = profile_manager(tmp_path)

    manager.set_main_window_zoom_factor(0.1)
    manager.set_browser_editor_zoom_factor(7.0)

    assert manager.main_window_zoom_factor() == 0.25
    assert manager.browser_editor_zoom_factor() == 5.0
