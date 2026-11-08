# $autorun

import base64
import code
import contextlib
import html
import io
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tokenize
import traceback
import uuid

import pya

import pocket_pdk
import pocket_edit


RUBY = "ruby"
PYTHON = "python"
SHELL = "shell"
PROMPT_COLOR = "#E5A044"
VERSION = "1.0"

RUBY_BOOTSTRAP = r"""
require 'stringio'

unless defined?($pocket_console_ruby_context)
  $pocket_console_ruby_context = Object.new

  class << $pocket_console_ruby_context
    def mw
      RBA::MainWindow.instance
    end

    def view
      current = mw.current_view
      raise "No hay un layout abierto." unless current
      current
    end

    def fit
      view.zoom_fit
      nil
    end

    def run(path)
      filename = File.expand_path(path)
      eval(
        File.read(filename, encoding: "UTF-8"),
        $pocket_console_ruby_binding,
        filename,
        1
      )
    end
  end

  $pocket_console_ruby_binding =
    $pocket_console_ruby_context.instance_eval { binding }
end
"""


def qt_value(obj, name):
    """Admite propiedades y métodos de los bindings de KLayout."""
    value = getattr(obj, name)
    return value() if callable(value) else value


def utf16_length(text):
    # Qt y PowerShell expresan posiciones en unidades UTF-16.
    return len(text.encode("utf-16-le")) // 2


def orange_prompt(text):
    return (
        '<span style="color:'
        + PROMPT_COLOR
        + '; font-weight:bold;">'
        + html.escape(text)
        + "</span>"
    )


class ConsoleClickFocus(pya.QObject):
    """Un clic en el historial o prompt enfoca la entrada."""

    def __init__(self, entry, parent):
        super().__init__(parent)
        self.entry = entry

    def eventFilter(self, watched, event):
        if qt_value(event, "type") == pya.QEvent.MouseButtonRelease:
            if qt_value(event, "button") == pya.Qt.LeftButton:
                self.entry.setFocus()

                # Continuar escribiendo al final del comando.
                text = qt_value(self.entry, "text")
                self.entry.setCursorPosition(utf16_length(text))

        # Mantener el comportamiento normal del widget.
        return False


class ConsoleEventWake(pya.QObject):
    """Process worker output on the GUI thread when it actually arrives."""

    def __init__(self, console, parent):
        super().__init__(parent)
        self.console = console

    def eventFilter(self, watched, event):
        if qt_value(event, "type") == pya.QEvent.User:
            self.console.poll()
            return True
        return False


def shell_program(marker):
    """Crear una sesión persistente de PowerShell o Bash."""
    if os.name == "nt":
        executable = (
            shutil.which("pwsh")
            or shutil.which("powershell.exe")
        )

        if not executable:
            raise RuntimeError("No se encontró PowerShell.")

        bootstrap = r"""
[Console]::InputEncoding = [Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$OutputEncoding = [Console]::OutputEncoding
$ProgressPreference = 'SilentlyContinue'

while ($null -ne ($__pc_line = [Console]::ReadLine())) {
    try {
        $__pc_cmd = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($__pc_line)
        )

        Invoke-Expression $__pc_cmd 2>&1 |
            Out-String -Stream |
            ForEach-Object {
                [Console]::WriteLine($_)
            }
    }
    catch {
        [Console]::WriteLine($_.ToString())
    }
    finally {
        $__pc_path = (Get-Location).Path
        $__pc_encoded = [Convert]::ToBase64String(
            [Text.Encoding]::UTF8.GetBytes($__pc_path)
        )

        [Console]::WriteLine('')
        [Console]::WriteLine('__MARKER__' + $__pc_encoded)
        [Console]::Out.Flush()
    }
}
""".replace("__MARKER__", marker)

        encoded = base64.b64encode(
            bootstrap.encode("utf-16le")
        ).decode("ascii")

        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ]

    executable = shutil.which("bash")

    if not executable or not shutil.which("base64"):
        raise RuntimeError("Se requieren Bash y base64.")

    bootstrap = r"""
while IFS= read -r __pc_line; do
    __pc_cmd=$(printf '%s' "$__pc_line" | base64 --decode)
    eval "$__pc_cmd" </dev/null
    printf '\n__MARKER__'
    printf '%s' "$PWD" | base64 | tr -d '\n'
    printf '\n'
done
""".replace("__MARKER__", marker)

    return [
        executable,
        "--noprofile",
        "--norc",
        "-c",
        bootstrap,
    ]


class PocketConsole:
    EDIT_COMMANDS = ("merge", "boxify", "stretch", "move", "union", "intersect",
                     "difference", "reverse-difference", "xor")

    def __init__(self):
        self.mw = pya.Application.instance().main_window()

        self.proc = None
        self.pdk_installing = False
        self.restart_pending = False
        self.shell_busy = False
        self.python_busy = False
        self.more = False
        self.ruby_busy = False
        self.ruby_interpreter = pya.Interpreter.ruby_interpreter()
        if self.ruby_interpreter is not None:
            self.ruby_interpreter.eval_string(RUBY_BOOTSTRAP)

        self.events = queue.Queue(maxsize=2048)
        self.marker = "__POCKET_" + uuid.uuid4().hex + "__"

        self.completion_marker = "__COMPLETE_" + uuid.uuid4().hex + "__"
        self.completion_pending = None
        self.completion_cache = None
        self.pdk_completion_cache = None

        self.histories = {}
        self.positions = {}
        self.outputs = {}
        self.entries = {}
        self.prompts = {}
        self.click_focus_filters = []
        self.shortcuts = []
        self.pages = {}
        self.tab_titles = {}
        self.console_actions = {}

        self.cwd = self.initial_directory()
        self.shell_name = "PowerShell" if os.name == "nt" else "Bash"

        self.dock = pya.QDockWidget("Pocket Console " + VERSION, self.mw)
        self.dock.setObjectName("PocketConsoleDock")

        body = pya.QWidget(self.dock)
        body.setObjectName("PocketConsoleBody")

        outer = pya.QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.partial_notice = pya.QWidget(body)
        notice_layout = pya.QHBoxLayout(self.partial_notice)
        notice_layout.setContentsMargins(6, 3, 6, 3)
        notice_text = pya.QLabel("Partial activo: mover bordes o vertices cambia la forma.", self.partial_notice)
        notice_layout.addWidget(notice_text, 1)
        self.partial_exit = pya.QPushButton("Volver a Move", self.partial_notice)
        self.partial_exit.clicked = self.leave_partial
        notice_layout.addWidget(self.partial_exit)
        outer.addWidget(self.partial_notice)
        self.partial_notice.hide()

        self.tabs = pya.QTabWidget(body)
        outer.addWidget(self.tabs, 1)

        self.create_tab(PYTHON, "Python", self.execute_python, "py >>")
        if self.ruby_interpreter is not None:
            self.create_tab(RUBY, "Ruby", self.execute_ruby, "rb >>")
        self.create_tab(SHELL, self.shell_name, self.execute_shell, "")

        self.dock.setWidget(body)

        # Colores obtenidos del tema de KLayout.
        self.dock.setStyleSheet("""
            QWidget#PocketConsoleBody,
            QWidget#PocketConsolePage,
            QWidget#PocketConsoleInput {
                background: palette(base);
                color: palette(text);
                border: 0;
                margin: 0;
                padding: 0;
            }

            QTabWidget::pane {
                border: 0;
                margin: 0;
                padding: 0;
                background: palette(base);
            }

            QTabBar::tab {
                background: palette(base);
                color: palette(text);
                border: 0;
                padding: 5px 12px;
            }

            QTabBar::tab:selected {
                border-bottom: 2px solid palette(highlight);
            }

            QTextEdit, QLineEdit, QLabel {
                background: palette(base);
                color: palette(text);
                font-family: Consolas, monospace;
                font-size: 12px;
                border: 0;
            }

            QLineEdit {
                padding: 0;
                margin: 0;
            }
        """)

        self.mw.addDockWidget(
            pya.Qt.BottomDockWidgetArea,
            self.dock,
        )

        self.namespace = {
            "__name__": "__console__",
            "pya": pya,
            "mw": self.mw,
            "view": self.view,
            "cv": self.cv,
            "run": self.run_script,
            "project": lambda: self.cwd,
            "fit": self.fit,
            "pdk": self.pdk,
            "restart": self.request_restart,
            "install_pdk": lambda name: self.pdk("install", name),
            "bind_fit": self.bind_fit,
            "clear": self.outputs[PYTHON].clear,
        }

        self.console = code.InteractiveConsole(self.namespace)

        self.timer = pya.QTimer(self.dock)
        self.timer.timeout = self.poll
        self.wake_filter = ConsoleEventWake(self, self.dock)
        self.dock.installEventFilter(self.wake_filter)

        self.write(PYTHON, "Python listo. help | merge | boxify | stretch | run('archivo.py') | restart")
        self.write(SHELL, self.shell_name + " — " + self.cwd)

        self.update_shell_prompt()
        status_tab = PYTHON
        if RUBY in self.entries:
            self.write(RUBY, "Ruby listo. fit | mw | view | run('archivo.rb')")
        try:
            self.write(status_tab, self.bind_fit())
        except Exception as exc:
            self.write(status_tab, "No se pudo asignar F: " + str(exc))
        self.dock.show()
        self.install_console_menu()
        self.dock.visibilityChanged = self.console_visibility_changed
        self.entries[status_tab].setFocus()

    def console_visibility_changed(self, visible):
        # Closing the dock also clears its menu checks, so one click restores it.
        if not visible and qt_value(self.dock, "hidden"):
            for action in self.console_actions.values():
                action.checked = False
            while qt_value(self.tabs, "count"):
                self.tabs.removeTab(0)

    def update_partial_notice(self):
        view = self.mw.current_view()
        active = view is not None and view.mode_name() == "partial"
        if active == qt_value(self.partial_notice, "hidden"):
            self.partial_notice.setVisible(active)

    def leave_partial(self):
        try:
            message = pocket_edit.execute("move", self.mw.current_view())
        except (ValueError, RuntimeError) as exc:
            message = "[AVISO] " + str(exc)
        self.write(PYTHON, message)
        self.update_partial_notice()

    def install_console_menu(self):
        menu = self.mw.menu()
        root = "view_menu.pocket_tools"
        if not menu.is_valid(root):
            menu.insert_menu("view_menu.end", "pocket_tools", "Pocket Tools")
        branch = root + ".consoles"
        if menu.is_valid(branch):
            menu.delete_item(branch)
        menu.insert_menu(root + ".end", "consoles", "Consoles")
        for key in (PYTHON, RUBY, SHELL):
            if key not in self.pages:
                continue
            action = pya.Action()
            action.title = self.tab_titles[key]
            action.checkable = True
            action.checked = True
            action.on_triggered = lambda key=key: self.toggle_console(key)
            menu.insert_item(branch + ".end", key, action)
            self.console_actions[key] = action

    def toggle_console(self, key):
        current = qt_value(self.tabs, "currentWidget")
        while qt_value(self.tabs, "count"):
            self.tabs.removeTab(0)
        for name in (PYTHON, RUBY, SHELL):
            action = self.console_actions.get(name)
            if action is not None and action.is_checked():
                self.tabs.addTab(self.pages[name], self.tab_titles[name])
        if not qt_value(self.tabs, "count"):
            self.dock.hide()
            return
        self.dock.show()
        if current is not None and self.tabs.indexOf(current) >= 0:
            self.tabs.setCurrentWidget(current)
        elif self.console_actions[PYTHON].is_checked():
            self.tabs.setCurrentWidget(self.pages[PYTHON])
        qt_value(self.tabs, "currentWidget").show()

    def initial_directory(self):
        current = self.mw.current_view()

        if current is not None:
            cv = current.active_cellview()

            if cv.is_valid():
                filename = cv.filename()

                if filename:
                    parent = Path(filename).resolve().parent
                    if parent.is_dir():
                        return str(parent)

        return str(Path.home())

    def create_tab(self, index, title, execute, initial_prompt):
        page = pya.QWidget(self.tabs)
        page.setObjectName("PocketConsolePage")

        layout = pya.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Área superior: es la única que crece.
        output = pya.QTextEdit(page)
        output.setReadOnly(True)
        output.setFrameShape(pya.QFrame.NoFrame)
        output.setMinimumHeight(40)
        output.setContentsMargins(0, 0, 0, 0)
        output.document.setMaximumBlockCount(3000)
        output.document.setDocumentMargin(4)

        layout.addWidget(output, 1)

        if index == PYTHON:
            self.pdk_progress = pya.QWidget(page)
            progress_layout = pya.QHBoxLayout(self.pdk_progress)
            progress_layout.setContentsMargins(6, 0, 6, 0)
            self.pdk_progress_label = pya.QLabel(self.pdk_progress)
            self.pdk_progress_label.setTextFormat(pya.Qt.PlainText)
            self.pdk_progress_bar = pya.QProgressBar(self.pdk_progress)
            progress_layout.addWidget(self.pdk_progress_label, 1)
            progress_layout.addWidget(self.pdk_progress_bar)
            layout.addWidget(self.pdk_progress)
            self.pdk_progress.hide()

        # Barra inferior: altura fija, fuera del área desplazable.
        input_bar = pya.QWidget(page)
        input_bar.setObjectName("PocketConsoleInput")
        input_bar.setFixedHeight(30)

        input_layout = pya.QHBoxLayout(input_bar)
        input_layout.setContentsMargins(6, 0, 6, 0)
        input_layout.setSpacing(6)

        prompt = pya.QLabel(input_bar)
        prompt.setTextFormat(pya.Qt.RichText)
        prompt.setWordWrap(False)
        prompt.setText(orange_prompt(initial_prompt))

        entry = pya.QLineEdit(input_bar)
        entry.setFrame(False)
        entry.setFixedHeight(26)

        input_layout.addWidget(prompt, 0)
        input_layout.addWidget(entry, 1)

        # Stretch cero: esta barra no aumenta de altura.
        layout.addWidget(input_bar, 0)

        entry.returnPressed = execute
        self.outputs[index] = output
        self.entries[index] = entry
        self.prompts[index] = prompt
        self.histories[index] = []
        self.positions[index] = 0

        click_filter = ConsoleClickFocus(entry, self.dock)
        self.click_focus_filters.append(click_filter)
        qt_value(output, "viewport").installEventFilter(click_filter)
        prompt.installEventFilter(click_filter)
        input_bar.installEventFilter(click_filter)

        self.add_shortcut(
            entry,
            "Up",
            lambda n=index: self.history(n, -1),
        )
        self.add_shortcut(
            entry,
            "Down",
            lambda n=index: self.history(n, 1),
        )
        self.add_shortcut(
            entry,
            "Ctrl+L",
            lambda n=index: self.outputs[n].clear(),
        )

        if index == SHELL:
            entry.setMinimumWidth(100)
            if os.name == "nt":
                self.add_shortcut(entry, "Tab", self.complete_powershell)
                self.add_shortcut(
                    entry, "Shift+Tab", lambda: self.complete_powershell(-1)
                )
            self.add_shortcut(
                entry,
                "Ctrl+Shift+C",
                self.stop_shell,
            )

        elif index == PYTHON:
            self.add_shortcut(entry, "Tab", self.complete_pdk)
            self.add_shortcut(entry, "Shift+Tab", lambda: self.complete_pdk(-1))
            entry.setToolTip("help: ayuda | Tab / Shift+Tab: completar comandos de edicion, restart y PDK (experimental)")

        elif index == RUBY:
            entry.setToolTip("run('archivo.rb') | fit | ↑/↓: historial | Ctrl+L: limpiar")

        self.tabs.addTab(page, title)
        self.pages[index] = page
        self.tab_titles[index] = title

    def add_shortcut(self, widget, sequence, callback):
        shortcut = pya.QShortcut(
            pya.QKeySequence(sequence),
            widget,
        )
        shortcut.setContext(pya.Qt.WidgetShortcut)
        shortcut.activated = callback
        self.shortcuts.append(shortcut)

    def write(self, index, text):
        """Añadir al historial y desplazar hasta el final."""
        output = self.outputs[index]

        output.append(
            '<pre style="margin:0; white-space:pre-wrap;">'
            + html.escape(str(text))
            + "</pre>"
        )

        output.moveCursor(pya.QTextCursor.End)
        output.ensureCursorVisible()

    def remember(self, index, command):
        history = self.histories[index]

        if command.strip():
            if not history or history[-1] != command:
                history.append(command)

        self.positions[index] = len(history)

    def history(self, index, delta):
        history = self.histories[index]

        position = max(
            0,
            min(
                len(history),
                self.positions[index] + delta,
            ),
        )

        self.positions[index] = position

        self.entries[index].setText(
            history[position]
            if position < len(history)
            else ""
        )

    def view(self):
        current = self.mw.current_view()

        if current is None:
            raise RuntimeError("No hay un layout abierto.")

        return current

    def cv(self):
        current = self.view().active_cellview()

        if not current.is_valid():
            raise RuntimeError("No hay una celda activa.")

        return current

    def run_script(self, filename):
        path = Path(filename).expanduser()

        if not path.is_absolute():
            path = Path(self.cwd) / path

        path = path.resolve(strict=True)

        with tokenize.open(str(path)) as source:
            compiled = compile(
                source.read(),
                str(path),
                "exec",
            )

        missing = object()
        previous_file = self.namespace.get("__file__", missing)
        previous_path = list(sys.path)

        self.namespace["__file__"] = str(path)
        sys.path.insert(0, str(path.parent))

        try:
            exec(compiled, self.namespace)
        finally:
            sys.path[:] = previous_path

            if previous_file is missing:
                self.namespace.pop("__file__", None)
            else:
                self.namespace["__file__"] = previous_file

    def pdk(self, action=None, name=None, *, source=None):
        if action is None:
            print(pocket_pdk.ascii_table(("Comando", "Descripcion"), [
                ("pdk help", "Mostrar esta ayuda"),
                ("pdk list", "Ver catalogo y estado"),
                ("pdk status <id>", "Ver detalles y ubicacion"),
                ("pdk install <id> [--source <ruta>]", "Descargar o importar un PDK"),
                ("pdk remove <id>", "Eliminar la copia de Pocket Tools"),
                ("restart / pdk restart", "Reiniciar KLayout (confirmacion y/n)"),
            ]))
            print('Tambien: pdk("list"), pdk("status", "sky130"), restart().')
            return
        if action == "restart":
            if name is not None or source is not None:
                raise ValueError("Sintaxis: pdk('restart')")
            self.request_restart()
            return
        if action not in ("list", "status", "install", "remove"):
            raise ValueError("Acción desconocida. Usa pdk() para ver los comandos.")

        app = pya.Application.instance()
        data_dir = Path(app.application_data_path()).resolve()
        search_paths = tuple(Path(path).resolve() for path in app.klayout_path())
        if action == "list":
            print(pocket_pdk.list_table(data_dir, search_paths))
            print("Detalles: pdk status <id> | Instalar: pdk install <id>")
            return

        spec = pocket_pdk.source_for(name)
        if action == "status":
            print(pocket_pdk.ascii_table(("Campo", "Valor"), pocket_pdk.status_details(data_dir, name, search_paths, runtime=pya)))
            return
        if self.pdk_installing:
            print("Ya hay una operación de PDK en curso.")
            return

        if action == "remove" and source is not None:
            raise ValueError("Sintaxis: pdk('remove', PDK); remove no admite source.")
        source_dir = Path(source).expanduser().resolve() if source is not None else None
        if action == "install" and spec.get("download") == "local" and source_dir is None:
            print(f"Indica source=... con la carpeta de {spec['local_variant']}.")
            return

        last_progress = [None, 0.0]
        remove_executable = str(qt_value(app, "applicationFilePath")) if action == "remove" else None

        def progress(event):
            now = time.monotonic()
            # No llenar la cola por cada bloque. Los cambios de fase son inmediatos.
            finished = (
                bool(event.get("total"))
                and event.get("completed") == event["total"]
                and event.get("files") == event.get("total_files")
            )
            if event["phase"] != last_progress[0] or now - last_progress[1] >= 0.25 or finished:
                last_progress[:] = [event["phase"], now]
                self.enqueue_event("pdk_progress", event)

        def worker():
            success = True
            try:
                if action == "remove":
                    message = pocket_pdk.remove(data_dir, name, search_paths, progress=progress,
                                                klayout_executable=remove_executable)
                else:
                    message = pocket_pdk.install(
                        data_dir, name, search_paths, source_dir, progress=progress
                    )
            except Exception as exc:
                success = False
                verb = "eliminar" if action == "remove" else "instalar"
                message = f"No se pudo {verb} {name}: {exc}"
            self.enqueue_event("pdk", dict(name=name, action=action, success=success, message=message,
                                           restart=success and "Reinicia KLayout" in message))

        self.pdk_installing = True
        self.update_pdk_progress({"phase": "prepare"})
        self.timer.start(60)
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.pdk_installing = False
            self.timer.stop()
            self.pdk_progress.hide()
            raise
        print(f"[EN CURSO] {action} {name}\nEl progreso aparece abajo; puedes seguir usando la consola.")

    def request_restart(self):
        if self.pdk_installing:
            print("[ESPERA] Hay una operacion de PDK en curso. Reinicia cuando termine.")
            return
        if self.shell_busy:
            print("[ESPERA] Hay un comando de shell en curso. Reinicia cuando termine.")
            return
        self.restart_pending = True
        print("Reiniciar KLayout ahora? [y/n]\nKLayout preguntara por los cambios sin guardar.")

    def restart_klayout(self):
        if self.pdk_installing or self.shell_busy:
            print("[ESPERA] Termina la operacion en curso antes de reiniciar.")
            return
        app = pya.Application.instance()
        executable = str(qt_value(app, "applicationFilePath"))
        if not Path(executable).is_file():
            raise RuntimeError("No se encontro el ejecutable de KLayout.")
        arguments = [executable]
        if app.is_editable():
            arguments.append("-e")
        # QWidget.close() returns False when the user cancels the save dialog.
        if not self.mw.close():
            print("[CANCELADO] KLayout sigue abierto.")
            return
        try:
            subprocess.Popen(arguments, cwd=self.cwd)
        except OSError:
            self.mw.show()
            raise

    def dispatch_console(self, command):
        stripped = command.strip()
        if getattr(self, "restart_pending", False):
            self.restart_pending = False
            if stripped.lower() in ("y", "yes", "s", "si", "sí"):
                self.restart_klayout()
                return True
            print("Reinicio pospuesto. Usa restart cuando quieras reiniciar.")
            if stripped.lower() in ("", "n", "no"):
                return True
        if stripped == "restart":
            self.request_restart()
            return True
        if stripped in ("help", "pdk help"):
            self.show_help()
            return True
        if stripped in self.EDIT_COMMANDS:
            try:
                print(pocket_edit.execute(stripped, self.mw.current_view()))
                self.update_partial_notice()
            except (ValueError, RuntimeError) as exc:
                print("[AVISO] " + str(exc))
            return True
        return self.dispatch_pdk(command)

    def show_help(self):
        print("Comandos de edicion (solos en py >>):")
        for name, description in pocket_edit.HELP:
            print("  " + name.ljust(20) + description)
        print("A = primer clic; B = segundo clic con Shift. Misma capa y celda editable.")
        print("restart             Reiniciar KLayout con confirmacion y/n")
        print("Python normal: pya, mw, view(), cv(), fit(), run('archivo.py'), clear()")
        print("PDK: Work in progress - experimental, no recomendado por ahora.")
        self.execute_pdk_command()

    def update_pdk_progress(self, event):
        """Solo se llama desde el hilo de la interfaz."""
        phase = event["phase"]
        labels = {
            "prepare": "Preparando PDK…",
            "extract": "Extrayendo PDK…",
            "copy": "Copiando desde carpeta local…",
            "install": "Instalando PDK…",
            "remove": "Eliminando PDK…",
        }
        self.pdk_progress_bar.setRange(0, 0)
        if phase == "download":
            downloaded = event["downloaded"]
            total = event["total"]
            label = f"Descargando PDK: {downloaded / 1048576:.1f} MB"
            if total:
                percent = min(100, int(downloaded * 100 / total))
                label += f" / {total / 1048576:.1f} MB ({percent}%)"
                self.pdk_progress_bar.setRange(0, 100)
                self.pdk_progress_bar.setValue(percent)
        else:
            label = labels[phase]
            total = event.get("total")
            if total:
                percent = min(100, int(event["completed"] * 100 / total))
                self.pdk_progress_bar.setRange(0, 100)
                self.pdk_progress_bar.setValue(percent)
                label += f" {percent}%"
            if "total_files" in event:
                unit = "elementos" if phase == "remove" else "archivos"
                label += f" · {event['files']}/{event['total_files']} {unit}"
            if phase in ("extract", "copy") and "completed" in event:
                label += f" · {event['completed'] / 1048576:.1f} MB"
        self.pdk_progress_text = label
        self.animate_pdk_progress(force=True)
        self.pdk_progress.show()

    def animate_pdk_progress(self, force=False):
        """Animate from the UI timer even while the worker waits on a large file."""
        if not self.pdk_installing or not getattr(self, "pdk_progress_text", None):
            return
        now = time.monotonic()
        if force or now - getattr(self, "pdk_spinner_time", 0.0) >= 0.25:
            self.pdk_spinner_time = now
            frame = getattr(self, "pdk_spinner_frame", -1) + 1
            self.pdk_spinner_frame = frame
            self.pdk_progress_label.setText("|/-\\"[frame % 4] + " " + self.pdk_progress_text)

    def dispatch_pdk(self, command):
        """Atajos de texto; las llamadas pdk(...) siguen siendo Python."""
        words = command.split(None, 2)
        if not words or words[0] != "pdk":
            return False
        if len(words) > 1 and words[1] not in ("help", "list", "status", "install", "remove", "restart"):
            return False

        syntax = (
            "Sintaxis: pdk help | pdk list | pdk status <id> | "
            "pdk install <id> [--source <ruta>] | pdk remove <id> | pdk restart"
        )
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        # Las barras de las rutas Windows son literales, incluso entre comillas.
        lexer.escape = ""
        try:
            args = list(lexer)[1:]
        except ValueError:
            print(syntax)
            return True

        if args == ["help"]:
            self.execute_pdk_command()
        elif args == ["list"]:
            self.execute_pdk_command("list")
        elif args == ["restart"]:
            self.execute_pdk_command("restart")
        elif (
            len(args) == 2
            and args[0] in ("status", "install", "remove")
            and args[1]
            and not args[1].startswith("--")
        ):
            self.execute_pdk_command(args[0], args[1])
        elif (
            len(args) == 4
            and args[0] == "install"
            and args[1]
            and not args[1].startswith("--")
            and args[2] == "--source"
            and args[3]
            and not args[3].startswith("--")
        ):
            self.execute_pdk_command("install", args[1], source=args[3])
        else:
            print(syntax)
        return True

    def execute_pdk_command(self, *args, **kwargs):
        try:
            self.pdk(*args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            print(pocket_pdk.ascii_table(("Estado", "Detalle"), [("ERROR", str(exc))]))

    def complete_pdk(self, direction=1):
        if self.more:
            return
        entry = self.entries[PYTHON]
        text = str(qt_value(entry, "text"))
        qt_cursor = int(qt_value(entry, "cursorPosition"))
        cursor = len(text.encode("utf-16-le")[:qt_cursor * 2].decode("utf-16-le"))
        cache = self.pdk_completion_cache
        if (cache and len(cache["matches"]) > 1 and text == cache["shown"]
                and cursor == cache["cursor"]):
            cache["choice"] = (cache["choice"] + direction) % len(cache["matches"])
        else:
            start, end, matches = pocket_pdk.pdk_completion_options(text, cursor)
            # Complete only a standalone command, never a Python expression or
            # arguments. Delegate PDK subcommands and paths to the existing helper.
            if not text[:start].strip() and not text[end:].strip():
                prefix = text[start:cursor].lower()
                options = ("help",) + self.EDIT_COMMANDS + ("restart", "pdk")
                matches = [option for option in options if option.startswith(prefix)]
            if not matches:
                self.pdk_completion_cache = None
                return
            cache = dict(original=text, start=start, end=end, matches=matches,
                         choice=0 if direction > 0 else len(matches) - 1)
            self.pdk_completion_cache = cache
            if len(matches) > 1:
                self.write(PYTHON, "Opciones: " + " | ".join(matches))
        replacement = cache["matches"][cache["choice"]]
        suffix = cache["original"][cache["end"]:]
        if not suffix:
            replacement += " "
        shown = cache["original"][:cache["start"]] + replacement + suffix
        cursor = cache["start"] + len(replacement)
        entry.setText(shown)
        entry.setCursorPosition(utf16_length(shown[:cursor]))
        cache.update(shown=shown, cursor=cursor)

    def execute_python(self):
        if self.python_busy:
            return

        self.pdk_completion_cache = None
        self.python_busy = True

        command = self.entries[PYTHON].text
        self.entries[PYTHON].clear()

        self.remember(PYTHON, command)
        self.write(
            PYTHON,
            ("... " if self.more else "reiniciar [y/n] >> "
             if self.restart_pending else "py >> ") + command,
        )

        stream = io.StringIO()
        previous_cwd = os.getcwd()
        previous_hook = sys.displayhook

        def display(value):
            if value is not None:
                self.namespace["_"] = value
                print(repr(value))

        try:
            os.chdir(self.cwd)
            sys.displayhook = display

            with contextlib.redirect_stdout(stream):
                with contextlib.redirect_stderr(stream):
                    if self.more or not self.dispatch_console(command):
                        self.more = self.console.push(command)

        except SystemExit:
            self.console.resetbuffer()
            self.more = False
            self.write(PYTHON, "Usa la X para ocultar la consola.")

        except BaseException:
            self.write(PYTHON, traceback.format_exc())

        finally:
            sys.displayhook = previous_hook

            try:
                os.chdir(previous_cwd)
            finally:
                self.python_busy = False

        if stream.getvalue():
            self.write(PYTHON, stream.getvalue().rstrip("\n"))

        self.prompts[PYTHON].setText(orange_prompt(
            "..." if self.more else "reiniciar [y/n] >>" if self.restart_pending else "py >>"
        ))

    def execute_ruby(self):
        if self.ruby_busy:
            return

        index = RUBY
        command = str(qt_value(self.entries[index], "text"))

        if not command.strip():
            return

        self.entries[index].clear()
        self.remember(index, command)
        self.write(index, "rb >> " + command)

        if command.strip() == "clear":
            self.outputs[index].clear()
            return

        encoded_command = base64.b64encode(
            command.encode("utf-8")
        ).decode("ascii")

        encoded_directory = base64.b64encode(
            self.cwd.encode("utf-8")
        ).decode("ascii")

        # Capturar puts, print, warn y errores de Ruby.
        # Las variables del usuario persisten en el binding.
        wrapper = r"""
begin
  __pc_output = StringIO.new
  __pc_old_stdout = $stdout
  __pc_old_stderr = $stderr

  begin
    $stdout = __pc_output
    $stderr = __pc_output

    __pc_command = '__COMMAND__'.unpack1('m0').force_encoding('UTF-8')
    __pc_directory = '__DIRECTORY__'.unpack1('m0').force_encoding('UTF-8')

    __pc_result = Dir.chdir(__pc_directory) do
      eval(
        __pc_command,
        $pocket_console_ruby_binding,
        '(Pocket Console)',
        1
      )
    end

    unless __pc_result.nil?
      __pc_output.puts("=> " + __pc_result.inspect)
    end

  rescue Exception => __pc_error
    __pc_output.puts(
      __pc_error.class.to_s + ": " + __pc_error.message
    )
    (__pc_error.backtrace || []).first(5).each do |line|
      __pc_output.puts("  " + line)
    end

  ensure
    $stdout = __pc_old_stdout
    $stderr = __pc_old_stderr
  end

  __pc_output.string
end
"""
        wrapper = (
            wrapper
            .replace("__COMMAND__", encoded_command)
            .replace("__DIRECTORY__", encoded_directory)
        )

        self.ruby_busy = True

        try:
            result = self.ruby_interpreter.eval_expr(wrapper)

            if result:
                self.write(index, str(result).rstrip("\n"))

        except Exception:
            self.write(index, traceback.format_exc())

        finally:
            self.ruby_busy = False

    def update_shell_prompt(self, location=None):
        location = str(location or self.cwd)
        prefix = "PS " if os.name == "nt" else ""

        self.prompts[SHELL].setText(
            orange_prompt(prefix + location + " >>")
        )

        self.entries[SHELL].setToolTip(
            location
            + "\nTab / Shift+Tab: autocompletar"
            + "\n↑/↓: historial"
            + "\nCtrl+L: limpiar"
            + "\nCtrl+Shift+C: terminar sesión"
        )

    def start_shell(self):
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )

        self.proc = subprocess.Popen(
            shell_program(self.marker),
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **options,
        )

        process = self.proc
        marker = self.marker
        # Este hilo lee texto y publica avisos en la cola de Qt; los widgets
        # solo se actualizan cuando la interfaz recibe esos avisos.
        def reader():
            try:
                for raw_line in process.stdout:
                    line = raw_line.rstrip("\r\n")

                    if line.startswith(marker):
                        try:
                            location = base64.b64decode(
                                line[len(marker):]
                            ).decode("utf-8")

                            self.enqueue_event("ready", location)

                        except Exception:
                            self.enqueue_event("text", line)

                    else:
                        self.enqueue_event("text", line)

            finally:
                process.stdout.close()

                try:
                    process.stdin.close()
                except OSError:
                    pass

                self.enqueue_event("exit", process.wait())

        threading.Thread(
            target=reader,
            daemon=True,
        ).start()

    def send_shell(self, command):
        if self.proc is None:
            self.start_shell()

        payload = base64.b64encode(command.encode("utf-8")).decode("ascii")
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        self.shell_busy = True

    def execute_shell(self):
        self.completion_cache = None
        if self.shell_busy:
            self.write(
                SHELL,
                "Hay un comando en ejecución. "
                "Ctrl+Shift+C termina la sesión.",
            )
            return

        command = self.entries[SHELL].text

        if not command.strip():
            return

        self.entries[SHELL].clear()
        self.remember(SHELL, command)

        if command.strip().lower() in ("clear", "cls"):
            self.outputs[SHELL].clear()
            return

        # Copiar el comando al historial superior.
        # El campo de entrada permanece vacío y fijo abajo.
        self.write(SHELL, self.cwd + "> " + command)

        try:
            self.send_shell(command)

        except Exception:
            self.shell_busy = False
            self.write(SHELL, traceback.format_exc())

    def apply_completion(self, direction):
        cache = self.completion_cache

        if not cache or not cache["matches"]:
            return

        if cache["choice"] is None:
            cache["choice"] = 0 if direction > 0 else len(cache["matches"]) - 1
        else:
            cache["choice"] = (
                cache["choice"] + direction
            ) % len(cache["matches"])

        replacement = cache["matches"][cache["choice"]]

        original = cache["original"].encode("utf-16-le")
        start = cache["start"] * 2
        end = start + cache["length"] * 2

        text = (
            original[:start]
            + replacement.encode("utf-16-le")
            + original[end:]
        ).decode("utf-16-le")

        cursor = cache["start"] + utf16_length(replacement)

        self.entries[SHELL].setText(text)
        self.entries[SHELL].setCursorPosition(cursor)

        cache["shown"] = text
        cache["cursor"] = cursor

    def complete_powershell(self, direction=1):
        if os.name != "nt" or self.shell_busy:
            return

        entry = self.entries[SHELL]
        text = str(qt_value(entry, "text"))
        cursor = int(qt_value(entry, "cursorPosition"))

        cache = self.completion_cache

        # Pulsaciones sucesivas recorren las mismas alternativas.
        if (
            cache
            and text == cache["shown"]
            and cursor == cache["cursor"]
            and self.cwd == cache["cwd"]
        ):
            self.apply_completion(direction)
            return

        self.completion_cache = None

        request = {
            "original": text,
            "cursor": cursor,
            "direction": direction,
        }

        # Transportar el texto sin problemas de comillas o caracteres especiales.
        encoded_text = base64.b64encode(
            text.encode("utf-8")
        ).decode("ascii")

        ps_command = r"""
& {
    try {
        $__pct_text = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String('__TEXT__')
        )

        $__pct_result =
            [System.Management.Automation.CommandCompletion]::CompleteInput(
                $__pct_text,
                __CURSOR__,
                $null
            )

        $__pct_data = @{
            start = $__pct_result.ReplacementIndex
            length = $__pct_result.ReplacementLength
            matches = @(
                $__pct_result.CompletionMatches |
                    ForEach-Object { $_.CompletionText }
            )
        }
    }
    catch {
        $__pct_data = @{
            start = 0
            length = 0
            matches = @()
            error = $_.ToString()
        }
    }

    $__pct_json = ConvertTo-Json -InputObject $__pct_data -Compress
    $__pct_encoded = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($__pct_json)
    )

    [Console]::WriteLine('__MARKER__' + $__pct_encoded)
}
"""
        ps_command = (
            ps_command
            .replace("__TEXT__", encoded_text)
            .replace("__CURSOR__", str(cursor))
            .replace("__MARKER__", self.completion_marker)
        )

        try:
            self.completion_pending = request
            self.send_shell(ps_command)

        except Exception:
            self.shell_busy = False
            self.completion_pending = None
            self.write(SHELL, traceback.format_exc())

    def enqueue_event(self, kind, value):
        self.events.put((kind, value))
        pya.QCoreApplication.postEvent(self.dock, pya.QEvent(pya.QEvent.User))

    def poll(self):
        self.update_partial_notice()
        for _ in range(100):
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "text":
                if value.startswith(self.completion_marker):
                    request = self.completion_pending

                    try:
                        data = json.loads(
                            base64.b64decode(
                                value[len(self.completion_marker):]
                            ).decode("utf-8")
                        )

                        if data.get("error"):
                            self.write(SHELL, data["error"])
                            continue

                        if request is None:
                            continue

                        entry = self.entries[SHELL]

                        # No sobrescribir si escribiste mientras llegaba la respuesta.
                        if (
                            str(qt_value(entry, "text")) != request["original"]
                            or int(qt_value(entry, "cursorPosition"))
                            != request["cursor"]
                        ):
                            continue

                        matches = data.get("matches") or []
                        if isinstance(matches, str):
                            matches = [matches]

                        start = int(data["start"])
                        length = int(data["length"])

                        if (
                            start < 0
                            or length < 0
                            or start + length > utf16_length(request["original"])
                        ):
                            continue

                        self.completion_cache = {
                            "original": request["original"],
                            "start": start,
                            "length": length,
                            "matches": matches,
                            "choice": None,
                            "shown": request["original"],
                            "cursor": request["cursor"],
                            "cwd": self.cwd,
                        }

                        self.apply_completion(request["direction"])

                    except Exception:
                        self.write(SHELL, traceback.format_exc())

                elif value:
                    self.write(SHELL, value)

            elif event == "pdk_progress":
                if self.pdk_installing:
                    self.update_pdk_progress(value)

            elif event == "pdk":
                self.pdk_installing = False
                self.timer.stop()
                self.pdk_progress.hide()
                if isinstance(value, dict):
                    self.write(PYTHON, pocket_pdk.ascii_table(("Campo", "Resultado"), [
                        ("PDK", value["name"]), ("Operacion", value["action"]),
                        ("Estado", "OK" if value["success"] else "ERROR"),
                        ("Detalle", value["message"]),
                    ]))
                    if value["restart"]:
                        self.restart_pending = True
                        self.write(PYTHON, "Reiniciar KLayout ahora? [y/n]\n"
                                   "Responde al terminar el bloque Python si hay uno pendiente.\n"
                                   "Otra entrada pospone el reinicio; tambien puedes usar restart.")
                        if not self.more:
                            self.prompts[PYTHON].setText(orange_prompt("reiniciar [y/n] >>"))
                else:
                    self.write(PYTHON, value)

            elif event == "ready":
                self.shell_busy = False
                self.completion_pending = None

                if Path(value).is_dir():
                    self.cwd = value

                self.update_shell_prompt(value)

            elif event == "exit":
                self.proc = None
                self.shell_busy = False
                self.completion_pending = None
                self.completion_cache = None
                self.write(
                    SHELL,
                    "[Sesión terminada. El siguiente comando abrirá una nueva.]",
                )

        if self.pdk_installing:
            self.animate_pdk_progress()

    def stop_shell(self):
        process = self.proc

        if process is None or process.poll() is not None:
            return

        # Termina la sesión completa, no emula Ctrl+C.
        if os.name == "nt":
            subprocess.Popen(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def fit(self):
        self.view().zoom_fit()

    def bind_fit(self):
        mw = self.mw

        defaults = dict(mw.get_default_key_bindings())
        current = dict(mw.get_key_bindings())

        effective = defaults.copy()
        effective.update(current)

        candidates = [
            path
            for path in effective
            if path.split(".")[-1] == "zoom_fit"
        ]

        if not candidates:
            related = [
                path for path in effective
                if "fit" in path.lower()
            ]
            raise RuntimeError(
                "No encontré la acción zoom_fit. "
                "Acciones disponibles relacionadas: "
                + repr(related)
            )

        target = next(
            (path for path in candidates if not path.startswith("@")),
            candidates[0],
        )

        # Conservar los demás atajos personalizados.
        updated = current.copy()

        # Liberar F si otra acción la está usando.
        for path, shortcut in effective.items():
            if str(shortcut).strip().upper() == "F" and path != target:
                updated[path] = ""

        updated[target] = "F"
        mw.set_key_bindings(updated)

        return "Fit asignado a F: " + target


# Retirar una instancia anterior en lugar de mostrar código viejo.
previous = getattr(pya, "_pocket_console_v1", None)

if previous is not None:
    if getattr(previous, "pdk_installing", False):
        raise RuntimeError("Espera a que termine la operación del PDK.")

    process = getattr(previous, "proc", None)

    if process is not None and process.poll() is None:
        raise RuntimeError(
            "Termina la sesión anterior con exit, "
            "o reinicia KLayout antes de cargar esta versión."
        )

    previous.timer.stop()
    previous.dock.hide()
    previous.mw.removeDockWidget(previous.dock)
    previous.dock.deleteLater()

    pya._pocket_console_v1 = None

pya._pocket_console_v1 = PocketConsole()
