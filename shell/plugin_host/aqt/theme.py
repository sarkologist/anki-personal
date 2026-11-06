# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations


class ThemeManager:
    # the shell's: set when the host starts and when it changes
    # (`set_theme`, `theme_did_change`)
    night_mode = False


theme_manager = ThemeManager()
