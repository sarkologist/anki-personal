# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Per-card, semantic triage of suspended cards."""


def _install() -> None:
    try:
        import aqt  # noqa: F401
    except ImportError:
        return
    from .browser import install

    install()


_install()
