import ast
from pathlib import Path
import queue
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_pdk


class ProgressTests(unittest.TestCase):
    def test_large_zip_reports_partial_bytes_and_completed_files(self):
        spec = pocket_pdk.source_for("sky130")
        prefix = "sky130_klayout_pdk-" + spec["revision"] + "/"
        payload = b"x" * (2 * 1024 * 1024 + 1)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source.zip"
            with ZipFile(archive, "w") as contents:
                contents.writestr(prefix, b"")
                contents.writestr(prefix + "large.bin", payload)
                contents.writestr(prefix + "empty.txt", b"")
            events = []
            with patch.object(pocket_pdk, "download_file"), patch.object(pocket_pdk, "archive_extractor", return_value=None):
                pocket_pdk.download_archive(spec, archive, root / "package", events.append)
            measured = [e for e in events if "completed" in e]
            self.assertTrue(any(0 < e["completed"] < len(payload) for e in measured))
            self.assertEqual(measured[-1], dict(phase="extract", completed=len(payload),
                                              total=len(payload), files=2, total_files=2))
            self.assertEqual((root / "package/large.bin").read_bytes(), payload)
            self.assertEqual([e["completed"] for e in measured], sorted(e["completed"] for e in measured))

    def test_local_copy_and_install_reach_full_progress(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source/gf180mcuD"
            technology = source / "libs.tech/klayout/tech/gf180mcu.lyt"
            technology.parent.mkdir(parents=True)
            technology.write_bytes(b"<technology/>")
            (technology.parent / "empty.txt").write_bytes(b"")
            events = []
            message = pocket_pdk.install(root, "gf180mcuD", source_dir=source, progress=events.append)
            copied = [e for e in events if e["phase"] == "copy"][-1]
            self.assertEqual(copied["files"], copied["total_files"])
            self.assertEqual(copied["files"], 2)
            self.assertEqual(copied["completed"], copied["total"])
            self.assertEqual(events[-1], dict(phase="install", completed=4, total=4))
            self.assertIn("Reinicia KLayout", message)

    def test_spinner_advances_without_worker_events_and_stops_at_completion(self):
        tree = ast.parse((ROOT / "pymacros/pocket_console.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PocketConsole")
        clock = [1.0]
        namespace = dict(time=SimpleNamespace(monotonic=lambda: clock[0]), queue=queue, PYTHON="python")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<console>", "exec"), namespace)
        console = namespace["PocketConsole"].__new__(namespace["PocketConsole"])
        labels, ranges, values, hidden, history = [], [], [], [], []
        console.pdk_installing = True
        console.events = queue.Queue()
        console.update_partial_notice = lambda: None
        console.pdk_progress_label = SimpleNamespace(setText=labels.append)
        console.pdk_progress_bar = SimpleNamespace(setRange=lambda *r: ranges.append(r), setValue=values.append)
        console.pdk_progress = SimpleNamespace(show=lambda: None, hide=lambda: hidden.append(True))
        console.write = lambda tab, message: history.append(message)
        console.update_pdk_progress(dict(phase="extract", completed=5, total=10, files=1, total_files=2))
        self.assertIn("50%", labels[-1])
        self.assertIn("1/2 archivos", labels[-1])
        self.assertEqual(values[-1], 50)
        self.assertEqual(ranges[-1], (0, 100))
        first = labels[-1]
        clock[0] += 0.1
        console.poll()
        self.assertEqual(labels[-1], first)
        clock[0] += 0.2
        console.poll()
        self.assertNotEqual(labels[-1][0], first[0])
        self.assertEqual(labels[-1][1:], first[1:])
        console.update_pdk_progress(dict(phase="remove", completed=3, total=6, files=3, total_files=6))
        self.assertIn("50%", labels[-1])
        self.assertIn("3/6 elementos", labels[-1])
        self.assertEqual(values[-1], 50)
        console.events.put(("pdk", "Done"))
        console.poll()
        count = len(labels)
        clock[0] += 1
        console.poll()
        self.assertEqual(len(labels), count)
        self.assertEqual(hidden, [True])
        self.assertEqual(history, ["Done"])


if __name__ == "__main__":
    unittest.main()
