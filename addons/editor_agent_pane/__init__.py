# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Editor Agent Pane add-on prototype.

This package is intentionally safe to import in unit tests outside of Anki.
When Anki loads it as an add-on, the runtime module registers the GUI hooks.
"""

from __future__ import annotations


def _install_runtime() -> None:
    try:
        import aqt  # noqa: F401
    except ImportError:
        return

    from .runtime import install

    install()


_install_runtime()
