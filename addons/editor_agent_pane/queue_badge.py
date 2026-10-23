# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""A top-toolbar link, "Agent: N ready", that opens the queue window.

N counts the threads waiting on the user (proposal ready, answered, stale),
excluding deferred ones; the link is hidden at zero. The link is added through
``top_toolbar_did_init_links`` whenever the toolbar is drawn, and updated in
place (one ``eval`` on the toolbar webview, no page reload) when a thread
changes, since this module's listener is in the queue's fan-out.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any

import aqt
from aqt import gui_hooks

from . import queue_runtime, queue_window
from .queue_store import ANSWERED, READY, STALE, QueueStore

BADGE_COMMAND = "agentQueueBadge"
BADGE_ID = "agent-queue-badge"
BADGE_STATUSES = (READY, ANSWERED, STALE)

ConfigGetter = Callable[[], dict[str, Any]]

_config: ConfigGetter = dict
_installed = False


def install(*, config: ConfigGetter) -> None:
    global _installed, _config
    _config = config
    if _installed:
        return
    _installed = True
    gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_did_init_links)
    # After queue_runtime opened the store (its hook was added first).
    gui_hooks.profile_did_open.append(refresh)
    # Deferrals end without a store change; catch up on screen changes.
    gui_hooks.state_did_change.append(lambda _new, _old: refresh())


def enabled() -> bool:
    value = _config().get("queue_show_badge", True)
    return value if isinstance(value, bool) else True


def badge_count(store: QueueStore, now: float | None = None) -> int:
    return len(store.list_threads(BADGE_STATUSES, now=now))


def badge_text(count: int) -> str:
    return f"Agent: {count} ready"


def _current_count() -> int:
    service = queue_runtime.service()
    return badge_count(service.store) if service is not None else 0


def _open_queue() -> None:
    queue_window.open_window()


def _on_top_toolbar_did_init_links(links: list[str], toolbar: Any) -> None:
    if not enabled():
        return
    toolbar.link_handlers[BADGE_COMMAND] = _open_queue
    count = _current_count()
    text = html.escape(badge_text(count))
    tip = html.escape(f"Open the agent queue ({queue_window.shortcut()})")
    hidden = "" if count else ' style="display:none"'
    links.append(
        f'<a class=hitem tabindex="-1" id="{BADGE_ID}" aria-label="{text}" '
        f'title="{tip}" href=# onclick="return pycmd(\'{BADGE_COMMAND}\')"'
        f"{hidden}>{text}</a>"
    )


def badge_js(count: int) -> str:
    text = json.dumps(badge_text(count))
    return (
        f"(function(){{var a=document.getElementById({json.dumps(BADGE_ID)});"
        f"if(!a)return;a.textContent={text};a.setAttribute('aria-label',{text});"
        f"a.style.display={json.dumps('' if count else 'none')};}})();"
    )


def refresh() -> None:
    """Update the drawn link's text and visibility."""
    if not enabled():
        return
    web = getattr(getattr(aqt.mw, "toolbar", None), "web", None)
    if web is None:
        return
    web.eval(badge_js(_current_count()))


class _Listener:
    """Receives the queue's thread-change fan-out (see runtime._all_panes)."""

    def on_thread_changed(self, _thread_id: str) -> None:
        refresh()

    def on_thread_activity(self, _thread_id: str, _line: str) -> None:
        pass


_listener = _Listener()


def listener() -> _Listener:
    return _listener
