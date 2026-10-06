from pathlib import Path

from PyQt6 import uic
from PyQt6.QtWidgets import QApplication, QMainWindow


class BrowserWindow(QMainWindow):
    def _handle_close(self) -> None:
        pass


def test_long_search_history_does_not_set_browser_pane_width() -> None:
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    browser = BrowserWindow()
    uic.loadUi(
        str(Path(__file__).resolve().parents[1] / "aqt/forms/browser.ui"), browser
    )
    browser.searchEdit.addItems(["", "x" * 300])
    browser.resize(1200, 600)
    browser.show()
    app.processEvents()

    assert browser.searchEdit.sizeHint().width() < browser.width() // 2
    browser.splitter.setSizes([browser.width() // 3, browser.width()])
    assert browser.splitter.sizes()[1] > browser.width() // 2

    browser.close()
