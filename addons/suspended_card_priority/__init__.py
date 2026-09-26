"""Per-card, semantic triage of suspended cards."""


def _install() -> None:
    try:
        import aqt  # noqa: F401
    except ImportError:
        return
    from .browser import install

    install()


_install()
