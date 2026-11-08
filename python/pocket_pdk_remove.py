"""Private deletion helper for Python or KLayout batch mode (no GUI)."""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pocket_pdk


def main(request_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    try:
        with Path(request["progress"]).open("a", encoding="utf-8") as stream:
            last_update = [None]

            def progress(event):
                now = time.monotonic()
                if (last_update[0] is None or now - last_update[0] >= 0.25
                        or event["completed"] == event["total"]):
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                    last_update[0] = now

            pocket_pdk.remove_package_worker(request["data_dir"], request["name"], progress)
        outcome = {"success": True}
    except Exception as exc:
        outcome = {"success": False, "error": str(exc)}
    Path(request["result"]).write_text(json.dumps(outcome), encoding="utf-8")


# KLayout injects -rd variables into the script's globals.
if "pocket_remove_request" in globals():
    main(pocket_remove_request)
elif __name__ == "__main__":
    main(sys.argv[1])
