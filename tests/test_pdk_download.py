from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import pocket_pdk


SPEC = pocket_pdk.source_for("sky130")
archive = io.BytesIO()
with ZipFile(archive, "w") as contents:
    contents.writestr(
        f"sky130_klayout_pdk-{SPEC['revision']}/{SPEC['technology']}", b"<technology/>" + b" " * (128 * 1024)
    )
PAYLOAD = archive.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/slow")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/error":
            self.send_error(404)
            return
        self.send_response(200)
        if self.path != "/unknown":
            self.send_header("Content-Length", str(len(PAYLOAD) + (100 if self.path == "/broken" else 0)))
        self.end_headers()
        if self.path == "/slow":
            for start in range(0, len(PAYLOAD), 8192):
                self.wfile.write(PAYLOAD[start:start + 8192])
                self.wfile.flush()
                time.sleep(0.05)
        else:
            self.wfile.write(PAYLOAD)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="pdk download spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "source file.zip"
        self.curl = shutil.which("curl.exe" if sys.platform == "win32" else "curl")

    def test_curl_redirect_progress_and_paths_with_spaces(self):
        if not self.curl:
            self.skipTest("curl not available")
        events = []
        pocket_pdk.download_with_curl(self.curl, self.base + "/redirect", self.destination, events.append)
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertTrue(any(0 < e["downloaded"] < len(PAYLOAD) for e in events))
        self.assertEqual(events[-1], {"phase": "download", "downloaded": len(PAYLOAD), "total": len(PAYLOAD)})

    def test_curl_unknown_length(self):
        if not self.curl:
            self.skipTest("curl not available")
        events = []
        pocket_pdk.download_with_curl(self.curl, self.base + "/unknown", self.destination, events.append)
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertIsNone(events[-1]["total"])
        self.assertEqual(events[-1]["downloaded"], len(PAYLOAD))

    def test_urllib_fallback_without_curl(self):
        events = []
        with patch.object(pocket_pdk.shutil, "which", return_value=None):
            pocket_pdk.download_file(self.base + "/unknown", self.destination, events.append)
        self.assertEqual(self.destination.read_bytes(), PAYLOAD)
        self.assertIsNone(events[-1]["total"])

    def test_extract_only_after_successful_download(self):
        events = []
        download = pocket_pdk.download_file
        def local_download(url, destination, progress):
            download(self.base + "/known", destination, progress)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
            self.assertFalse((self.root / "package").exists())
        with patch.object(pocket_pdk, "download_file", side_effect=local_download):
            pocket_pdk.download_archive(SPEC, self.destination, self.root / "package", events.append)
        self.assertEqual(events[-1]["phase"], "extract")
        self.assertTrue((self.root / "package" / SPEC["technology"]).is_file())

    def test_failed_download_never_extracts_or_silently_falls_back(self):
        if not self.curl:
            self.skipTest("curl not available")
        download = pocket_pdk.download_file
        for endpoint in ("/error", "/broken"):
            events = []
            def local_download(url, destination, progress):
                download(self.base + endpoint, destination, progress)
            with patch.object(pocket_pdk, "download_file", side_effect=local_download):
                with patch.object(pocket_pdk, "download_with_urllib") as fallback:
                    with self.assertRaisesRegex(RuntimeError, "curl"):
                        pocket_pdk.download_archive(SPEC, self.destination, self.root / "package", events.append)
                    fallback.assert_not_called()
            self.assertFalse((self.root / "package").exists())
            self.assertNotIn("extract", [e["phase"] for e in events])


if __name__ == "__main__":
    unittest.main()
