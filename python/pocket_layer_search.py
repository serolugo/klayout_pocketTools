"""Compact layer search backed by KLayout's native layer-tree filter (0.30.12)."""

import pya


def qt_value(obj, name):
    value = getattr(obj, name)
    return value() if callable(value) else value


def descendants(obj):
    for child in qt_value(obj, "children"):
        yield child
        yield from descendants(child)


class LayerSearch(pya.QObject):
    def __init__(self, tree, view):
        panel = qt_value(tree, "parent")
        native = next((child for child in descendants(panel)
                       if str(qt_value(child, "objectName")) == "cellview_search_edit_box"), None)
        # These three actions belong to LayerControlPanel in KLayout 0.30.12:
        # wildcard matching, case sensitivity, and filtering the list.
        actions = [child for child in qt_value(panel, "children")
                   if type(child).__name__.startswith("QAction")]
        if native is None or len(actions) != 3 or not all(qt_value(a, "checkable") for a in actions):
            raise RuntimeError("El panel de capas no tiene un buscador nativo compatible.")
        super().__init__(panel)
        self.tree, self.panel, self.native, self.actions = tree, panel, native, actions
        self.view = view
        self.original_show_source = view.get_config("layers-always-show-source")
        if self.original_show_source is None:
            self.original_show_source = pya.Application.instance().get_config("layers-always-show-source")
        view.set_config("layers-always-show-source", "true")
        self.native_frame = qt_value(native, "parent")
        self.original_text = str(qt_value(native, "text"))
        self.original_options = [bool(qt_value(action, "checked")) for action in actions]
        self.original_hidden = bool(qt_value(self.native_frame, "hidden"))
        self.disposed = False
        self.applying = False

        self.bar = pya.QFrame(panel)
        self.bar.setObjectName("PocketLayerSearch")
        layout = pya.QVBoxLayout(self.bar)
        layout.setContentsMargins(5, 4, 5, 3)
        layout.setSpacing(2)
        row = pya.QHBoxLayout()
        row.setSpacing(3)
        layout.addLayout(row)
        self.entry = pya.QLineEdit(self.bar)
        self.entry.setObjectName("PocketLayerSearchEntry")
        self.entry.setPlaceholderText("Buscar capa…")
        self.entry.setToolTip("Nombre o layer/datatype, sin distinguir mayusculas. Ejemplos: met1, 68/20. Comodines: * y ?. Esc: limpiar.")
        self.entry.setMinimumHeight(25)
        row.addWidget(self.entry, 1)
        self.clear_button = pya.QToolButton(self.bar)
        self.clear_button.setText("×")
        self.clear_button.setToolTip("Limpiar busqueda (Esc)")
        self.clear_button.setAutoRaise(True)
        row.addWidget(self.clear_button)
        self.counter = pya.QLabel(self.bar)
        self.counter.setText("Todas las capas")
        self.counter.setStyleSheet("font-size:11px; color:palette(text);")
        layout.addWidget(self.counter)
        self.bar.setStyleSheet("QFrame#PocketLayerSearch { background:palette(base); border-bottom:1px solid palette(mid); }")
        qt_value(panel, "layout").insertWidget(0, self.bar)
        self.native_frame.hide()

        self.debounce = pya.QTimer(self.bar)
        self.debounce.setSingleShot(True)
        self.debounce.timeout = self.apply
        self.count_timer = pya.QTimer(self.bar)
        self.count_timer.setSingleShot(True)
        self.count_timer.timeout = self.refresh_count
        self.entry.textChanged = self.schedule
        self.entry.returnPressed = self.finish_editing
        self.clear_button.clicked = self.clear
        self.entry.installEventFilter(self)
        self.tree.installEventFilter(self)

        self.model = qt_value(tree, "model")
        self.model_callbacks = []
        for signal_name in ("modelReset", "rowsInserted", "rowsRemoved", "layoutChanged"):
            signal = getattr(self.model, signal_name)
            callback = self.model_changed
            signal.connect(callback)
            self.model_callbacks.append((signal, callback))
        self.model.dataChanged.connect(self.defer_count)
        self.model_callbacks.append((self.model.dataChanged, self.defer_count))
        self.entry.setText(self.original_text)
        self.apply()

    def schedule(self, *args):
        if not self.disposed:
            self.debounce.start(120)

    def defer_count(self, *args):
        if not self.disposed:
            self.count_timer.start(40)

    def model_changed(self, *args):
        if not self.applying:
            self.schedule()

    def set_native_filter(self, text, options):
        # KLayout's Qt binding cannot emit textEdited or invoke private slots.
        # Triggering its own filter action runs the existing native search slot.
        selection_model = qt_value(self.tree, "selectionModel")
        selected = qt_value(selection_model, "selection")
        current = qt_value(selection_model, "currentIndex")
        blocked = selection_model.blockSignals(True)
        self.applying = True
        try:
            self.native.setText(text)
            self.actions[0].setChecked(options[0])
            self.actions[1].setChecked(options[1])
            self.actions[2].setChecked(not options[2])
            self.actions[2].trigger()
            # Searching must not change the current drawing layer or visibility.
            selection_model.select(selected, pya.QItemSelectionModel.ClearAndSelect)
            selection_model.setCurrentIndex(current, pya.QItemSelectionModel.NoUpdate)
        finally:
            self.applying = False
            selection_model.blockSignals(blocked)

    def apply(self):
        if self.disposed:
            return
        self.debounce.stop()
        query = str(qt_value(self.entry, "text")).strip()
        self.set_native_filter("*" + query + "*" if query else "", (True, False, True))
        self.clear_button.setEnabled(bool(query))
        self.defer_count()

    def refresh_count(self):
        if self.disposed:
            return
        visible = total = 0
        matched = False
        rows = []
        stack = [(pya.QModelIndex(), True)]
        while stack:
            parent, parent_visible = stack.pop()
            for row in range(self.model.rowCount(parent)):
                index = self.model.index(row, 0, parent)
                shown = parent_visible and not self.tree.isRowHidden(row, parent)
                rows.append((row, parent))
                if self.model.data(index, int(pya.Qt.BackgroundRole)) is not None:
                    matched = True
                if self.model.rowCount(index):
                    stack.append((index, shown))
                else:
                    total += 1
                    visible += int(shown)
        query = str(qt_value(self.entry, "text")).strip()
        # Native filtering deliberately shows everything when nothing matches.
        # Keep a real empty result for this search bar, without changing layers.
        if query and not matched:
            for row, parent in rows:
                self.tree.setRowHidden(row, parent, True)
            visible = 0
        label = f"{visible} / {total} capas" if query else f"{total} capas"
        if query and not visible:
            label = f"Sin coincidencias · 0 / {total} capas"
        self.counter.setText(label)

    def clear(self, *args):
        self.entry.clear()
        self.apply()
        self.entry.setFocus()

    def finish_editing(self):
        self.apply()
        self.tree.setFocus()

    def eventFilter(self, watched, event):
        if int(qt_value(event, "type")) != int(pya.QEvent.KeyPress):
            return False
        key = int(qt_value(event, "key"))
        name = str(qt_value(watched, "objectName"))
        if name == "PocketLayerSearchEntry" and key == int(pya.Qt.Key_Escape):
            self.clear()
            return True
        if name == "layer_tree":
            text = str(qt_value(event, "text"))
            modifiers = int(qt_value(event, "modifiers"))
            if text and text.isprintable() and not modifiers & (int(pya.Qt.ControlModifier) | int(pya.Qt.AltModifier)):
                self.entry.setFocus()
                if text == "/":
                    self.entry.selectAll()
                else:
                    self.entry.setText(text)
                return True
        return False

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.debounce.stop()
        self.count_timer.stop()
        for signal, callback in self.model_callbacks:
            signal.disconnect(callback)
        self.entry.removeEventFilter(self)
        self.tree.removeEventFilter(self)
        self.set_native_filter(self.original_text, self.original_options)
        self.view.set_config("layers-always-show-source", self.original_show_source or "false")
        self.native_frame.setVisible(not self.original_hidden)
        self.bar.setObjectName("PocketLayerSearchDisposed")
        self.bar.hide()
        self.bar.deleteLater()
        self.deleteLater()


class LayerSearchManager:
    def __init__(self, main_window):
        self.main_window = main_window
        self.disposed = False
        self.controllers = {}
        self.failed = set()
        self.timer = pya.QTimer(main_window)
        self.timer.timeout = self.scan
        self.timer.start(600)
        self.scan()

    def scan(self):
        if self.disposed:
            return
        owners = {}
        for index in range(qt_value(self.main_window, "views")):
            view = self.main_window.view(index)
            frame = view.widget().layer_control_frame()
            for child in descendants(frame):
                if str(qt_value(child, "objectName")) == "layer_tree":
                    owners[child] = view
        trees = set(owners)
        for tree in list(self.controllers):
            if tree not in trees:
                controller = self.controllers.pop(tree)
                try:
                    controller.dispose()
                except RuntimeError:
                    pass  # Qt already destroyed the closed layout's widgets.
        self.failed.intersection_update(trees)
        for tree in trees - self.controllers.keys() - self.failed:
            try:
                self.controllers[tree] = LayerSearch(tree, owners[tree])
            except (RuntimeError, AttributeError) as exc:
                self.failed.add(tree)
                print("Pocket Layers: " + str(exc))

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.timer.stop()
        for controller in self.controllers.values():
            try:
                controller.dispose()
            except RuntimeError:
                pass
        self.controllers.clear()
        self.timer.deleteLater()


def install():
    main_window = pya.Application.instance().main_window()
    if main_window is None:
        return None
    previous = getattr(pya, "_pocket_layer_search", None)
    if previous is not None:
        previous.dispose()
    manager = LayerSearchManager(main_window)
    pya._pocket_layer_search = manager
    return manager
