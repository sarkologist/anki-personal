# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Where the queue says how many threads wait on the user.

N counts the threads waiting on the user (proposal ready, answered, stale),
excluding deferred ones. Two places show it, both opening the queue window:

- A top-toolbar link, "Agent: N ready", hidden at zero. It is added through
  ``top_toolbar_did_init_links`` whenever the toolbar is drawn, and updated in
  place (one ``eval`` on the toolbar webview, no page reload) when a thread
  changes, since this module's listener is in the queue's fan-out.
- A main-screen line under the deck list, like the Frontier add-on's: "Agent
  queue: N requests need your attention. Review", or "nothing needs your
  attention". The deck browser is redrawn only when N changes.
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
STATUS_COMMAND = "agentQueueStatus"

ConfigGetter = Callable[[], dict[str, Any]]

_config: ConfigGetter = dict
_installed = False
# The count the main screen last showed; None when it showed no line.
_shown: int | None = None


def install(*, config: ConfigGetter) -> None:
    global _installed, _config
    _config = config
    if _installed:
        return
    _installed = True
    gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_did_init_links)
    gui_hooks.deck_browser_will_render_content.append(_on_deck_browser_render)
    gui_hooks.webview_did_receive_js_message.append(_on_js_message)
    # After queue_runtime opened the store (its hook was added first).
    gui_hooks.profile_did_open.append(refresh)
    # Deferrals end without a store change; catch up on screen changes. A move
    # to the deck browser renders it anyway, after this hook (asynchronously).
    gui_hooks.state_did_change.append(lambda _new, _old: refresh(redraw=False))


def enabled() -> bool:
    value = _config().get("queue_show_badge", True)
    return value if isinstance(value, bool) else True


def badge_count(store: QueueStore, now: float | None = None) -> int:
    return len(store.list_threads(BADGE_STATUSES, now=now))


def badge_text(count: int) -> str:
    return f"Agent: {count} ready"


def status_html(count: int) -> str:
    style = "margin:1em auto;text-align:center"
    if not count:
        return (
            f'<div style="{style};opacity:.6">'
            "Agent queue: nothing needs your attention.</div>"
        )
    requests = "request needs" if count == 1 else "requests need"
    return (
        f'<div style="{style}">Agent queue: {count} {requests} your attention. '
        f"<a href=# onclick=\"return pycmd('{STATUS_COMMAND}')\">Review</a></div>"
    )


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


def _main_screen_count() -> int | None:
    """None while there is no store to report on (it opens after the first
    deck-browser draw, in profile_did_open)."""
    service = queue_runtime.service()
    if not enabled() or service is None:
        return None
    return badge_count(service.store)


def _on_deck_browser_render(_deck_browser: Any, content: Any) -> None:
    global _shown
    _shown = _main_screen_count()
    if _shown is not None:
        content.stats += status_html(_shown)


def _on_js_message(
    handled: tuple[bool, Any], message: str, _context: Any
) -> tuple[bool, Any]:
    if message != STATUS_COMMAND:
        return handled
    _open_queue()
    return (True, None)


def badge_js(count: int) -> str:
    text = json.dumps(badge_text(count))
    return (
        f"(function(){{var a=document.getElementById({json.dumps(BADGE_ID)});"
        f"if(!a)return;a.textContent={text};a.setAttribute('aria-label',{text});"
        f"a.style.display={json.dumps('' if count else 'none')};}})();"
    )


def refresh(*, redraw: bool = True) -> None:
    """Update the drawn link, and the main screen if its count changed."""
    if not enabled():
        return
    count = _main_screen_count()
    web = getattr(getattr(aqt.mw, "toolbar", None), "web", None)
    if web is not None:
        web.eval(badge_js(count or 0))
    mw = aqt.mw
    # count is None once the store closed (profile closing): never redraw then.
    if (
        redraw
        and count is not None
        and count != _shown
        and getattr(mw, "state", None) == "deckBrowser"
    ):
        mw.deckBrowser.refresh()


class _Listener:
    """Receives the queue's thread-change fan-out (see runtime._all_panes)."""

    def on_thread_changed(self, _thread_id: str) -> None:
        refresh()

    def on_thread_activity(self, _thread_id: str, _line: str) -> None:
        pass


_listener = _Listener()


def listener() -> _Listener:
    return _listener
