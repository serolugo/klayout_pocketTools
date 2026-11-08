import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class LayerSearchIntegrationTests(unittest.TestCase):
    def test_native_layer_search_in_isolated_klayout(self):
        executable = shutil.which("klayout")
        windows_executable = Path.home() / "AppData/Roaming/KLayout/klayout_app.exe"
        if not executable and windows_executable.is_file():
            executable = str(windows_executable)
        if not executable:
            self.skipTest("KLayout executable not available")
        checks = Path(__file__).with_name("klayout_layer_search_checks.py")
        with TemporaryDirectory(prefix="pocket-layer-search-") as temporary:
            report = Path(temporary) / "report.json"
            process = subprocess.run([
                executable, "-z", "-nc", "-rx", "-r", str(checks),
                "-rd", "layer_search_report=" + str(report),
            ], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(report.is_file(), process.stdout + process.stderr)
            outcome = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(outcome["success"], outcome["output"])


if __name__ == "__main__":
    unittest.main()
