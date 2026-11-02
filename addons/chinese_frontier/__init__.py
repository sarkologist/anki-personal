# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Incremental Chinese sentence cards. Pure modules also work outside Anki."""

from __future__ import annotations


def _install() -> None:
    try:
        import aqt
    except ImportError:
        return
    if aqt.mw is not None:
        from .runtime import install

        install()
        from .french.runtime import install as install_french

        install_french()


_install()
