from __future__ import annotations


def repair_header(header) -> bool:
    """Repair Qt 6.11's geometry after restoring a shorter saved header.

    Qt can report the new section count while retaining the old total length
    and returning -1 for new section positions. Rebuild its section storage,
    preserving the user's layout, without resetting the table or its selection.
    Healthy headers (including on other Qt versions) are left alone.
    """
    count = header.count()
    sizes = [header.sectionSize(i) for i in range(count)]
    if header.length() == sum(sizes) and all(
        header.isSectionHidden(i) or header.sectionPosition(i) >= 0
        for i in range(count)
    ):
        return False

    blocked = header.blockSignals(True)
    try:
        order = [header.logicalIndex(i) for i in range(count)]
        modes = [header.sectionResizeMode(i) for i in range(count)]
        hidden = [header.isSectionHidden(i) for i in range(count)]
        sort = header.sortIndicatorSection(), header.sortIndicatorOrder()
        offset = header.offset()
        # sectionSize() returns zero for hidden sections; retain their real width.
        for i in range(count):
            if hidden[i]:
                header.showSection(i)
                sizes[i] = header.sectionSize(i)

        model = header.model()
        selection = header.selectionModel()
        header.setModel(None)
        header.setModel(model)
        header.setSelectionModel(selection)
        header.setSectionResizeMode(header.ResizeMode.Interactive)
        for i, size in enumerate(sizes):
            header.resizeSection(i, size)
        for visual, logical in enumerate(order):
            header.moveSection(header.visualIndex(logical), visual)
        for i in range(count):
            header.setSectionHidden(i, hidden[i])
            header.setSectionResizeMode(i, modes[i])
        header.setSortIndicator(*sort)
        header.setOffset(offset)
    finally:
        header.blockSignals(blocked)
    # Notify the view to recalculate its scroll range after the blocked rebuild.
    header.geometriesChanged.emit()
    header.viewport().update()
    return True
