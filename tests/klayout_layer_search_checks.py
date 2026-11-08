"""Run only inside a separate KLayout GUI-capable process, using the offscreen platform."""

import io
import json
from pathlib import Path
import sys
import time
import unittest

import pya

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_layer_search as search

app = pya.Application.instance()
mw = app.main_window()


def settle(seconds=0.22):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.process_events()
        time.sleep(0.005)


class LayerSearchChecks(unittest.TestCase):
    def setUp(self):
        previous = getattr(pya, "_pocket_layer_search", None)
        if previous is not None:
            previous.dispose()
            pya._pocket_layer_search = None
        mw.close_all()
        self.cv = mw.create_layout(1)
        self.view = mw.current_view()
        for number in (68, 69, 70):
            self.cv.layout().layer(number, 20)
        self.view.add_missing_layers()
        for node, name in zip(self.view.each_layer(), ("met1.drawing", "MET2.drawing", "via1.drawing")):
            node.name = name
        settle(0.05)
        self.manager = search.install()
        self.assertEqual(len(self.manager.controllers), 1)
        self.controller = next(iter(self.manager.controllers.values()))
        settle()

    def tearDown(self):
        manager = getattr(pya, "_pocket_layer_search", None)
        if manager is not None:
            manager.dispose()
            pya._pocket_layer_search = None
        mw.close_all()
        settle(0.03)

    def query(self, text):
        self.controller.entry.setText(text)
        settle()
        self.controller.refresh_count()

    def label(self):
        return str(search.qt_value(self.controller.counter, "text"))

    def test_name_number_case_clear_and_empty_results(self):
        self.assertEqual(self.label(), "3 capas")
        for query in ("met1", "68/20", "MET1"):
            self.query(query)
            self.assertEqual(self.label(), "1 / 3 capas", "query=" + query)
        self.query("met")
        self.assertEqual(self.label(), "2 / 3 capas")
        self.query("not-a-layer")
        self.assertIn("Sin coincidencias", self.label())
        self.controller.clear_button.click()
        settle()
        self.assertEqual(self.label(), "3 capas")

    def test_search_preserves_colors_visibility_selection_and_current_layer(self):
        nodes = list(self.view.each_layer())
        nodes[0].visible = False
        nodes[1].fill_color = 0x123456
        self.view.current_layer = self.view.begin_layers()
        settle(0.03)
        def snapshot():
            current = self.view.current_layer.current()
            return ([(node.name, node.visible, node.fill_color, node.frame_color)
                     for node in self.view.each_layer()], current.name, current.source,
                    [layer.current().source for layer in self.view.selected_layers()])
        before = snapshot()
        self.query("MET2")
        self.assertEqual(snapshot(), before)
        self.controller.clear()
        settle()
        self.assertEqual(snapshot(), before)

    def test_escape_slash_enter_and_native_search_remains_hidden(self):
        self.query("met1")
        event = pya.QKeyEvent(pya.QEvent.KeyPress, int(pya.Qt.Key_Escape), pya.Qt.NoModifier)
        pya.QApplication.sendEvent(self.controller.entry, event)
        settle()
        self.assertEqual(search.qt_value(self.controller.entry, "text"), "")
        slash = pya.QKeyEvent(pya.QEvent.KeyPress, int(pya.Qt.Key_Slash), pya.Qt.NoModifier, "/")
        pya.QApplication.sendEvent(self.controller.tree, slash)
        self.assertTrue(search.qt_value(self.controller.native_frame, "hidden"))
        self.query("68/20")
        self.controller.finish_editing()
        settle()
        self.assertFalse(search.qt_value(self.controller.bar, "hidden"))
        self.assertEqual(self.label(), "1 / 3 capas")

    def test_new_layers_and_new_views_are_attached_without_duplicate_bars(self):
        self.query("met")
        self.cv.layout().layer(71, 20)
        self.view.add_missing_layers()
        settle(0.3)
        self.assertEqual(self.label(), "2 / 4 capas")
        self.manager.scan()
        self.assertEqual(len(self.manager.controllers), 1)
        self.assertEqual(sum(str(search.qt_value(child, "objectName")) == "PocketLayerSearch"
                             for child in search.descendants(mw)), 1)
        mw.create_layout(1)
        settle(0.05)
        self.manager.scan()
        self.assertEqual(len(self.manager.controllers), 2)
        mw.current_view_index = 0
        settle()
        self.assertEqual(search.qt_value(self.controller.entry, "text"), "met")

    def test_reload_restores_native_options_and_does_not_stack_bars(self):
        original = self.controller.original_options
        native_actions = self.controller.actions
        self.query("met1")
        self.manager.dispose()
        self.manager.dispose()  # Reload and explicit cleanup may both run.
        settle(0.05)
        self.assertEqual([bool(search.qt_value(a, "checked")) for a in native_actions], original)
        self.manager = search.install()
        settle()
        self.assertEqual(len(self.manager.controllers), 1)
        self.assertEqual(sum(str(search.qt_value(child, "objectName")) == "PocketLayerSearch"
                             for child in search.descendants(mw)), 1)

    def test_groups_remain_available_when_a_child_matches(self):
        self.view.clear_layers()
        group = pya.LayerPropertiesNode()
        group.name = "Metals"
        for source, name in (("68/20@1", "met1"), ("69/20@1", "met2")):
            node = pya.LayerPropertiesNode()
            node.name, node.source = name, source
            group.add_child(node)
        self.view.insert_layer(self.view.begin_layers(), group)
        settle()
        self.query("met1")
        self.assertEqual(self.label(), "1 / 2 capas")
        self.assertFalse(self.controller.tree.isRowHidden(0, pya.QModelIndex()))
        self.controller.clear()
        settle()
        self.assertEqual(self.label(), "2 capas")

    def test_layer_tabs_use_the_same_search_and_theme_changes_keep_it_readable(self):
        self.query("met")
        self.view.insert_layer_list(1)
        node = pya.LayerPropertiesNode()
        node.name, node.source = "via2", "70/20@1"
        self.view.insert_layer(self.view.begin_layers(1), node)
        settle()
        self.controller.refresh_count()
        self.assertIn("Sin coincidencias", self.label())
        self.view.current_layer_list = 0
        settle()
        self.assertEqual(self.label(), "2 / 3 capas")
        original = search.qt_value(app, "palette")
        palette = pya.QPalette(original)
        palette.setColor(pya.QPalette.Base, pya.QColor("#181818"))
        palette.setColor(pya.QPalette.Text, pya.QColor("#eeeeee"))
        try:
            app.setPalette(palette)
            settle(0.05)
            self.assertEqual(self.label(), "2 / 3 capas")
            self.assertFalse(search.qt_value(self.controller.bar, "hidden"))
        finally:
            app.setPalette(original)

    def test_optional_preview(self):
        if "layer_search_preview" not in globals():
            return
        self.query("met")
        mw.resize(1050, 650)
        mw.show()
        settle(0.2)
        self.assertTrue(mw.grab().save(layer_search_preview))


output = io.StringIO()
try:
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LayerSearchChecks)
    )
    Path(layer_search_report).write_text(json.dumps(dict(
        success=result.wasSuccessful(), tests=result.testsRun, output=output.getvalue()
    )), encoding="utf-8")
finally:
    app.exit(0)
