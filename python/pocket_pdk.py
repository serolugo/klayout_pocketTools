"""Catalog and installer for open PDK integrations in KLayout."""

from pathlib import Path, PurePosixPath, PureWindowsPath
import json
import os
import shutil
from stat import S_IFDIR, S_IFMT, S_IFREG, S_ISLNK
import subprocess
import sys
import textwrap
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import zlib

CATALOG = {
    "sky130": {
        "repository": "fossi-foundation/sky130_klayout_pdk",
        "revision": "4ea0975391feacbdac697bf07029d8908d292613",
        "technology": "tech/sky130/sky130.lyt",
        "folder": "PocketPDK_sky130",
    },
}

IHP_REVISION = "f658ac22974185eb65f27e06caa72aa72ecf6d17"
for process in ("sg13g2", "sg13cmos5l"):
    name = f"ihp-{process}"
    CATALOG[name] = {
        "repository": "IHP-GmbH/IHP-Open-PDK",
        "revision": IHP_REVISION,
        "technology": f"tech/{process}.lyt",
        "folder": f"PocketPDK_{name}",
        "local_variant": name,
        "download": "layout",
    }

for variant in "ABCD":
    name = f"gf180mcu{variant}"
    CATALOG[name.lower()] = {
        "repository": "fossi-foundation/open-pdks",
        "technology": "tech/gf180mcu.lyt",
        "folder": f"PocketPDK_{name}",
        "local_variant": name,
        "download": "local",
    }


REVISION_FILE = "pocket-pdk-revision.txt"


def ascii_table(headers, rows):
    """Readable multiline tables for the console's monospace output."""
    rows = [[str(value) for value in row] for row in rows]
    widths = [min(64, max([len(str(header)), *(len(line) for row in rows
              for line in row[index].splitlines() or [""])]))
              for index, header in enumerate(headers)]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render(row):
        cells = [[part for line in str(value).splitlines() or [""]
                  for part in textwrap.wrap(line, width=width, replace_whitespace=False,
                                            drop_whitespace=False) or [""]]
                 for value, width in zip(row, widths)]
        return ["| " + " | ".join((cell[line] if line < len(cell) else "").ljust(width)
                for cell, width in zip(cells, widths)) + " |"
                for line in range(max(map(len, cells)))]

    lines = [border, *render(headers), border]
    for row in rows:
        lines.extend(render(row))
    return "\n".join([*lines, border])


def status_details(data_dir, name, search_paths=(), *, runtime=None):
    spec = source_for(name)
    current = installed_technology(data_dir, name, search_paths)
    managed = data_dir / "salt" / spec["folder"]
    marker = managed / REVISION_FILE
    owned = current == managed / spec["technology"] and marker.is_file()
    rows = [
        ("PDK", normalize_name(name)),
        ("Estado", "Eliminacion pendiente" if removal_record(data_dir, name).is_file()
         else "Instalado" if owned else "Externo" if current else "Disponible"),
        ("Origen", spec["repository"]),
        ("Revision", marker.read_text(encoding="utf-8").strip() if owned else spec.get("revision", "Local")),
        ("Metodo", "Carpeta local" if spec.get("download") == "local" else "Descarga / carpeta local" if spec.get("local_variant") else "Descarga ZIP"),
        ("Ruta", str(managed if owned else current) if current else "-"),
    ]
    if normalize_name(name) == "sky130":
        import pocket_sky130
        rows.extend(pocket_sky130.status_rows(current, runtime))
    return rows


def list_table(data_dir, search_paths=()):
    rows = []
    for name in CATALOG:
        details = dict(status_details(data_dir, name, search_paths))
        rows.append((name, details["Estado"], details["Metodo"]))
    return ascii_table(("PDK", "Estado", "Instalacion"), rows)


def source_for(name):
    name = normalize_name(name)
    try:
        return CATALOG[name]
    except KeyError:
        raise ValueError(f"PDK desconocido: {name}. Usa pdk('list').") from None


def normalize_name(name):
    if not isinstance(name, str):
        raise ValueError("Indica un PDK. Usa pdk('list').")
    return name.strip().lower()


def installed_technology(data_dir, name, search_paths=()):
    spec = source_for(name)
    technology = spec["technology"]
    salt = data_dir / "salt"
    managed = salt / spec["folder"] / technology
    if managed.is_file():
        return managed
    for root in (data_dir, *search_paths):
        candidate = root / technology
        if candidate.is_file():
            return candidate
    for pattern in (f"*/{technology}", f"*/*/{technology}"):
        for candidate in salt.glob(pattern):
            if candidate.is_file():
                return candidate
    return None


def status(data_dir, name, search_paths=()):
    spec = source_for(name)
    current = installed_technology(data_dir, name, search_paths)
    if current is None:
        mode = spec.get("download", "archive")
        if mode == "local":
            return f"{name}: disponible para importar desde {spec['local_variant']} (Open PDKs/Ciel)"
        scope = " · archivos de layout" if mode == "layout" else ""
        return f"{name}: disponible · {spec['repository']} @ {spec['revision'][:12]}{scope}"
    managed = data_dir / "salt" / spec["folder"]
    if current == managed / spec["technology"]:
        marker = managed / REVISION_FILE
        if marker.is_file():
            return f"{name}: instalado · {marker.read_text(encoding='utf-8').strip()}\n{managed}"
    return f"{name}: ya hay una instalación externa\n{current}"


def report_progress(progress, phase, **details):
    if progress is not None:
        progress({"phase": phase, **details})


def download_file(url, destination, progress=None):
    """Prefer a separate curl process so downloading is independent of KLayout."""
    executable = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if executable:
        download_with_curl(executable, url, destination, progress)
    else:
        download_with_urllib(url, destination, progress)


def download_with_curl(executable, url, destination, progress=None):
    # curl writes directly to disk; Python only samples progress, never pipes data.
    with TemporaryDirectory(prefix="curl-", dir=destination.parent) as temporary:
        headers = Path(temporary) / "headers.txt"
        errors = Path(temporary) / "errors.txt"
        command = [
            executable, "--disable", "--location", "--fail", "--silent", "--show-error",
            "--connect-timeout", "45", "--speed-limit", "1", "--speed-time", "45",
            "--dump-header", str(headers), "--output", str(destination), "--url", url,
        ]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

        def sample():
            total = None
            if headers.exists():
                # Keep only the final response's length, excluding redirects/proxies.
                for line in headers.read_text(encoding="iso-8859-1").splitlines():
                    if line.startswith("HTTP/"):
                        total = None
                    elif line.lower().startswith("content-length:"):
                        try:
                            length = int(line.split(":", 1)[1].strip())
                            total = length if length > 0 else None
                        except ValueError:
                            total = None
            downloaded = destination.stat().st_size if destination.exists() else 0
            report_progress(progress, "download", downloaded=downloaded, total=total)

        report_progress(progress, "download", downloaded=0, total=None)
        with errors.open("wb") as error_stream:
            with subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=error_stream, **options
            ) as process:
                try:
                    while True:
                        try:
                            result = process.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            sample()
                    sample()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        if result:
            detail = errors.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(f"Falló la descarga con curl (código {result}): {detail}")


def download_with_urllib(url, destination, progress=None):
    """Stream a download without requiring a Content-Length header."""
    with urlopen(url, timeout=45) as response, destination.open("wb") as output:
        try:
            total = int(response.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            total = 0
        total = total if total > 0 else None
        downloaded = 0
        report_progress(progress, "download", downloaded=0, total=total)
        while True:
            block = response.read(256 * 1024)
            if not block:
                break
            output.write(block)
            downloaded += len(block)
            report_progress(progress, "download", downloaded=downloaded, total=total)


def copy_source(source_dir, spec, package, progress=None):
    variant = spec.get("local_variant")
    if variant is None:
        raise ValueError("Esta entrada solo admite la descarga del catálogo.")
    if source_dir.name.lower() != variant.lower():
        source_dir = source_dir / variant
    klayout = source_dir / "libs.tech" / "klayout"
    if not (klayout / spec["technology"]).is_file():
        raise FileNotFoundError(f"No encontré la integración de {variant} en {klayout}.")
    report_progress(progress, "copy")
    files = [path for path in klayout.rglob("*") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    completed = copied = 0
    report_progress(progress, "copy", completed=0, total=total, files=0, total_files=len(files))

    def copy_file(source, destination):
        nonlocal completed, copied
        result = shutil.copy2(source, destination)
        completed += Path(destination).stat().st_size
        copied += 1
        report_progress(progress, "copy", completed=completed, total=total,
                        files=copied, total_files=len(files))
        return result

    shutil.copytree(klayout, package, copy_function=copy_file)
    return f"importado de {source_dir}"


def download_layout(spec, package, progress=None):
    variant = spec["local_variant"]
    base = (
        f"https://raw.githubusercontent.com/{spec['repository']}/{spec['revision']}"
        f"/{variant}/libs.tech/klayout"
    )
    technology = PurePosixPath(spec["technology"])
    (package / "tech").mkdir(parents=True)
    for extension in (".lyt", ".lyp", ".map"):
        relative = technology.with_suffix(extension)
        download_file(f"{base}/{relative}", package / relative, progress)
    return f"{spec['revision']} (archivos de layout)"


def create_manifest(name, spec, package):
    manifest = package / "grain.xml"
    if manifest.is_file():
        return
    grain = ET.Element("salt-grain")
    for key, value in (
        ("name", spec["folder"]), ("version", "1.0.0"), ("title", f"Pocket PDK: {name}"),
        ("doc-url", f"https://github.com/{spec['repository']}"),
    ):
        ET.SubElement(grain, key).text = value
    ET.ElementTree(grain).write(manifest, encoding="utf-8", xml_declaration=True)


def install(data_dir, name, search_paths=(), source_dir=None, progress=None):
    spec = source_for(name)
    if removal_record(data_dir, name).exists():
        raise RuntimeError(f"Hay una eliminacion pendiente. Ejecuta pdk remove {name} antes de instalar.")
    if installed_technology(data_dir, name, search_paths) is not None:
        return status(data_dir, name, search_paths)

    salt = data_dir / "salt"
    target = salt / spec["folder"]
    salt.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Ya existe {target}.")

    mode = spec.get("download", "archive")
    if mode == "local" and source_dir is None:
        raise ValueError(f"Indica la carpeta de {spec['local_variant']} con source=...")

    # Stage outside Salt so KLayout sees only the complete package.
    with TemporaryDirectory(prefix="ppdk-", dir=data_dir) as temporary:
        temporary = Path(temporary)
        package = temporary / "p"
        if source_dir is not None:
            revision = copy_source(source_dir, spec, package, progress)
        elif mode == "layout":
            revision = download_layout(spec, package, progress)
        else:
            revision = download_archive(spec, temporary / "source.zip", package, progress)

        report_progress(progress, "install", completed=0, total=4)
        if not (package / spec["technology"]).is_file():
            raise ValueError("La fuente no contiene una tecnología válida.")
        report_progress(progress, "install", completed=1, total=4)
        create_manifest(name, spec, package)
        if normalize_name(name) == "sky130":
            import pocket_sky130
            pocket_sky130.instrument_startup(package)
        report_progress(progress, "install", completed=2, total=4)
        (package / REVISION_FILE).write_text(revision + "\n", encoding="utf-8")
        report_progress(progress, "install", completed=3, total=4)
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Ya existe {target}.")
        if installed_technology(data_dir, name, search_paths):
            return status(data_dir, name, search_paths)
        package.rename(target)
        report_progress(progress, "install", completed=4, total=4)

    message = f"{name} instalado en {target}\nReinicia KLayout para cargar la tecnología."
    if normalize_name(name) == "sky130":
        message += "\npdk status sky130 comprueba por separado la tecnologia y las PCells (skywater130)."
    return message


def removal_record(data_dir, name):
    source_for(name)
    return data_dir / f".pocket-pdk-remove-{normalize_name(name)}.json"


def removal_paths(data_dir, name):
    """Resolve and check the final target again in both parent and child."""
    spec = source_for(name)
    salt = (data_dir / "salt").resolve()
    target = salt / spec["folder"]
    if target.is_symlink() or target.resolve() != target or target.parent != salt:
        raise ValueError(f"Ruta de PDK no segura para eliminar: {target}")
    record = removal_record(data_dir, name)
    if record.is_symlink() or (record.exists() and not record.is_file()):
        raise ValueError(f"Registro de eliminacion no seguro: {record}")
    return target, record


def validate_removal_record(record, name, target):
    expected = dict(version=1, name=normalize_name(name), target=str(target))
    if json.loads(record.read_text(encoding="utf-8")) != expected:
        raise ValueError("El registro de eliminacion no corresponde a este PDK.")


def matches_cached_package(data_dir, name, target):
    """Recover old interrupted removals only when all remaining files match a cached ZIP."""
    spec = source_for(name)
    if "revision" not in spec or not (target / spec["technology"]).is_file():
        return False
    prefix = f"{spec['repository'].split('/')[-1]}-{spec['revision']}/"
    for archive in data_dir.glob("ppdk-*/source.zip"):
        try:
            with ZipFile(archive) as source:
                expected = {member.filename: member for member in source.infolist()}
                matched = 0
                for root, directories, files in os.walk(target, followlinks=False):
                    for entry in [*directories, *files]:
                        path = Path(root) / entry
                        if path.is_symlink() or path.resolve() != path:
                            raise ValueError("La carpeta parcial contiene enlaces.")
                    for filename in files:
                        path = Path(root) / filename
                        member = expected.get(prefix + path.relative_to(target).as_posix())
                        if member is None or path.stat().st_size != member.file_size:
                            raise ValueError("El contenido parcial no coincide con el ZIP.")
                        checksum = 0
                        with path.open("rb") as contents:
                            while True:
                                block = contents.read(1024 * 1024)
                                if not block:
                                    break
                                checksum = zlib.crc32(block, checksum)
                        if checksum != member.CRC:
                            raise ValueError("El contenido parcial no coincide con el ZIP.")
                        matched += 1
                if matched:
                    return True
        except (OSError, ValueError, KeyError, BadZipFile):
            continue
    return False


def remove_tree_progress(target, progress=None):
    """Plan once, then delete without following directory links or junctions."""
    directories, leaves, stack = [], [], [target]
    while stack:
        directory = stack.pop()
        directories.append(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                is_directory = entry.is_dir(follow_symlinks=False)
                reparse = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400
                path = Path(entry.path)
                if is_directory and not reparse:
                    stack.append(path)
                else:
                    leaves.append((path, is_directory))
    total = len(leaves) + len(directories)

    def report(completed):
        report_progress(progress, "remove", completed=completed, total=total,
                        files=completed, total_files=total)

    report(0)
    completed = 0
    for path, is_directory in leaves:
        path.rmdir() if is_directory else path.unlink()
        completed += 1
        report(completed)
    for directory in reversed(directories):
        directory.rmdir()
        completed += 1
        report(completed)


def remove_package_worker(data_dir, name, progress=None):
    """Runs in a separate process with no GUI; the journal survives partial deletion."""
    data_dir = Path(data_dir).resolve()
    target, record = removal_paths(data_dir, name)
    if record.exists():
        validate_removal_record(record, name, target)
    elif target.exists():
        marker = target / REVISION_FILE
        marked = marker.is_file() and not marker.is_symlink()
        if not target.is_dir() or (not marked and not matches_cached_package(data_dir, name, target)):
            raise ValueError(f"La carpeta no esta identificada como instalacion de Pocket Tools: {target}")
        with record.open("x", encoding="utf-8") as output:
            json.dump(dict(version=1, name=normalize_name(name), target=str(target)), output)
    if target.exists():
        # Recheck immediately before recursive deletion. Never use a shell-built path.
        removal_paths(data_dir, name)
        remove_tree_progress(target, progress)
    if record.exists():
        record.unlink()


def remove(data_dir, name, search_paths=(), progress=None, *, klayout_executable=None):
    """Remove only a marked Pocket Tools package inside the data directory."""
    data_dir = Path(data_dir).resolve()
    target, record = removal_paths(data_dir, name)
    if record.exists():
        validate_removal_record(record, name, target)
    if not target.exists():
        if record.exists():
            record.unlink()
            return f"{name}: eliminacion completada.\nReinicia KLayout para descargar la tecnologia de la sesion."
        current = installed_technology(data_dir, name, search_paths)
        if current is not None:
            return f"{name}: instalación externa; no se eliminó\n{current}"
        return f"{name}: no está instalado por Pocket Tools."
    marker = target / REVISION_FILE
    if not target.is_dir() or marker.is_symlink():
        raise ValueError(f"La carpeta no esta identificada como instalacion de Pocket Tools: {target}")
    if not record.exists() and not marker.is_file():
        spec = source_for(name)
        if not (target / spec["technology"]).is_file() or not any(data_dir.glob("ppdk-*/source.zip")):
            raise ValueError(f"La carpeta no esta identificada como instalacion de Pocket Tools: {target}")
    report_progress(progress, "remove")
    helper = Path(__file__).with_name("pocket_pdk_remove.py").resolve()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with TemporaryDirectory(prefix="pdk-remove-", dir=data_dir) as temporary:
        request = Path(temporary) / "request.json"
        result_file = Path(temporary) / "result.json"
        progress_file = Path(temporary) / "progress.jsonl"
        request.write_text(json.dumps(dict(data_dir=str(data_dir), name=normalize_name(name),
                                          result=str(result_file), progress=str(progress_file))), encoding="utf-8")
        last_event = None

        def sample():
            nonlocal last_event
            if not progress_file.is_file():
                return
            with progress_file.open("rb") as source:
                source.seek(max(0, progress_file.stat().st_size - 4096))
                lines = source.read().split(b"\n")
            for line in reversed(lines[:-1]):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if event != last_event:
                    last_event = event
                    if progress is not None:
                        progress(event)
                break
        if klayout_executable:
            command = [str(klayout_executable), "-b", "-nc", "-r", str(helper),
                       "-rd", "pocket_remove_request=" + str(request)]
        else:
            command = [sys.executable, str(helper), str(request)]
        errors = Path(temporary) / "errors.txt"
        with errors.open("wb") as error_stream:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=error_stream, **options) as process:
                try:
                    while True:
                        try:
                            result = process.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            sample()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        sample()
        outcome = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {}
        if result or not outcome.get("success") or target.exists():
            detail = outcome.get("error") or errors.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(f"No se pudo completar la eliminacion: {detail}. Reintenta pdk remove {name}.")
    return f"{name} eliminado de {target}\nReinicia KLayout para descargar la tecnología de la sesión."


def pdk_completion_options(text, cursor):
    """Return a token range and candidates for CLI commands, never Python expressions."""
    before = text[:cursor]
    start = cursor
    while start and not text[start - 1].isspace():
        start -= 1
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    prefix = text[start:cursor]
    words = before[:start].split()
    if not words:
        options = ["pdk"] if prefix else []
    elif words == ["pdk"]:
        options = ["help", "list", "status", "install", "remove", "restart"]
    elif len(words) == 2 and words[0] == "pdk" and words[1] in ("status", "install", "remove"):
        options = [spec["local_variant"] if spec.get("download") == "local" else name
                   for name, spec in CATALOG.items()]
    elif (len(words) == 3 and words[:2] == ["pdk", "install"]
          and normalize_name(words[2]) in CATALOG):
        options = ["--source"]
    else:
        options = []
    matches = [option for option in options if option.lower().startswith(prefix.lower())]
    return start, end, matches


def download_archive(spec, archive, package, progress=None):
    repository = spec["repository"]
    revision = spec["revision"]
    url = f"https://codeload.github.com/{repository}/zip/{revision}"
    archive_root = f"{repository.split('/')[-1]}-{revision}"
    download_file(url, archive, progress)

    report_progress(progress, "extract")
    with ZipFile(archive) as source:
        members = source.infolist()
        total = sum(member.file_size for member in members if not member.is_dir())
        total_files = sum(not member.is_dir() for member in members)
        completed = extracted = 0
        # Validate the whole archive before either extractor writes any files.
        for member in members:
            path = PurePosixPath(member.filename)
            if (
                not path.parts or path.parts[0] != archive_root
                or ".." in path.parts or "\\" in member.filename
                or member.orig_filename != member.filename
                or S_ISLNK(member.external_attr >> 16)
                or S_IFMT(member.external_attr >> 16) not in (0, S_IFREG, S_IFDIR)
                or any(PureWindowsPath(part).drive or ":" in part for part in path.parts)
            ):
                raise ValueError(f"Ruta inesperada en el ZIP: {member.filename}")
        report_progress(progress, "extract", completed=0, total=total, files=0, total_files=total_files)
        executable = archive_extractor()
        if executable:
            extract_with_tar(executable, archive, package, members, total, total_files, progress)
            return revision
        for member in members:
            path = PurePosixPath(member.filename)
            if member.is_dir():
                continue
            output = package.joinpath(*path.parts[1:])
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as contents, output.open("xb") as destination:
                while True:
                    block = contents.read(1024 * 1024)
                    if not block:
                        break
                    destination.write(block)
                    completed += len(block)
                    report_progress(progress, "extract", completed=completed, total=total,
                                    files=extracted, total_files=total_files)
            extracted += 1
            report_progress(progress, "extract", completed=completed, total=total,
                            files=extracted, total_files=total_files)

    return revision


def archive_extractor():
    """Only libarchive/bsdtar supports ZIP; GNU tar needs the Python fallback."""
    executable = shutil.which("tar.exe" if os.name == "nt" else "bsdtar")
    if executable is None:
        executable = shutil.which("tar")
    if executable is None:
        return None
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        version = subprocess.run(
            [executable, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5, **options
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if version.returncode == 0 and b"bsdtar" in version.stdout.lower():
        return executable
    return None


def extract_with_tar(executable, archive, package, members, total, total_files, progress=None):
    """Extract directly to disk, sampling just the current file instead of walking the tree."""
    package.mkdir(parents=True)
    files = [(package.joinpath(*PurePosixPath(m.filename).parts[1:]), m.file_size)
             for m in members if not m.is_dir()]
    completed = extracted = 0

    def sample():
        nonlocal completed, extracted
        partial = 0
        # Bound the work per sample; each completed file is checked only once.
        for _ in range(256):
            if extracted == len(files):
                break
            path, size = files[extracted]
            try:
                partial = min(path.stat().st_size, size)
            except FileNotFoundError:
                partial = 0
                break
            if partial < size:
                break
            completed += size
            extracted += 1
            partial = 0
        report_progress(progress, "extract", completed=completed + partial, total=total,
                        files=extracted, total_files=total_files)

    command = [executable, "-xf", str(archive.resolve()), "-C", str(package.resolve()),
               "--strip-components", "1", "--no-same-owner", "--no-same-permissions"]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with TemporaryDirectory(prefix="extract-", dir=package.parent) as temporary:
        errors = Path(temporary) / "errors.txt"
        with errors.open("wb") as error_stream:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=error_stream, **options) as process:
                try:
                    while True:
                        try:
                            result = process.wait(timeout=0.25)
                            break
                        except subprocess.TimeoutExpired:
                            sample()
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        if result:
            detail = errors.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(f"Falló la extracción con tar (código {result}): {detail}")
    report_progress(progress, "extract", completed=total, total=total,
                    files=total_files, total_files=total_files)
