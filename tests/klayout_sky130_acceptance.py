"""Fresh-start SKY130 check. Run with KLayout -z -t -r and -rd sky130_report=... ."""

import json
from pathlib import Path
import sys
import traceback

import pya

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import pocket_pdk
import pocket_sky130

app = pya.Application.instance()
report = {"accepted": False}
try:
    data_dir = Path(app.application_data_path()).resolve()
    paths = tuple(Path(path).resolve() for path in app.klayout_path())
    technology = pocket_pdk.installed_technology(data_dir, "sky130", paths)
    report["libraries_before"] = list(pya.Library.library_names())
    report["startup_before_layout"] = getattr(pya, "_pocket_sky130_startup", None)
    cv = app.main_window().create_layout("sky130", 1)
    report["selected_technology"] = cv.technology
    report["libraries_after"] = list(pya.Library.library_names())
    report["diagnostics"] = pocket_sky130.diagnostics(technology, pya)
    report["status"] = pocket_pdk.ascii_table(("Campo", "Valor"),
        pocket_pdk.status_details(data_dir, "sky130", paths, runtime=pya))
    library = pya.Library.library_by_name("skywater130", "sky130")
    if library is None:
        raise RuntimeError("No esta registrada la biblioteca skywater130; no se puede insertar nfet.")
    layout = cv.layout()
    transistor = layout.create_cell("nfet", "skywater130", {
        "type": "sky130_fd_pr__nfet_01v8", "l": 0.15, "w": 0.42, "nf": 1,
    })
    if transistor is None:
        raise RuntimeError("El generador nfet no devolvio una celda.")
    geometry = False
    for layer in layout.layer_indices():
        shapes = transistor.begin_shapes_rec(layer)
        while not shapes.at_end():
            shape = shapes.shape()
            if shape.is_box() or shape.is_polygon() or shape.is_path():
                geometry = True
                break
            shapes.next()
        if geometry:
            break
    if not geometry:
        raise RuntimeError("nfet no tiene geometria: una celda de error o solo texto no cumple la prueba.")
    top = layout.create_cell("POCKET_SKY130_ACCEPTANCE")
    top.insert(pya.CellInstArray(transistor.cell_index(), pya.Trans()))
    report["transistor_cell"] = transistor.name
    report["transistor_bbox"] = str(transistor.bbox())
    report["accepted"] = cv.technology == "sky130"
except BaseException as exc:
    report["error"] = type(exc).__name__ + ": " + str(exc)
    report["traceback"] = traceback.format_exc()
finally:
    Path(sky130_report).write_text(json.dumps(report, default=str), encoding="utf-8")
    app.exit(0)
