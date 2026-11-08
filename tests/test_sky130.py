import contextlib
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
import pocket_pdk
import pocket_sky130


class Sky130DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.package = self.data / "salt/PocketPDK_sky130"
        self.technology = self.package / "tech/sky130/sky130.lyt"
        self.macro = self.technology.parent / "pymacros/sky130_pcells.lym"
        self.macro.parent.mkdir(parents=True)
        self.technology.write_text("<technology><name>sky130</name></technology>")
        self.macro.write_text('''<?xml version="1.0" encoding="utf-8"?>
<!-- Copyright upstream. Apache License 2.0. -->
<klayout-macro><autorun>true</autorun><interpreter>python</interpreter><text>
import pya
pya.runs += 1
pya.library = pya.make_library()
</text></klayout-macro>
''', encoding="utf-8")
        self.runtime = ModuleType("pya")
        self.runtime.library = None
        self.runtime.runs = 0
        self.lookups = []
        def library_by_name(name, technology):
            self.lookups.append((name, technology))
            return self.runtime.library if name == "skywater130" and technology == "sky130" else None
        self.runtime.Library = SimpleNamespace(library_by_name=library_by_name,
                                               library_names=lambda: ["Basic", "CIDESI", "CM05_ResLib"])
        self.runtime.make_library = lambda: SimpleNamespace(
            layout=lambda: SimpleNamespace(pcell_names=lambda: ["nfet", "pfet"]))

    def test_installed_technology_and_unrelated_libraries_do_not_imply_sky130_pcells(self):
        result = pocket_sky130.diagnostics(self.technology, self.runtime,
                                          importer=lambda name: ModuleType(name))
        self.assertTrue(result["technology_installed"])
        self.assertTrue(result["macro_present"])
        self.assertTrue(result["autorun"])
        self.assertFalse(result["registered"])
        self.assertIsNone(result["startup"])
        self.assertEqual(self.lookups, [("skywater130", "sky130")])
        self.assertEqual(self.runtime.runs, 0, "Status must never run the macro")

    def test_concrete_nested_import_error_and_traceback_are_preserved(self):
        def broken_import(name):
            raise ImportError("DLL load failed while importing orjson: incompatible ABI")
        result = pocket_sky130.diagnostics(self.technology, self.runtime, broken_import)
        self.assertIn("DLL load failed", result["import_error"])
        self.assertIn("orjson", result["import_traceback"])

    def test_startup_instrumentation_records_original_failure_without_exiting_klayout(self):
        pocket_sky130.instrument_startup(self.package)
        text = self.macro.read_text(encoding="utf-8")
        self.assertIn("Copyright upstream. Apache License 2.0.", text)
        self.assertEqual(ET.fromstring(text).findtext("autorun"), "true")
        code = ET.fromstring(text).findtext("text")
        with patch.dict(sys.modules, {"pya": self.runtime, "gdsfactory": None}):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                exec(compile(code, str(self.macro), "exec"), {"__file__": str(self.macro)})
        state = self.runtime._pocket_sky130_startup
        self.assertTrue(state["attempted"])
        self.assertFalse(state["success"])
        self.assertIn("ModuleNotFoundError", state["error"])
        self.assertIn("gdsfactory", output.getvalue())
        self.assertEqual(self.runtime.runs, 0)
        pocket_sky130.instrument_startup(self.package)
        self.assertEqual(self.macro.read_text(encoding="utf-8"), text, "Instrumentation must be idempotent")

    def test_successful_upstream_registration_is_observed_separately(self):
        pocket_sky130.instrument_startup(self.package)
        code = ET.parse(self.macro).getroot().findtext("text")
        module = ModuleType("gdsfactory")
        with patch.dict(sys.modules, {"pya": self.runtime, "gdsfactory": module}):
            exec(compile(code, str(self.macro), "exec"), {"__file__": str(self.macro)})
        result = pocket_sky130.diagnostics(self.technology, self.runtime, importer=lambda name: module)
        self.assertTrue(result["registered"])
        self.assertTrue(result["startup"]["success"])
        self.assertEqual(set(result["pcells"]), {"nfet", "pfet"})
        self.assertEqual(self.runtime.runs, 1)

    def test_incomplete_library_is_reported_without_claiming_transistors_work(self):
        self.runtime.library = SimpleNamespace(layout=lambda: SimpleNamespace(pcell_names=lambda: ["resistor"]))
        with patch.object(pocket_sky130.importlib, "import_module", return_value=ModuleType("gdsfactory")):
            rows = dict(pocket_pdk.status_details(self.data, "sky130", runtime=self.runtime))
        self.assertEqual(rows["Tecnologia SKY130"], "Instalada")
        self.assertIn("faltan nfet/pfet", rows["PCells SKY130"])
        self.assertIn("no basta", rows["Prueba funcional"])

    def test_missing_macro_is_not_silently_accepted(self):
        self.macro.unlink()
        with self.assertRaisesRegex(ValueError, "macro de PCells"):
            pocket_sky130.instrument_startup(self.package)
        self.assertFalse(pocket_sky130.diagnostics(self.technology)["macro_present"])


if __name__ == "__main__":
    unittest.main()
