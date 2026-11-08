from pathlib import Path
from stat import S_IFLNK
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo, ZIP_STORED


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import pocket_pdk


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="pdk extract spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "source file.zip"
        self.package = self.root / "package files"
        self.spec = pocket_pdk.source_for("sky130")
        self.prefix = "sky130_klayout_pdk-" + self.spec["revision"] + "/"

    def test_native_zip_extraction_preserves_contents_and_reports_completion(self):
        if not pocket_pdk.archive_extractor():
            self.skipTest("bsdtar not available")
        with ZipFile(self.archive, "w") as archive:
            archive.writestr(self.prefix, b"")
            archive.writestr(self.prefix + self.spec["technology"], b"<technology/>")
            archive.writestr(self.prefix + "nested/empty.txt", b"")
            archive.writestr(self.prefix + "nested/spaces in name.bin", b"x" * 1048576)
        events = []
        with patch.object(pocket_pdk, "download_file"):
            pocket_pdk.download_archive(self.spec, self.archive, self.package, events.append)
        self.assertEqual((self.package / self.spec["technology"]).read_bytes(), b"<technology/>")
        self.assertEqual((self.package / "nested/spaces in name.bin").read_bytes(), b"x" * 1048576)
        self.assertTrue((self.package / "nested/empty.txt").is_file())
        self.assertEqual(events[-1]["completed"], events[-1]["total"])
        self.assertEqual(events[-1]["files"], 3)
        self.assertEqual(events[-1]["total_files"], 3)

    def test_all_paths_validated_before_any_extraction(self):
        for bad in ("../escape", "C:/escape", "nested\\escape", "/absolute"):
            with self.subTest(path=bad):
                with ZipFile(self.archive, "w") as archive:
                    archive.writestr(self.prefix + "safe.txt", b"safe")
                    entry = ZipInfo(self.prefix + bad)
                    entry.filename = bad if bad.startswith("/") else self.prefix + bad
                    archive.writestr(entry, b"unsafe")
                with patch.object(pocket_pdk, "download_file"):
                    with self.assertRaises(ValueError):
                        pocket_pdk.download_archive(self.spec, self.archive, self.package)
                self.assertFalse(self.package.exists())

    def test_symlink_rejected_before_extraction(self):
        link = ZipInfo(self.prefix + "link")
        link.create_system = 3
        link.external_attr = (S_IFLNK | 0o777) << 16
        with ZipFile(self.archive, "w") as archive:
            archive.writestr(self.prefix + "safe.txt", b"safe")
            archive.writestr(link, "../../outside")
        with patch.object(pocket_pdk, "download_file"):
            with self.assertRaises(ValueError):
                pocket_pdk.download_archive(self.spec, self.archive, self.package)
        self.assertFalse(self.package.exists())

    def test_native_corrupt_zip_fails_without_reporting_success(self):
        if not pocket_pdk.archive_extractor():
            self.skipTest("bsdtar not available")
        with ZipFile(self.archive, "w", compression=ZIP_STORED) as archive:
            archive.writestr(self.prefix + "data.txt", b"UNIQUE_PAYLOAD")
        data = self.archive.read_bytes().replace(b"UNIQUE_PAYLOAD", b"BROKEN_PAYLOAD")
        self.archive.write_bytes(data)
        events = []
        with patch.object(pocket_pdk, "download_file"):
            with self.assertRaisesRegex(RuntimeError, "tar"):
                pocket_pdk.download_archive(self.spec, self.archive, self.package, events.append)
        self.assertFalse(any(e.get("files") == 1 for e in events))


if __name__ == "__main__":
    unittest.main()
