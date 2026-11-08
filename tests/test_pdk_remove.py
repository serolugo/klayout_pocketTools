import ast
import contextlib
import io
import json
from pathlib import Path
import queue
import shlex
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_pdk


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve()
        self.spec = pocket_pdk.source_for("sky130")
        self.target = self.data / "salt" / self.spec["folder"]

    def package(self):
        self.target.mkdir(parents=True)
        (self.target / pocket_pdk.REVISION_FILE).write_text("revision")
        return self.target

    def test_remove_managed_package_preserves_other_files(self):
        target = self.package()
        (target / "partial-install").write_text("incomplete technology is removable")
        neighbor = self.data / "salt" / "another-package"
        neighbor.mkdir()
        events = []
        message = pocket_pdk.remove(self.data, "SKY130", progress=events.append)
        self.assertFalse(target.exists())
        self.assertTrue(neighbor.is_dir())
        self.assertTrue(all(event["phase"] == "remove" for event in events))
        self.assertEqual(events[-1]["completed"], events[-1]["total"])
        self.assertEqual(events[-1]["files"], events[-1]["total_files"])
        self.assertIn("Reinicia KLayout", message)
        self.assertIn("no está instalado", pocket_pdk.remove(self.data, "sky130"))

    def test_external_installation_is_preserved(self):
        technology = self.data / self.spec["technology"]
        technology.parent.mkdir(parents=True)
        technology.write_text("external")
        self.assertIn("instalación externa", pocket_pdk.remove(self.data, "sky130"))
        self.assertEqual(technology.read_text(), "external")

    def test_deletion_progress_counts_files_and_directories_monotonically(self):
        target = self.package()
        child = target / "nested"
        child.mkdir()
        (child / "payload.txt").write_text("payload")
        events = []
        pocket_pdk.remove_package_worker(self.data, "sky130", events.append)
        self.assertFalse(target.exists())
        self.assertEqual([e["completed"] for e in events], [0, 1, 2, 3, 4])
        self.assertTrue(all(e["total"] == 4 and e["total_files"] == 4 for e in events))

    def test_unmarked_directory_is_preserved(self):
        self.target.mkdir(parents=True)
        with self.assertRaises(ValueError):
            pocket_pdk.remove(self.data, "sky130")
        self.assertTrue(self.target.is_dir())

    def test_interrupted_removal_can_retry_after_marker_is_gone(self):
        target = self.package()
        def interrupted(path, progress):
            (path / pocket_pdk.REVISION_FILE).unlink()
            raise PermissionError("locked file")
        with patch.object(pocket_pdk, "remove_tree_progress", side_effect=interrupted):
            with self.assertRaises(PermissionError):
                pocket_pdk.remove_package_worker(self.data, "sky130")
        record = pocket_pdk.removal_record(self.data, "sky130")
        self.assertTrue(record.is_file())
        self.assertIn("Eliminacion pendiente", pocket_pdk.list_table(self.data))
        with self.assertRaisesRegex(RuntimeError, "pendiente"):
            pocket_pdk.install(self.data, "sky130")
        pocket_pdk.remove(self.data, "sky130")
        self.assertFalse(target.exists())
        self.assertFalse(record.exists())

    def test_wrong_journal_cannot_authorize_removal(self):
        self.target.mkdir(parents=True)
        record = pocket_pdk.removal_record(self.data, "sky130")
        record.write_text(json.dumps(dict(version=1, name="sky130", target=str(self.data / "outside"))))
        with self.assertRaises(ValueError):
            pocket_pdk.remove(self.data, "sky130")
        self.assertTrue(self.target.is_dir())

    def test_recovery_when_target_already_deleted(self):
        record = pocket_pdk.removal_record(self.data, "sky130")
        record.write_text(json.dumps(dict(version=1, name="sky130", target=str(self.target))))
        self.assertIn("Reinicia KLayout", pocket_pdk.remove(self.data, "sky130"))
        self.assertFalse(record.exists())

    def legacy_package(self):
        cache = self.data / "ppdk-fixture/source.zip"
        cache.parent.mkdir()
        technology = self.target / self.spec["technology"]
        technology.parent.mkdir(parents=True)
        technology.write_bytes(b"<technology/>")
        prefix = "sky130_klayout_pdk-" + self.spec["revision"] + "/"
        with ZipFile(cache, "w") as archive:
            archive.writestr(prefix + self.spec["technology"], b"<technology/>")
            archive.writestr(prefix + "already-deleted.txt", b"deleted")
        return technology

    def test_legacy_partial_removal_matches_cached_zip_before_deleting(self):
        self.legacy_package()
        # An earlier incomplete download must not prevent finding a valid ZIP.
        broken = self.data / "ppdk-broken/source.zip"
        broken.parent.mkdir()
        broken.write_bytes(b"incomplete")
        pocket_pdk.remove(self.data, "sky130")
        self.assertFalse(self.target.exists())

    def test_legacy_modified_content_is_preserved(self):
        technology = self.legacy_package()
        technology.write_bytes(b"<changed-xxx>")
        with self.assertRaisesRegex(RuntimeError, "identificada"):
            pocket_pdk.remove(self.data, "sky130")
        self.assertTrue(technology.is_file())
        self.assertFalse(pocket_pdk.removal_record(self.data, "sky130").exists())

    def test_real_klayout_batch_helper(self):
        executable = Path.home() / "AppData/Roaming/KLayout/klayout_app.exe"
        if not executable.is_file():
            self.skipTest("KLayout executable not available")
        self.package()
        (self.target / "with spaces.txt").write_text("payload")
        pocket_pdk.remove(self.data, "sky130", klayout_executable=executable)
        self.assertFalse(self.target.exists())

    def test_unknown_id_is_rejected(self):
        with self.assertRaises(ValueError):
            pocket_pdk.remove(self.data, "../outside")

    def test_catalog_path_cannot_escape_salt(self):
        outside = self.data / "outside"
        outside.mkdir()
        (outside / pocket_pdk.REVISION_FILE).write_text("revision")
        with patch.dict(self.spec, folder="../outside"):
            with self.assertRaises(ValueError):
                pocket_pdk.remove(self.data, "sky130")
        self.assertTrue(outside.exists())

    def test_directory_link_is_preserved(self):
        outside = self.data / "outside"
        outside.mkdir()
        (outside / pocket_pdk.REVISION_FILE).write_text("revision")
        self.target.parent.mkdir()
        try:
            self.target.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks unavailable for this user")
        with self.assertRaises(ValueError):
            pocket_pdk.remove(self.data, "sky130")
        self.assertTrue(outside.exists())

    def test_imported_source_is_preserved(self):
        source = self.data / "My PDKs" / "gf180mcuD"
        technology = source / "libs.tech/klayout/tech/gf180mcu.lyt"
        technology.parent.mkdir(parents=True)
        technology.write_text("local technology")
        pocket_pdk.install(self.data, "gf180mcuD", source_dir=source)
        pocket_pdk.remove(self.data, "gf180mcuD")
        self.assertEqual(technology.read_text(), "local technology")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_directory_junction_is_preserved(self):
        import _winapi
        outside = self.data / "outside"
        outside.mkdir()
        (outside / pocket_pdk.REVISION_FILE).write_text("revision")
        self.target.parent.mkdir()
        _winapi.CreateJunction(str(outside), str(self.target))
        with self.assertRaises(ValueError):
            pocket_pdk.remove(self.data, "sky130")
        self.assertTrue((outside / pocket_pdk.REVISION_FILE).is_file())

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_external_process_does_not_follow_nested_junctions(self):
        import _winapi
        target = self.package()
        outside = self.data / "external-source"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("keep")
        _winapi.CreateJunction(str(outside), str(target / "linked"))
        pocket_pdk.remove(self.data, "sky130")
        self.assertFalse(target.exists())
        self.assertEqual(sentinel.read_text(), "keep")


class ConsoleRemoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load console methods without starting KLayout or creating Qt widgets.
        tree = ast.parse((ROOT / "pymacros/pocket_console.py").read_text(encoding="utf-8"))
        console = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "PocketConsole")
        cls.namespace = dict(Path=Path, shlex=shlex, time=time, queue=queue,
                             pocket_pdk=pocket_pdk, PYTHON="python")
        exec(compile(ast.Module(body=[console], type_ignores=[]), "<console>", "exec"), cls.namespace)

    def test_dispatch_and_argument_validation(self):
        console = self.namespace["PocketConsole"].__new__(self.namespace["PocketConsole"])
        calls = []
        console.pdk = lambda *args, **kwargs: calls.append((args, kwargs))
        self.assertTrue(console.dispatch_pdk("pdk remove sky130"))
        self.assertEqual(calls, [(("remove", "sky130"), {})])
        for text in ("pdk remove", "pdk remove sky130 extra", "pdk remove sky130 --source local"):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertTrue(console.dispatch_pdk(text))
            self.assertIn("Sintaxis:", output.getvalue())
        self.assertEqual(len(calls), 1)
        self.assertFalse(console.dispatch_pdk('pdk("remove", "sky130")'))

    def test_worker_result_and_ui_cleanup(self):
        with TemporaryDirectory() as directory:
            app = SimpleNamespace(application_data_path=lambda: directory, klayout_path=lambda: [],
                                  applicationFilePath="mock-klayout")
            self.namespace["qt_value"] = lambda obj, name: getattr(obj, name)
            self.namespace["pya"] = SimpleNamespace(Application=SimpleNamespace(instance=lambda: app))
            workers = []
            class Thread:
                def __init__(self, target, daemon):
                    self.target = target
                def start(self):
                    workers.append(self.target)
            self.namespace["threading"] = SimpleNamespace(Thread=Thread)
            for fail in (False, True):
                console = self.namespace["PocketConsole"].__new__(self.namespace["PocketConsole"])
                console.pdk_installing = False
                console.shell_busy = False
                console.more = False
                console.prompts = {"python": SimpleNamespace(setText=lambda text: None)}
                self.namespace["orange_prompt"] = lambda text: text
                console.events = queue.Queue()
                console.update_partial_notice = lambda: None
                phases, hidden, history = [], [], []
                console.update_pdk_progress = phases.append
                console.pdk_progress = SimpleNamespace(hide=lambda: hidden.append(True))
                console.write = lambda tab, message: history.append(message)
                with contextlib.redirect_stdout(io.StringIO()):
                    # This exercises the Python API used by pdk("remove", id).
                    console.pdk("remove", "gf180mcuD")
                    self.assertTrue(console.pdk_installing)
                    console.pdk("install", "sky130")
                self.assertEqual(len(workers), 1, "Concurrent operations must be blocked")
                def remove(*args, progress, klayout_executable):
                    progress({"phase": "remove"})
                    if fail:
                        raise OSError("access denied")
                    return "Eliminado. Reinicia KLayout."
                with patch.object(pocket_pdk, "remove", side_effect=remove):
                    workers.pop()()
                self.assertEqual(phases, [{"phase": "prepare"}], "Worker must only queue events")
                console.poll()
                self.assertEqual(phases[-1], {"phase": "remove"})
                self.assertFalse(console.pdk_installing)
                self.assertEqual(hidden, [True])
                self.assertEqual(len(history), 1 if fail else 2)
                self.assertIn("No se pudo eliminar" if fail else "Reinicia KLayout", history[0])
                with self.assertRaises(ValueError):
                    console.pdk("remove", "sky130", source="unused")


if __name__ == "__main__":
    unittest.main()
