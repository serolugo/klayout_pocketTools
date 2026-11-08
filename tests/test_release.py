import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class ReleaseTests(unittest.TestCase):
    def test_native_release_checks(self):
        executable = shutil.which("klayout")
        windows = Path.home() / "AppData/Roaming/KLayout/klayout_app.exe"
        if not executable and windows.is_file():
            executable = str(windows)
        if not executable:
            self.skipTest("KLayout not available")
        with TemporaryDirectory(prefix="pocket-release-") as temporary:
            report = Path(temporary) / "report.json"
            checks = Path(__file__).with_name("klayout_release_checks.py")
            result = subprocess.run([
                executable, "-e", "-z", "-nc", "-rx", "-r", str(checks),
                "-rd", "release_report=" + str(report),
            ], env=dict(os.environ, QT_QPA_PLATFORM="offscreen", KLAYOUT_HOME=temporary),
                capture_output=True, text=True, timeout=50)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(report.is_file(), result.stdout + result.stderr)
            outcome = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(outcome["success"], outcome["output"])


if __name__ == "__main__":
    unittest.main()
