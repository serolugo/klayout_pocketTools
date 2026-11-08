"""Selection editing commands for KLayout 0.30.12, in integer database coordinates."""

import pya


HELP = (
    ("merge", "Merge nativo de la seleccion"),
    ("boxify", "Convertir poligonos rectangulares a Box; un Undo"),
    ("stretch", "Alternar Partial / Move; Partial modifica bordes y vertices"),
    ("move", "Volver a Move para mover figuras completas"),
    ("union", "A union B"),
    ("intersect", "A interseccion B"),
    ("difference", "A menos B"),
    ("reverse-difference", "B menos A"),
    ("xor", "Diferencia simetrica"),
)


def value(obj, name):
    attr = getattr(obj, name)
    return attr() if callable(attr) else attr


def editable_context(view):
    if view is None:
        raise ValueError("Abre un layout editable primero.")
    cv = view.active_cellview()
    if not cv.is_valid() or cv.cell is None:
        raise ValueError("Selecciona una celda activa primero.")
    layout, cell = cv.layout(), cv.cell
    if not value(view, "is_editable") or not value(layout, "is_editable"):
        raise ValueError("El layout esta en modo de solo lectura.")
    if cell.is_locked() or cell.is_library_cell() or cell.is_pcell_variant():
        raise ValueError("La celda activa no es editable (bloqueada, biblioteca o PCell).")
    return cv, layout, cell


def editable_shape(obj, view, cv, cell):
    if obj.is_cell_inst():
        return None
    if obj.cv_index != cv.index() or obj.source() != cell.cell_index() or not obj.is_valid(view):
        return None
    shape = obj.shape
    if shape is None or not shape.is_valid() or shape.is_array_member():
        return None
    if shape.cell is None or shape.cell.cell_index() != cell.cell_index() or shape.layer != obj.layer:
        return None
    return shape


def rectangle_box(polygon):
    """Reject holes/diagonals/notches; collinear redundant vertices are harmless."""
    box = polygon.bbox()
    if polygon.holes() or box.width() <= 0 or box.height() <= 0 or polygon.area() == 0:
        return None
    for edge in polygon.each_edge():
        a, b = edge.p1, edge.p2
        if a.x == b.x:
            if a.x not in (box.left, box.right):
                return None
        elif a.y == b.y:
            if a.y not in (box.bottom, box.top):
                return None
        else:
            return None
    if not (pya.Region(polygon) ^ pya.Region(box)).is_empty():
        return None
    return box


def boxify(view):
    cv, layout, cell = editable_context(view)
    selection = list(view.object_selection)
    planned, seen = [], []
    for obj in selection:
        shape = editable_shape(obj, view, cv, cell)
        if shape is None or not shape.is_polygon() or any(shape == previous for previous in seen):
            continue
        seen.append(shape)
        polygon = shape.polygon.dup()
        box = rectangle_box(polygon)
        if box is not None:
            planned.append((shape, polygon, box))
    if planned:
        converted = []
        view.transaction("Pocket Tools: boxify")
        try:
            for shape, polygon, box in planned:
                shape.box = box  # Native setter preserves the layer and properties.
                converted.append((shape, polygon))
        except Exception:
            for shape, polygon in reversed(converted):
                shape.polygon = polygon
            raise
        finally:
            view.commit()
        # Type changes invalidate selection paths; avoid leaving dangling handles.
        view.object_selection = []
    return f"boxify: {len(planned)} convertidos; {len(selection) - len(planned)} omitidos."


def polygon_of(shape):
    if shape.is_box():
        return pya.Polygon(shape.box)
    if shape.is_polygon():
        return shape.polygon.dup()
    if shape.is_path():
        return shape.path.polygon()
    raise ValueError("Selecciona Boxes, poligonos o pistas con area; texto, aristas e instancias no son validos.")


def boolean(view, operation):
    cv, layout, cell = editable_context(view)
    selection = list(view.object_selection)
    if len(selection) != 2:
        raise ValueError("Selecciona exactamente dos figuras, en la celda activa y en la misma capa.")
    shapes = [editable_shape(obj, view, cv, cell) for obj in selection]
    if any(shape is None for shape in shapes):
        raise ValueError("Las dos figuras deben ser editables en la celda activa; se omiten instancias y otras celdas.")
    if selection[0].layer != selection[1].layer:
        raise ValueError("Las dos figuras deben estar en la misma capa.")
    if shapes[0] == shapes[1]:
        raise ValueError("Selecciona dos figuras distintas.")
    if selection[0].seq == selection[1].seq and operation in ("difference", "reverse-difference"):
        raise ValueError("Orden A/B ambiguo: selecciona A primero y luego B con Shift.")
    ordered = sorted(zip(selection, shapes), key=lambda pair: pair[0].seq)
    a, b = (pya.Region(polygon_of(pair[1])).merged() for pair in ordered)
    if a.is_empty() or b.is_empty():
        raise ValueError("Las dos figuras deben tener area no nula.")
    if operation == "union":
        result = a | b
    elif operation == "intersect":
        result = a & b
    elif operation == "difference":
        result = a - b
    elif operation == "reverse-difference":
        result = b - a
    elif operation == "xor":
        result = a ^ b
    else:
        raise ValueError("Operacion booleana desconocida.")
    result = result.merged()
    polygons = [polygon.dup() for polygon in result.each()]
    if not polygons or any(polygon.area() <= 0 for polygon in polygons):
        return f"{operation}: resultado vacio o sin area; las figuras se conservan."
    # Compute everything before opening the single editing transaction.
    properties = [value(shape, "properties") for shape in shapes]
    common = {key: item for key, item in properties[0].items()
              if key in properties[1] and properties[1][key] == item}
    originals = [(shape, shape.box if shape.is_box() else shape.path if shape.is_path()
                  else shape.polygon, shape.prop_id) for shape in shapes]
    layer = selection[0].layer
    inserted, deleted = [], []
    view.transaction("Pocket Tools: " + operation)
    try:
        for polygon in polygons:
            shape = cell.shapes(layer).insert(polygon)
            inserted.append(shape)
            if common:
                shape.set_properties(common)
        view.object_selection = []
        for original in originals:
            original[0].delete()
            deleted.append(original)
    except Exception:
        for shape in inserted:
            if shape.is_valid():
                shape.delete()
        for _, geometry, prop_id in deleted:
            cell.shapes(layer).insert(geometry, prop_id)
        raise
    finally:
        view.commit()
    return f"{operation}: 2 figuras reemplazadas por {len(polygons)} poligonos; un Undo."


def focus_canvas(view):
    widget = view.widget()
    target = value(widget, "focusProxy")
    if target is None:
        stack = list(value(widget, "children"))
        while stack:
            child = stack.pop()
            if str(value(child, "objectName")) == "canvas" or "LayoutCanvas" in type(child).__name__:
                target = child
                break
            stack.extend(value(child, "children"))
    (target or widget).setFocus()


def execute(command, view):
    if command == "merge":
        editable_context(view)
        if not list(view.object_selection):
            return "merge: seleccion vacia."
        view.call_menu("edt::sel_union")
        return "merge: Merge nativo ejecutado sobre la seleccion."
    if command == "boxify":
        return boxify(view)
    if command in ("stretch", "move"):
        editable_context(view)
        partial = command == "stretch" and view.mode_name() != "partial"
        view.switch_mode("partial" if partial else "move")
        focus_canvas(view)
        if partial:
            return ("stretch: Partial activo; mover bordes o vertices CAMBIA la forma. "
                    "Usa move, repite stretch o pulsa Volver a Move para salir.")
        return "Move activo: mueve figuras completas; Partial desactivado."
    return boolean(view, command)
