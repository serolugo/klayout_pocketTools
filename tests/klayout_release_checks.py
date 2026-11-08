"""Native Windows/GUI API checks; launched in a separate, isolated KLayout."""
import io
from collections import Counter
import json
import os
from pathlib import Path
import runpy
import sys
import time
import traceback
import unittest
import xml.etree.ElementTree as ET

import pya

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_edit as edit

app = pya.Application.instance()
mw = app.main_window()


class ReleaseChecks(unittest.TestCase):
    def test_drc_status_colors_are_rendered(self):
        # Windows Qt's offscreen platform does not discover system fonts.
        # Load one explicitly so pixel assertions include real text glyphs.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
        if font.is_file():
            self.assertGreaterEqual(pya.QFontDatabase.addApplicationFont(str(font)), 0)
            pya.QApplication.setFont(pya.QFont("Segoe UI", 10))
        ruby = pya.Interpreter.ruby_interpreter()
        if ruby is None:
            self.skipTest("Ruby unavailable")
        ruby.eval_string((ROOT / "macros/pocket_themes.rb").read_text(encoding="utf-8"))
        rdb_index = self.view.create_rdb("DRC states")
        database = self.view.rdb(rdb_index)
        cell = database.create_cell("TOP")
        clean = database.create_category("clean_rule")
        failing = database.create_category("failing_rule")
        waived = database.create_category("waived_rule")
        database.create_item(cell, failing).add_value(pya.DBox(0, 0, 1, 1))
        item = database.create_item(cell, waived)
        item.add_value(pya.DBox(2, 0, 3, 1))
        item.add_tag(database.tag_id("waived"))
        self.view.show_rdb(rdb_index, self.cv.index())
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            app.process_events()
            time.sleep(0.01)
        tree = next(w for w in pya.QApplication.allWidgets()
                    if edit.value(w, "objectName") == "directory_tree")
        tree.expandAll()
        model = edit.value(tree, "model")
        def find(label, parent=pya.QModelIndex()):
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                if str(model.data(index, int(pya.Qt.DisplayRole))) == label:
                    return index
                result = find(label, index)
                if result is not None:
                    return result
        indices = {label: find(label) for label in ("clean_rule", "failing_rule", "waived_rule")}
        self.assertTrue(all(index is not None for index in indices.values()), repr(indices))
        # "Show All" may be off by default; make empty categories visible.
        for index in indices.values():
            tree.setRowHidden(edit.value(index, "row"), edit.value(index, "parent"), False)
        tree.setColumnWidth(0, 300)
        tree.expandAll()
        edit.value(tree, "window").show()
        def rendered_color(index, color=0x00c000):
            tree.doItemsLayout()
            tree.scrollTo(index)
            rect = tree.visualRect(index)
            self.assertTrue(rect.isValid(), repr((model.data(index, 0), edit.value(index, "row"),
                tree.isRowHidden(edit.value(index, "row"), edit.value(index, "parent")),
                model.data(edit.value(index, "parent"), 0))))
            viewport = edit.value(tree, "viewport")
            offset = edit.value(viewport, "pos")
            rect = rect.translated(offset)
            image = edit.value(tree.grab(), "toImage")
            width, height = edit.value(image, "width"), edit.value(image, "height")
            scale = edit.value(image, "devicePixelRatio")
            pixels = Counter(image.pixel(x, y) & 0xffffff
                             for y in range(max(0, int(rect.top * scale)), min(height, int((rect.bottom + 1) * scale)))
                             for x in range(max(0, int(rect.left * scale)), min(width, int((rect.right + 1) * scale))))
            return pixels[color]
        for theme in ("dark", "graphite", "solarized_dark", "light", "solarized_light"):
            ruby.eval_string("PocketThemes.apply('" + theme + "')")
            tree.clearSelection()
            for label in ("clean_rule", "waived_rule"):
                index = indices[label]
                self.assertGreater(rendered_color(index), 0, (theme, label))
                tree.setCurrentIndex(index)
                # Selection must remain readable, and deselecting restores green.
                self.assertTrue(edit.value(tree, "selectionModel").isSelected(index))
                self.assertGreater(rendered_color(index, 0xffffff), 0, (theme, label, "selected"))
                tree.clearSelection()
                self.assertGreater(rendered_color(index), 0, (theme, label, "deselected"))
            self.assertEqual(rendered_color(indices["failing_rule"]), 0, theme)

        # Exercise the native Qt hierarchy used by Salt without making network
        # requests or touching installed packages. A black model foreground
        # must be corrected here, while DRC foregrounds above remain native.
        salt = pya.QDialog(mw)
        salt.setObjectName("SaltManagerDialog")
        layout = pya.QVBoxLayout(salt)
        listing = pya.QTreeWidget(salt)
        listing.setHeaderHidden(True)
        layout.addWidget(listing)
        item = pya.QTreeWidgetItem(["Pocket Tools package"])
        item.setForeground(0, pya.QBrush(pya.QColor("#000000")))
        listing.addTopLevelItem(item)
        salt.resize(420, 140)
        salt.show()
        try:
            for theme, color in (("dark", 0xeeeeee), ("graphite", 0xf1f2f3),
                                 ("solarized_dark", 0xd2dddd), ("light", 0x20242a),
                                 ("solarized_light", 0x344c56)):
                ruby.eval_string("PocketThemes.apply('" + theme + "')")
                listing.clearSelection()
                image = edit.value(listing.grab(), "toImage")
                count = sum((image.pixel(x, y) & 0xffffff) == color
                            for y in range(edit.value(image, "height"))
                            for x in range(edit.value(image, "width")))
                self.assertGreater(count, 0, (theme, "Salt foreground"))
                listing.setCurrentItem(item)
                selected = edit.value(listing.grab(), "toImage")
                count = sum((selected.pixel(x, y) & 0xffffff) == 0xffffff
                            for y in range(edit.value(selected, "height"))
                            for x in range(edit.value(selected, "width")))
                self.assertGreater(count, 0, (theme, "Salt selected foreground"))
        finally:
            salt.hide()
            salt.deleteLater()

    def test_drc_description_contrast(self):
        ruby = pya.Interpreter.ruby_interpreter()
        if ruby is None:
            self.skipTest("Ruby unavailable")
        ruby.eval_string((ROOT / "macros/pocket_themes.rb").read_text(encoding="utf-8"))
        rdb_index = self.view.create_rdb("Theme contrast test")
        self.view.show_rdb(rdb_index, self.cv.index())
        panels = []
        for widget in pya.QApplication.allWidgets():
            if edit.value(widget, "objectName") != "info_text":
                continue
            parent = edit.value(widget, "parent")
            while parent is not None and edit.value(parent, "objectName") not in ("MarkerBrowserPage", "MarkerBrowserDialog"):
                parent = edit.value(parent, "parent")
            if parent is not None:
                panels.append(widget)
        self.assertTrue(panels, "Native DRC Info panel not found")
        panel = panels[0]
        original = edit.value(panel, "toHtml")
        html = ('<p style="color:blue; font-weight:bold">Width violation</p>'
                '<p style="color:red">Error</p><p style="color:gray">Comment</p>'
                '<a href="show-snapshot">Snapshot</a>')
        def color_at(text):
            cursor = panel.document.find(text)
            color = edit.value(edit.value(edit.value(cursor, "charFormat"), "foreground"), "color")
            return edit.value(color, "name")
        try:
            for theme, target in (("dark", "#8dbdf5"), ("graphite", "#a6c8f2"),
                                  ("solarized_dark", "#85c6ee"), ("light", "#0000ff"),
                                  ("solarized_light", "#0000ff")):
                panel.setHtml(html)
                ruby.eval_string("PocketThemes.apply('" + theme + "')")
                self.assertEqual(color_at("Width violation"), target)
                self.assertEqual(color_at("Error"), "#ff0000")
                self.assertEqual(color_at("Comment"), "#808080")
                self.assertTrue(edit.value(edit.value(panel.document.find("Snapshot"), "charFormat"), "isAnchor"))
            ruby.eval_string("PocketThemes.apply('dark')")
            self.assertEqual(color_at("Width violation"), "#8dbdf5")
            ruby.eval_string("PocketThemes.apply('light')")
            self.assertEqual(color_at("Width violation"), "#0000ff")
            ruby.eval_string("PocketThemes.apply('dark')")
            panel.setHtml(html.replace("Width violation", "New violation"))
            # No timer tick or event-loop iteration: the native text update
            # must already have the readable foreground before its first paint.
            ruby.eval_string("raise PocketThemes.instance_variable_get(:@drc_error) if PocketThemes.instance_variable_get(:@drc_error)")
            self.assertEqual(color_at("New violation"), "#8dbdf5")
            for i in range(5):
                panel.setHtml(html.replace("Width violation", "Violation " + str(i)))
                self.assertEqual(color_at("Violation " + str(i)), "#8dbdf5")
            self.assertGreaterEqual(edit.value(edit.value(panel.document.find("Violation 4"), "charFormat"), "fontWeight"), pya.QFont.Bold)
        finally:
            panel.setHtml(original)

    def setUp(self):
        mw.close_all()
        self.cv = mw.create_layout(1)
        self.view = mw.current_view()
        self.layout = self.cv.layout()
        self.cell = self.layout.create_cell("TOP")
        self.cv.cell = self.cell
        self.layer = self.layout.layer(1, 0)
        self.view.add_missing_layers()

    def tearDown(self):
        mw.close_all()

    def insert(self, geometry, layer=None):
        return self.cell.shapes(self.layer if layer is None else layer).insert(geometry)

    def select(self, shapes, seq=None):
        objects = []
        for i, shape in enumerate(shapes):
            obj = pya.ObjectInstPath()
            obj.top = self.cell.cell_index()
            obj.cv_index = self.cv.index()
            obj.shape = shape
            obj.layer = shape.layer
            obj.seq = i if seq is None else seq[i]
            objects.append(obj)
        self.view.object_selection = objects
        for obj in self.view.object_selection:
            self.assertTrue(edit.editable_shape(obj, self.view, self.cv, self.cell) is not None,
                repr(dict(cv_index=obj.cv_index, cv=self.cv.index(), source=obj.source,
                    cell=self.cell.cell_index(), valid=obj.is_valid(self.view),
                    shape_valid=obj.shape.is_valid(), owner=obj.shape.cell.cell_index(),
                    layer=obj.layer, shape_layer=obj.shape.layer,
                    array=obj.shape.is_array_member(), instance=obj.is_cell_inst())))
        self.assertEqual(sorted(obj.seq for obj in self.view.object_selection),
                         sorted(range(len(shapes)) if seq is None else seq))

    def region(self):
        return pya.Region(self.cell.shapes(self.layer)).merged()

    def undo(self):
        mw.call_menu("cm_undo")

    def test_boxify_geometry_and_undo(self):
        rect = pya.Polygon(pya.Box(0, 0, 100, 100))
        redundant = pya.Polygon([pya.Point(200, 0), pya.Point(200, 50),
            pya.Point(200, 100), pya.Point(250, 100), pya.Point(300, 100),
            pya.Point(300, 0)], True)
        self.assertGreater(redundant.num_points_hull(), 4)
        ell = pya.Polygon([pya.Point(0, 0), pya.Point(0, 100), pya.Point(50, 100),
            pya.Point(50, 50), pya.Point(100, 50), pya.Point(100, 0)])
        hole = rect.dup()
        hole.insert_hole([pya.Point(25, 25), pya.Point(75, 25),
                          pya.Point(75, 75), pya.Point(25, 75)])
        diamond = pya.Polygon([pya.Point(0, 50), pya.Point(50, 100),
                              pya.Point(100, 50), pya.Point(50, 0)])
        zero = pya.Polygon([pya.Point(0, 0), pya.Point(10, 0), pya.Point(20, 0)], True)
        for polygon, expected in ((rect, True), (redundant, True), (ell, False),
                                  (hole, False), (diamond, False), (zero, False)):
            self.assertEqual(edit.rectangle_box(polygon) is not None, expected)
        shapes = [self.insert(poly) for poly in (rect, redundant, ell, hole, diamond)]
        shapes[0].set_property("test", "preserve")
        shapes += [self.insert(pya.Box(400, 0, 450, 50)),
                   self.insert(pya.Text("skip", pya.Trans()))]
        self.select(shapes)
        before = self.region()
        self.assertEqual(edit.boxify(self.view), "boxify: 2 convertidos; 5 omitidos.")
        boxes = [s for s in self.cell.shapes(self.layer).each() if s.is_box()]
        self.assertEqual(len(boxes), 3)
        self.assertTrue(any(s.property("test") == "preserve" for s in boxes))
        self.assertTrue((before ^ self.region()).is_empty())
        self.undo()
        self.assertEqual(sum(s.is_polygon() for s in self.cell.shapes(self.layer).each()), 5)
        self.assertTrue((before ^ self.region()).is_empty())

    def pair(self, reverse=False, tie=False, disjoint=False):
        self.cell.shapes(self.layer).clear()
        a = pya.Box(0, 0, 100, 100)
        b = pya.Box(200 if disjoint else 50, 0, 300 if disjoint else 150, 100)
        shapes = [self.insert(a), self.insert(b)]
        self.select(shapes, [0, 0] if tie else [1, 0] if reverse else [0, 1])
        return pya.Region(a), pya.Region(b)

    def test_booleans_order_and_undo(self):
        for operation in ("union", "intersect", "difference", "reverse-difference", "xor"):
            for reverse in (False, True):
                with self.subTest(operation=operation, reverse=reverse):
                    a, b = self.pair(reverse=reverse)
                    if reverse:
                        a, b = b, a
                    expected = {"union": a | b, "intersect": a & b,
                        "difference": a - b, "reverse-difference": b - a, "xor": a ^ b}[operation]
                    edit.boolean(self.view, operation)
                    self.assertTrue((self.region() ^ expected).is_empty())
                    self.undo()
                    self.assertEqual(self.cell.shapes(self.layer).size(), 2)
                    self.assertTrue((self.region() ^ (a | b)).is_empty())

    def test_ties_empty_and_invalid(self):
        for operation in ("difference", "reverse-difference"):
            self.pair(tie=True)
            before = self.region()
            with self.assertRaisesRegex(ValueError, "Shift"):
                edit.boolean(self.view, operation)
            self.assertTrue((before ^ self.region()).is_empty())
            self.assertEqual(self.cell.shapes(self.layer).size(), 2)
        for operation in ("union", "intersect", "xor"):
            a, b = self.pair(tie=True)
            edit.boolean(self.view, operation)
            expected = {"union": a | b, "intersect": a & b, "xor": a ^ b}[operation]
            self.assertTrue((self.region() ^ expected).is_empty())
        self.pair(disjoint=True)
        self.assertIn("vacio", edit.boolean(self.view, "intersect"))
        self.assertEqual(self.cell.shapes(self.layer).size(), 2)
        other = self.layout.layer(2, 0)
        a = self.insert(pya.Box(0, 0, 10, 10))
        b = self.insert(pya.Box(0, 0, 10, 10), other)
        self.select([a, b])
        with self.assertRaisesRegex(ValueError, "misma capa"):
            edit.boolean(self.view, "union")

    def test_merge_and_stretch(self):
        self.assertIn("vacia", edit.execute("merge", self.view))
        edit.execute("stretch", self.view)
        self.assertEqual(self.view.mode_name(), "partial")
        edit.execute("stretch", self.view)
        self.assertEqual(self.view.mode_name(), "move")
        edit.execute("stretch", self.view)
        edit.execute("move", self.view)
        self.assertEqual(self.view.mode_name(), "move")
        a, b = self.pair()
        edit.execute("merge", self.view)
        self.assertEqual(self.cell.shapes(self.layer).size(), 1)
        self.assertTrue((self.region() ^ (a | b)).is_empty())
        self.undo()
        self.assertEqual(self.cell.shapes(self.layer).size(), 2)

    def test_instances_and_other_cells_are_skipped(self):
        child = self.layout.create_cell("CHILD")
        child_shape = child.shapes(self.layer).insert(pya.Polygon(pya.Box(0, 0, 100, 100)))
        instance = self.cell.insert(pya.CellInstArray(child.cell_index(), pya.Trans()))
        obj = pya.ObjectInstPath()
        obj.top = self.cell.cell_index()
        obj.cv_index = self.cv.index()
        obj.append_path(pya.InstElement(instance))
        obj.layer = self.layer
        obj.shape = child_shape
        self.assertTrue(obj.is_valid(self.view))
        self.assertIsNone(edit.editable_shape(obj, self.view, self.cv, self.cell))
        inst_path = pya.ObjectInstPath()
        inst_path.top = self.cell.cell_index()
        inst_path.cv_index = self.cv.index()
        inst_path.append_path(pya.InstElement(instance))
        self.assertTrue(inst_path.is_cell_inst())
        polygon = self.insert(pya.Polygon(pya.Box(200, 0, 300, 100)))
        self.select([polygon])
        self.view.object_selection = list(self.view.object_selection) + [obj, inst_path]
        self.assertEqual(edit.boxify(self.view), "boxify: 1 convertidos; 2 omitidos.")
        self.assertTrue(child_shape.is_polygon())
        self.view.object_selection = [obj, inst_path]
        with self.assertRaisesRegex(ValueError, "editables"):
            edit.boolean(self.view, "union")

    def test_console_menu_dispatch_multiline_and_version(self):
        namespace = runpy.run_path(str(ROOT / "pymacros/pocket_console.py"))
        console = pya._pocket_console_v1
        self.assertEqual(namespace["VERSION"], ET.parse(ROOT / "grain.xml").findtext("version"))
        self.assertEqual(console.tabs.tabText(0), "Python")
        self.assertEqual(console.tabs.currentIndex, 0)
        def submit(line):
            console.entries["python"].setText(line)
            console.execute_python()
        console.entries["python"].setText("bo")
        console.entries["python"].setCursorPosition(2)
        console.complete_pdk()
        self.assertEqual(edit.value(console.entries["python"], "text"), "boxify ")
        submit("boxify")
        self.assertEqual(console.histories["python"][-1], "boxify")
        submit("merge = 7")
        self.assertEqual(console.namespace["merge"], 7)
        submit("seen = []")
        submit("for i in range(3):")
        self.assertTrue(console.more)
        submit("    seen.append(i)")
        submit("")
        self.assertFalse(console.more)
        self.assertEqual(console.namespace["seen"], [0, 1, 2])
        submit("help")
        output = edit.value(console.outputs["python"], "toPlainText")
        for command in console.EDIT_COMMANDS:
            self.assertIn(command, output)
        mw.show()
        app.process_events()
        console.entries["python"].setFocus()
        submit("stretch")
        app.process_events()
        self.assertFalse(console.partial_notice.hidden)
        focused = edit.value(pya.QApplication, "focusWidget")
        self.assertIsNotNone(focused)
        self.assertEqual(edit.value(focused, "objectName"), "canvas")
        console.partial_exit.click()
        self.assertEqual(self.view.mode_name(), "move")
        self.assertTrue(console.partial_notice.hidden)
        # Switching modes through the native toolbar is reflected by the timer.
        self.view.switch_mode("partial")
        console.poll()
        self.assertFalse(console.partial_notice.hidden)
        self.view.switch_mode("select")
        console.poll()
        self.assertTrue(console.partial_notice.hidden)
        for action in console.console_actions.values():
            action.trigger()
        self.assertEqual(console.tabs.count, 0)
        self.assertTrue(console.dock.hidden)
        console.console_actions["python"].trigger()
        self.assertFalse(console.dock.hidden)
        self.assertEqual(console.tabs.tabText(0), "Python")
        self.assertEqual(console.namespace["seen"], [0, 1, 2])
        console.dock.close()
        self.assertTrue(all(not action.is_checked() for action in console.console_actions.values()))
        console.console_actions["python"].trigger()
        self.assertFalse(console.dock.hidden)
        ruby = pya.Interpreter.ruby_interpreter()
        if ruby is not None:
            ruby.eval_string((ROOT / "macros/pocket_themes.rb").read_text(encoding="utf-8"))
            self.assertTrue(mw.menu().is_valid("view_menu.pocket_tools.themes"))
            self.assertTrue(mw.menu().is_valid("view_menu.pocket_tools.consoles"))
        console.timer.stop()
        console.dock.hide()
        # The opposite startup order and console reload must preserve Themes.
        runpy.run_path(str(ROOT / "pymacros/pocket_console.py"))
        console = pya._pocket_console_v1
        if ruby is not None:
            self.assertTrue(mw.menu().is_valid("view_menu.pocket_tools.themes"))
        expected = ["Python"] + (["Ruby"] if ruby is not None else []) + [console.shell_name]
        self.assertEqual([console.tabs.tabText(i) for i in range(console.tabs.count)], expected)
        self.assertEqual(console.tabs.currentIndex, 0)
        console.timer.stop()
        console.dock.hide()


class LiveOutput(io.StringIO):
    def write(self, text):
        result = super().write(text)
        Path(release_report + ".log").write_text(self.getvalue(), encoding="utf-8")
        return result


output = LiveOutput()
try:
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(ReleaseChecks))
    report = dict(success=result.wasSuccessful(), tests=result.testsRun, output=output.getvalue())
except BaseException:
    report = dict(success=False, output=traceback.format_exc())
finally:
    Path(release_report).write_text(json.dumps(report), encoding="utf-8")
    app.exit(0)
