import ast
import code
import contextlib
import io
import os
from pathlib import Path
import queue
import shlex
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_pdk


class ConsoleUXTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / "pymacros/pocket_console.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PocketConsole")
        self.launch = Mock()
        self.app = SimpleNamespace(applicationFilePath=sys.executable, is_editable=lambda: True)
        self.ns = dict(pocket_pdk=pocket_pdk, shlex=shlex, Path=Path, queue=queue,
                       subprocess=SimpleNamespace(Popen=self.launch), PYTHON="python",
                       qt_value=lambda obj, name: getattr(obj, name), orange_prompt=lambda text: text,
                       code=code, contextlib=contextlib, io=io, os=os, sys=sys)
        self.ns["pya"] = SimpleNamespace(Application=SimpleNamespace(instance=lambda: self.app))
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<console>", "exec"), self.ns)
        self.console = self.ns["PocketConsole"].__new__(self.ns["PocketConsole"])
        self.console.pdk_installing = self.console.shell_busy = self.console.more = False
        self.console.restart_pending = False
        self.console.cwd = str(ROOT)
        self.console.mw = SimpleNamespace(close=Mock(return_value=True), show=Mock())

    def test_restart_requires_yes_and_honors_save_cancellation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.console.dispatch_console("restart")
            self.launch.assert_not_called()
            self.assertTrue(self.console.restart_pending)
            self.console.dispatch_console("n")
            self.assertFalse(self.console.restart_pending)
            self.launch.assert_not_called()
            self.console.dispatch_console("pdk restart")
            self.console.mw.close.return_value = False
            self.console.dispatch_console("y")
            self.launch.assert_not_called()
            self.console.dispatch_console("restart")
            self.console.mw.close.return_value = True
            self.console.dispatch_console("y")
            self.launch.assert_called_once_with([sys.executable, "-e"], cwd=str(ROOT))

    def test_restart_blocked_while_busy_and_invalid_args_do_not_restart(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.console.pdk_installing = True
            self.console.dispatch_console("restart")
            self.assertFalse(self.console.restart_pending)
            self.console.restart_klayout()
            self.console.pdk_installing = False
            self.console.shell_busy = True
            self.console.dispatch_console("restart")
            self.assertFalse(self.console.restart_pending)
            self.console.shell_busy = False
            self.console.dispatch_console("pdk restart extra")
            self.console.dispatch_console("restart extra")
        self.assertIn("Sintaxis", output.getvalue())
        self.launch.assert_not_called()
        self.console.mw.close.assert_not_called()

    def test_launcher_failure_restores_the_window(self):
        self.launch.side_effect = OSError("launcher unavailable")
        with self.assertRaises(OSError):
            self.console.restart_klayout()
        self.console.mw.show.assert_called_once()

    def test_invalid_pdk_shows_readable_error_without_traceback(self):
        self.app.application_data_path = lambda: str(ROOT)
        self.app.klayout_path = lambda: []
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.console.dispatch_console("pdk status unknown-pdk")
        self.assertIn("ERROR", output.getvalue())
        self.assertIn("PDK desconocido", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_other_python_input_defers_restart_and_multiline_is_unchanged(self):
        console = self.console
        entry = SimpleNamespace(text="", clear=lambda: None)
        console.entries = {"python": entry}
        console.prompts = {"python": SimpleNamespace(setText=lambda text: None)}
        console.histories = {"python": []}
        console.positions = {"python": 0}
        console.namespace = {}
        console.console = code.InteractiveConsole(console.namespace)
        console.python_busy = False
        history = []
        console.write = lambda tab, message: history.append(message)
        def submit(text):
            entry.text = text
            console.execute_python()
        console.restart_pending = True
        submit("answer = 42")
        self.assertFalse(console.restart_pending)
        self.assertEqual(console.namespace["answer"], 42)
        submit('text = """')
        self.assertTrue(console.more)
        console.restart_pending = True
        submit("y")
        submit('"""')
        self.assertEqual(console.namespace["text"], "\ny\n")
        self.assertTrue(console.restart_pending)
        submit("n")
        self.assertFalse(console.restart_pending)
        self.assertIn("reiniciar [y/n] >> n", history)
        self.assertEqual(console.histories["python"][-1], "n")
        self.launch.assert_not_called()

    def test_tables_wrap_and_status_distinguishes_managed_and_external(self):
        table = pocket_pdk.ascii_table(("Campo", "Valor"), [("Ruta", "C:/My PDKs/" + "a" * 100), ("Resultado", "line 1\nline 2")])
        self.assertEqual(len(set(map(len, table.splitlines()))), 1)
        self.assertIn("line 2", table)
        self.assertIn("+", pocket_pdk.ascii_table(("Empty",), []))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            details = dict(pocket_pdk.status_details(root, "sky130"))
            self.assertEqual(details["Estado"], "Disponible")
            spec = pocket_pdk.source_for("sky130")
            managed = root / "salt" / spec["folder"]
            technology = managed / spec["technology"]
            technology.parent.mkdir(parents=True)
            technology.write_text("technology")
            (managed / pocket_pdk.REVISION_FILE).write_text("revision")
            details = dict(pocket_pdk.status_details(root, "sky130"))
            self.assertEqual(details["Estado"], "Instalado")
            self.assertEqual(details["Ruta"], str(managed))
            self.assertIn("Instalado", pocket_pdk.list_table(root))

    def test_pdk_autocomplete_cycles_and_preserves_existing_arguments(self):
        console = self.console
        console.pdk_completion_cache = None
        class Entry:
            text = ""
            cursorPosition = 0
            def setText(self, text):
                self.text = text
            def setCursorPosition(self, cursor):
                self.cursorPosition = cursor
        entry = Entry()
        console.entries = {"python": entry}
        suggestions = []
        console.write = lambda tab, text: suggestions.append(text)
        self.ns["utf16_length"] = lambda text: len(text.encode("utf-16-le")) // 2
        def edit(text, cursor=None):
            entry.text = text
            entry.cursorPosition = len(text.encode("utf-16-le")) // 2 if cursor is None else cursor
        edit("pd")
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk ")
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk help ")
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk list ")
        console.complete_pdk(-1)
        self.assertEqual(entry.text, "pdk help ")
        edit("pdk ins")
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk install ")
        console.complete_pdk(-1)
        self.assertEqual(entry.text, "pdk install gf180mcuD ")
        edit("pdk install sky130 --so")
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk install sky130 --source ")
        suffix = ' --source "C:\\My PDKs U0001f680"'
        edit("pdk install sk130" + suffix, len("pdk install sk"))
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk install sky130" + suffix)
        for text in ('pdk("install", "sky")', 'pdk install sky130 --source "C:\\My PDKs"'):
            edit(text)
            console.complete_pdk()
            self.assertEqual(entry.text, text)
        edit("pdk sta")
        console.more = True
        console.complete_pdk()
        self.assertEqual(entry.text, "pdk sta")
        self.assertTrue(suggestions)

    def test_edit_command_autocomplete_and_python_is_preserved(self):
        console = self.console
        console.pdk_completion_cache = None
        entry = SimpleNamespace(text="", cursorPosition=0)
        entry.setText = lambda text: setattr(entry, "text", text)
        entry.setCursorPosition = lambda cursor: setattr(entry, "cursorPosition", cursor)
        console.entries = {"python": entry}
        console.write = Mock()
        self.ns["utf16_length"] = lambda text: len(text.encode("utf-16-le")) // 2
        def complete(text, direction=1, cursor=None):
            entry.text = text
            entry.cursorPosition = len(text) if cursor is None else cursor
            console.complete_pdk(direction)
            return entry.text
        for command in ("help",) + console.EDIT_COMMANDS + ("restart",):
            console.pdk_completion_cache = None
            self.assertEqual(complete(command[:3]), command + " ")
        self.assertEqual(complete("re"), "reverse-difference ")
        console.complete_pdk()
        self.assertEqual(entry.text, "restart ")
        console.complete_pdk(-1)
        self.assertEqual(entry.text, "reverse-difference ")
        self.assertEqual(complete("boxfy", cursor=3), "boxify ")
        for text in ("boxify()", "merge = 1", "print('union')", "obj.merge", "union extra"):
            self.assertEqual(complete(text), text)
        console.pdk_completion_cache = None
        self.assertEqual(complete(""), "help ")
        console.more = True
        self.assertEqual(complete("    bo"), "    bo")


if __name__ == "__main__":
    unittest.main()
