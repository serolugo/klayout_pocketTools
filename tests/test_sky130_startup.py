import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


class Sky130StartupTests(unittest.TestCase):
    def test_fresh_start_exposes_real_pcells_result_without_counting_other_libraries(self):
        executable = Path.home() / "AppData/Roaming/KLayout/klayout_app.exe"
        macro = Path.home() / "KLayout/salt/PocketPDK_sky130/tech/sky130/pymacros/sky130_pcells.lym"
        if not executable.is_file() or not macro.is_file():
            self.skipTest("Installed Windows KLayout/SKY130 not available")
        if "# Pocket Tools: SKY130 startup diagnostics v1" not in macro.read_text(encoding="utf-8"):
            self.skipTest("Installed SKY130 predates startup diagnostics")
        with TemporaryDirectory(prefix="sky130-startup-test-") as directory:
            report = Path(directory) / "result.json"
            checks = Path(__file__).with_name("klayout_sky130_acceptance.py")
            process = subprocess.run([
                str(executable), "-z", "-t", "-r", str(checks),
                "-rd", "sky130_report=" + str(report),
            ], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=40)
            self.assertEqual(process.returncode, 0, process.stderr[-2000:])
            self.assertTrue(report.is_file(), process.stderr[-2000:])
            outcome = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(outcome["selected_technology"], "sky130")
            diagnostic = outcome["diagnostics"]
            self.assertTrue(diagnostic["technology_installed"])
            self.assertTrue(diagnostic["macro_present"])
            self.assertTrue(diagnostic["autorun"])
            self.assertTrue(diagnostic["startup"]["attempted"])
            if outcome["accepted"]:
                self.assertTrue(diagnostic["registered"])
                self.assertIn("transistor_bbox", outcome)
            else:
                # This tests truthful diagnostics, NOT the functional acceptance criterion.
                self.assertTrue(diagnostic["import_error"] or diagnostic["startup"].get("error")
                                or outcome.get("error"), outcome)
                if not diagnostic["registered"]:
                    self.assertIn("NO registrada: skywater130", outcome["status"])
                if diagnostic["import_error"]:
                    self.assertIn("ERROR:", outcome["status"])
                    self.assertIn(diagnostic["import_error"].split(":", 1)[0], outcome["status"])


if __name__ == "__main__":
    unittest.main()
