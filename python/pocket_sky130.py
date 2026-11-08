"""SKY130 PCell startup instrumentation and diagnostics for KLayout's own Python."""

import importlib
from html import escape
from pathlib import Path
import sys
import sysconfig
import textwrap
import traceback
from xml.etree import ElementTree as ET


LIBRARY_NAME = "skywater130"
TRANSISTORS = {"nfet", "pfet"}
STARTUP_MARKER = "# Pocket Tools: SKY130 startup diagnostics v1"


def instrument_startup(package):
    """Preserve upstream code/metadata, adding a precise error record before its bare except."""
    macro = package / "tech/sky130/pymacros/sky130_pcells.lym"
    if not macro.is_file():
        raise ValueError(f"El paquete SKY130 no contiene la macro de PCells: {macro}")
    source = macro.read_text(encoding="utf-8")
    root = ET.fromstring(source)
    node = root.find("text")
    if node is None or not node.text:
        raise ValueError("La macro SKY130 no contiene codigo Python.")
    if STARTUP_MARKER in node.text:
        return
    if root.findtext("autorun", "false").strip().lower() != "true":
        raise ValueError("La macro SKY130 no tiene autorun activado.")
    original = node.text
    prefix = '''import pya as _pocket_pya
import traceback as _pocket_traceback
_pocket_state = {"macro": __file__, "attempted": True, "success": False}
_pocket_pya._pocket_sky130_startup = _pocket_state
try:
    # Import first so the upstream bare except cannot hide the real exception.
    import gdsfactory
'''
    suffix = '''
    _pocket_library = _pocket_pya.Library.library_by_name("skywater130", "sky130")
    if _pocket_library is None or not {"nfet", "pfet"}.issubset(set(_pocket_library.layout().pcell_names())):
        raise RuntimeError("La macro no registro skywater130 con las PCells nfet y pfet.")
    _pocket_state["success"] = True
except BaseException as _pocket_error:
    _pocket_state["error"] = type(_pocket_error).__name__ + ": " + str(_pocket_error)
    _pocket_state["traceback"] = _pocket_traceback.format_exc()
    print("[SKY130 PCells] " + _pocket_state["error"])
    print(_pocket_state["traceback"])
'''
    code = STARTUP_MARKER + "\n" + prefix + textwrap.indent(original, "    ") + suffix
    start = source.index("<text>") + len("<text>")
    end = source.index("</text>", start)
    # Do not reserialize the document: keep upstream's copyright/license comments.
    macro.write_text(source[:start] + escape(code, quote=False) + source[end:], encoding="utf-8")


def diagnostics(technology, runtime=None, importer=None):
    """Observe the registry; never execute the macro or create PCells during status."""
    technology = Path(technology) if technology is not None else None
    macro = technology.parent / "pymacros/sky130_pcells.lym" if technology else None
    result = dict(technology_installed=bool(technology and technology.is_file()),
                  macro=str(macro) if macro else None, macro_present=False, autorun=False,
                  registered=None, library=LIBRARY_NAME, pcells=[], startup=None,
                  gdsfactory=None, import_error=None, import_traceback=None)
    if macro and macro.is_file():
        result["macro_present"] = True
        try:
            root = ET.parse(macro).getroot()
            result["autorun"] = root.findtext("autorun", "false").strip().lower() == "true"
            result["instrumented"] = STARTUP_MARKER in root.findtext("text", "")
        except (OSError, ET.ParseError) as exc:
            result["macro_error"] = type(exc).__name__ + ": " + str(exc)
    if runtime is None:
        return result

    # Exact upstream name AND transistor declarations. Basic/CIDESI are unrelated.
    library = runtime.Library.library_by_name(LIBRARY_NAME, "sky130")
    result["registered"] = library is not None
    if library is not None:
        result["pcells"] = list(library.layout().pcell_names())
    startup = getattr(runtime, "_pocket_sky130_startup", None)
    if startup and macro and Path(startup.get("macro", "")).resolve() == macro.resolve():
        result["startup"] = dict(startup)
    try:
        module = (importer or importlib.import_module)("gdsfactory")
        result["gdsfactory"] = str(getattr(module, "__version__", "import OK"))
    except Exception as exc:
        result["import_error"] = type(exc).__name__ + ": " + str(exc)
        result["import_traceback"] = traceback.format_exc()
    result["python"] = sys.version.splitlines()[0]
    result["platform"] = sysconfig.get_platform()
    result["prefix"] = sys.prefix
    return result


def status_rows(technology, runtime=None):
    result = diagnostics(technology, runtime)
    rows = [("Tecnologia SKY130", "Instalada" if result["technology_installed"] else "No instalada"),
            ("Macro PCells", ("Presente: " + result["macro"]) if result["macro_present"] else "Falta sky130_pcells.lym"),
            ("Autorun PCells", "Activado" if result["autorun"] else "Desactivado")]
    if result.get("macro_error"):
        rows.append(("Error de macro", result["macro_error"]))
    if runtime is None:
        rows.append(("PCells SKY130", "No comprobadas: consulta pdk status sky130 dentro de KLayout"))
        return rows
    if result["registered"]:
        pcells = set(result["pcells"])
        summary = f"Registrada: {LIBRARY_NAME} ({len(pcells)} PCells)"
        if not TRANSISTORS.issubset(pcells):
            summary += "; faltan nfet/pfet"
    else:
        summary = f"NO registrada: {LIBRARY_NAME}"
    rows.append(("PCells SKY130", summary))
    startup = result["startup"]
    if startup:
        execution = "Ejecutada correctamente" if startup["success"] else "Ejecutada con error: " + startup.get("error", "desconocido")
    else:
        execution = "Sin registro de ejecucion en esta sesion; reinicia con la macro instrumentada"
    rows.extend([
        ("Arranque PCells", execution),
        ("gdsfactory", "ERROR: " + result["import_error"] if result["import_error"] else result["gdsfactory"]),
        ("Python de KLayout", result["python"] + " / " + result["platform"]),
        ("Entorno Python", result["prefix"]),
    ])
    if result["import_error"]:
        advice = ("Instala gdsfactory y sus dependencias para el Python y ABI de KLayout; "
                  "instalarlos solo en el Python del sistema no los hace disponibles aqui. "
                  "Configura KLAYOUT_PYTHONPATH hacia un site-packages compatible y reinicia.")
        if result["platform"].startswith("mingw"):
            advice += (" Este ejecutable usa MinGW: los wheels win_amd64 de CPython no son equivalentes. "
                       "Usa dependencias compiladas para MinGW, o un KLayout y Python con ABI compatibles "
                       "(por ejemplo, ambos en Linux/WSL). No mezcles sus DLL/PYD.")
        rows.append(("Como resolver", advice))
    elif not result["registered"]:
        rows.append(("Como resolver", "Revisa el error de arranque de sky130_pcells.lym en la salida de macros. "
                     "Si gdsfactory importa pero falla cells/kfactory, comprueba las versiones/API de la revision del PDK. "
                     "La presencia de otras bibliotecas no registra skywater130."))
    rows.append(("Prueba funcional", "Crea un layout con tecnologia sky130; Libraries > skywater130 > nfet o pfet. "
                 "Una instancia con geometria valida confirma el funcionamiento; registrar la biblioteca no basta."))
    return rows
