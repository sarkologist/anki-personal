# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Run in the stock instance via benchctl: show OFFSET..OFFSET+CARDS cards,
question then answer, every INTERVAL ms. The placeholders are substituted by
bench.py; `mw` comes from benchctl."""

from anki.search_pb2 import SearchResponse
from aqt.qt import QTimer
import time

ids = list(SearchResponse.FromString(mw.col._backend.search_cards_raw(b"")).ids)[OFFSET:OFFSET + CARDS]
r = mw.reviewer
if mw.state != "review":
    mw.state = "review"
    r.card = mw.col.get_card(ids[0])
    r.card.start_timer()
    r._initWeb()
    # the page queues evals until it has loaded, so this runs first
    r.web.eval(PAGE_TIMING_JS)
if not hasattr(mw, "_bench_host"):
    mw._bench_host = {"q": [], "a": []}
r._showEaseButtons = lambda: None
r._showAnswerButton = lambda: None
steps = []
for cid in ids:
    steps.append(("q", cid))
    steps.append(("a", cid))
mw._bench = {"start": time.time(), "done": None, "errors": []}

def step(i=0):
    if i >= len(steps):
        mw._bench["done"] = time.time()
        return
    kind, cid = steps[i]
    try:
        t0 = time.perf_counter()
        if kind == "q":
            r.card = mw.col.get_card(cid)
            r.card.start_timer()
            r._showQuestion()
        else:
            r._showAnswer()
        mw._bench_host[kind].append((time.perf_counter() - t0) * 1000)
    except Exception as e:
        mw._bench["errors"].append(repr(e))
    QTimer.singleShot(INTERVAL, lambda: step(i + 1))

step()
result = (len(ids), ids[:3])
