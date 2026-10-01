from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QStandardItemModel
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QHeaderView, QTableView

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))

from suspended_card_priority.layout import repair_header  # noqa: E402


@pytest.fixture(scope="module")
def app():
    # No windows or interaction with the user's running Anki instance.
    return QApplication.instance() or QApplication(["test", "-platform", "offscreen"])


def resized_header(columns):
    header = QHeaderView(Qt.Orientation.Horizontal)
    model = QStandardItemModel(1, columns, header)
    header.setModel(model)
    header.setSectionsClickable(True)
    header.resizeSection(0, 120)
    return header


def test_restoring_fewer_columns_keeps_new_column_reachable(app):
    old = resized_header(1)
    new = resized_header(2)
    new.restoreState(old.saveState())

    repair_header(new)

    assert new.sectionPosition(1) == 120
    assert new.length() == 120 + new.sectionSize(1)
    assert new.logicalIndexAt(125) == 1


def test_repair_preserves_widths_order_and_sort_without_emitting_sort(app):
    old = resized_header(2)
    old.resizeSection(1, 160)
    old.moveSection(1, 0)
    old.setSortIndicator(1, Qt.SortOrder.DescendingOrder)
    new = resized_header(3)
    new.restoreState(old.saveState())
    sorts = []
    new.sortIndicatorChanged.connect(lambda *args: sorts.append(args))

    repair_header(new)

    assert [new.logicalIndex(i) for i in range(3)] == [1, 0, 2]
    assert [new.sectionSize(i) for i in range(2)] == [120, 160]
    assert new.sortIndicatorSection() == 1
    assert new.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert sorts == []
    assert new.length() == sum(new.sectionSize(i) for i in range(3))


def test_healthy_header_is_unchanged(app):
    header = resized_header(2)
    header.setOffset(50)
    before = header.saveState()
    assert not repair_header(header)
    assert header.saveState() == before
    assert header.offset() == 50


def test_repair_preserves_hidden_column_width(app):
    old = resized_header(2)
    old.hideSection(0)
    new = resized_header(3)
    new.restoreState(old.saveState())

    repair_header(new)

    assert new.isSectionHidden(0)
    new.showSection(0)
    assert new.sectionSize(0) == 120
    assert new.length() == sum(new.sectionSize(i) for i in range(3))


def test_repair_preserves_stretch_mode(app):
    old = resized_header(1)
    new = resized_header(2)
    new.restoreState(old.saveState())
    new.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    repair_header(new)

    assert new.sectionResizeMode(1) == QHeaderView.ResizeMode.Stretch
    assert new.sectionPosition(1) == 120
    assert new.length() == sum(new.sectionSize(i) for i in range(2))


def test_repair_preserves_shared_table_selection(app):
    old = resized_header(1)
    view = QTableView()
    model = QStandardItemModel(1, 2, view)
    view.setModel(model)
    view.selectRow(0)
    selection = view.selectionModel()
    header = view.horizontalHeader()
    header.restoreState(old.saveState())

    repair_header(header)

    assert header.selectionModel() is selection
    assert view.selectionModel() is selection
    assert selection.selectedRows()[0].row() == 0


def test_new_column_header_can_be_clicked_to_sort(app):
    old = resized_header(1)
    view = QTableView()
    model = QStandardItemModel(2, 2, view)
    model.setData(model.index(0, 1), 9)
    model.setData(model.index(1, 1), 91)
    view.setModel(model)
    view.setSortingEnabled(True)
    header = view.horizontalHeader()
    header.restoreState(old.saveState())
    repair_header(header)
    view.show()
    app.processEvents()

    QTest.mouseClick(
        header.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(header.sectionViewportPosition(1) + 20, header.height() // 2),
    )

    assert header.sortIndicatorSection() == 1
    order = header.sortIndicatorOrder()
    assert [model.data(model.index(i, 1)) for i in range(2)] == (
        [91, 9] if order == Qt.SortOrder.DescendingOrder else [9, 91]
    )
    view.close()
