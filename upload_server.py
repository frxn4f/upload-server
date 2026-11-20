#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import html
import json
import logging
import mimetypes
import os
import tempfile
import uuid
from datetime import datetime
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


VERSION = "1.0"
DEFAULT_MAX_MB = 500
CHUNK_SIZE = 64 * 1024
CLIENT_TIMEOUT = 60
MAX_FILENAME_LENGTH = 240
MAX_PARTS = 40
MAX_FILES = 20
MAX_PART_HEADERS = 64 * 1024

console = Console(highlight=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[
        RichHandler(
            console=console,
            show_time=True,
            show_level=False,
            show_path=False,
            markup=False,
            rich_tracebacks=True,
        )
    ],
)
log = logging.getLogger("upload-server")


class UploadError(Exception):
    """Error controlado durante una transferencia."""


class InvalidRequest(UploadError):
    """La petición no tiene el formato esperado."""


class RequestTooLarge(UploadError):
    """La petición supera el límite configurado."""


class RequestBody:
    def __init__(self, stream, length: int):
        self.stream = stream
        self.remaining = length
        self.buffer = bytearray()

    def _fill(self, minimum: int = 1) -> None:
        while len(self.buffer) < minimum and self.remaining > 0:
            amount = min(CHUNK_SIZE, self.remaining)
            chunk = self.stream.read(amount)
            if not chunk:
                raise InvalidRequest("La conexión se cerró antes de completar la subida.")
            self.buffer.extend(chunk)
            self.remaining -= len(chunk)

    def readline(self, limit: int = MAX_PART_HEADERS) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > limit:
                    raise InvalidRequest("Una cabecera multipart es demasiado larga.")
                line = bytes(self.buffer[: newline + 1])
                del self.buffer[: newline + 1]
                return line

            if len(self.buffer) >= limit:
                raise InvalidRequest("Una cabecera multipart supera el límite permitido.")

            previous_size = len(self.buffer)
            self._fill(previous_size + 1)
            if len(self.buffer) == previous_size:
                line = bytes(self.buffer)
                self.buffer.clear()
                return line

    def copy_until(self, marker: bytes, output) -> None:
        keep = len(marker) - 1

        while True:
            position = self.buffer.find(marker)
            if position >= 0:
                if output is not None and position:
                    output.write(self.buffer[:position])
                del self.buffer[: position + len(marker)]
                return

            safe_length = len(self.buffer) - keep
            if safe_length > 0:
                if output is not None:
                    output.write(self.buffer[:safe_length])
                del self.buffer[:safe_length]

            previous_size = len(self.buffer)
            self._fill(previous_size + 1)
            if len(self.buffer) == previous_size:
                raise InvalidRequest("No se encontró el cierre de una parte multipart.")


class FileManager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def safe_name(filename: str) -> str:
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(
            char for char in filename
            if char.isprintable() and char not in '<>:"|?*'
        ).strip(" .")
        return (filename or "archivo.bin")[:MAX_FILENAME_LENGTH]

    def create_temp(self) -> tuple[int, Path]:
        fd, name = tempfile.mkstemp(
            prefix=".upload-",
            suffix=".part",
            dir=self.directory,
        )
        return fd, Path(name)

    def commit(self, temporary: Path, filename: str) -> Path:
        filename = self.safe_name(filename)
        original = Path(filename)

        for attempt in range(12):
            if attempt == 0:
                candidate = filename
            else:
                candidate = f"{original.stem}_{uuid.uuid4().hex[:8]}{original.suffix}"

            destination = self.directory / candidate
            try:
                # El enlace es atómico: el archivo aparece completo o no aparece.
                os.link(temporary, destination)
            except FileExistsError:
                continue
            except OSError as error:
                message = error.strerror or str(error)
                raise OSError(
                    error.errno,
                    f"No se pudo guardar el archivo en {self.directory}: {message}",
                ) from error

            temporary.unlink(missing_ok=True)
            return destination

        raise OSError(errno.EEXIST, "No se pudo encontrar un nombre de archivo libre.")

    def list_files(self) -> list[dict]:
        result = []
        for path in self.directory.iterdir():
            try:
                if not path.is_file() or path.name.startswith(".upload-"):
                    continue
                stat = path.stat()
                result.append({
                    "name": path.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
            except OSError:
                continue
        return sorted(result, key=lambda item: item["modified"], reverse=True)

    def find(self, filename: str) -> Path | None:
        name = self.safe_name(filename)
        path = self.directory / name
        try:
            return path if path.is_file() else None
        except OSError:
            return None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def log_transfer(kind: str, client: str, filename: str, size: int) -> None:
    line = Text()
    style = "bold green" if kind == "SUBIDA" else "bold cyan"
    line.append(kind, style=style)
    line.append("  ")
    line.append(client, style="dim")
    line.append("  →  ")
    line.append(filename, style="bold #9780ff")
    line.append(f"  ({human_size(size)})", style="dim")
    console.print(line)


def show_banner(host: str, port: int, directory: Path, max_mb: int) -> None:
    details = Table.grid(padding=(0, 2))
    details.add_column(style="dim")
    details.add_column(style="#9780ff")
    details.add_row("DIRECCIÓN", f"http://{host}:{port}")
    details.add_row("DESTINO", str(directory))
    details.add_row("LÍMITE", f"{max_mb} MB por petición")
    details.add_row("VERSIÓN", VERSION)

    console.print()
    console.print(Panel(
        details,
        title="[bold #9780ff]UPLOAD SERVER[/bold #9780ff]",
        subtitle="transferencia de archivos",
        border_style="#9780ff",
        box=box.ROUNDED,
        padding=(1, 2),
    ))
    console.print("[dim]Ctrl+C para detener el servidor.[/dim]\n")


class UploadServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, handler, files: FileManager):
        super().__init__(address, handler)
        self.files = files


class UploadHandler(BaseHTTPRequestHandler):
    server_version = "UploadServer"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(CLIENT_TIMEOUT)

    def log_message(self, fmt, *args):
        log.info("%s  %s", self.client_address[0], fmt % args)

    @property
    def files(self) -> FileManager:
        return self.server.files

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def fail(self, status: int, message: str, close: bool = False) -> None:
        if close:
            self.close_connection = True
        self.send_json(status, {"success": False, "error": message, "status": status})

    def send_html(self, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def content_length(self) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise InvalidRequest("No se admite Transfer-Encoding; envía Content-Length.")

        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1:
            raise InvalidRequest("Falta una cabecera Content-Length válida.")

        try:
            length = int(values[0])
        except ValueError as error:
            raise InvalidRequest("Content-Length no es un número válido.") from error

        if length <= 0:
            raise InvalidRequest("La petición está vacía.")
        if length > self.server.max_size:
            raise RequestTooLarge(
                f"La petición supera el límite de {human_size(self.server.max_size)}."
            )
        return length

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        try:
            if path == "/":
                return self.index()
            if path == "/files":
                return self.send_json(200, {"success": True, "files": self.files.list_files()})
            if path == "/health":
                return self.send_json(200, {"success": True, "status": "online", "version": VERSION})
            if path.startswith("/download/"):
                return self.download(path[len("/download/"):])
            return self.fail(404, "No existe esa ruta.")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            log.exception("Error atendiendo GET")
            return self.fail(500, "Error interno del servidor.")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/upload":
            return self.fail(404, "No existe esa ruta.")

        try:
            length = self.content_length()
            content_type = self.headers.get("Content-Type", "")
            if content_type.lower().startswith("multipart/form-data"):
                saved = self.receive_multipart(length, content_type)
            else:
                saved = [self.receive_raw(length)]

            self.send_json(200, {"success": True, "count": len(saved), "files": saved})

        except RequestTooLarge as error:
            return self.fail(413, str(error), close=True)
        except InvalidRequest as error:
            return self.fail(400, str(error), close=True)
        except TimeoutError:
            return self.fail(408, "La subida tardó demasiado. Comprueba la conexión y vuelve a intentarlo.", close=True)
        except OSError as error:
            log.exception("No se pudo guardar la subida")
            message = error.strerror or str(error) or "Error de almacenamiento."
            return self.fail(500, f"No se pudo guardar el archivo: {message}", close=True)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            log.exception("Error inesperado durante la subida")
            return self.fail(500, "Error inesperado. Consulta la salida del servidor.", close=True)

    def receive_raw(self, length: int) -> dict:
        filename = self.files.safe_name(self.headers.get("X-Filename", "archivo.bin"))
        fd, temporary = self.files.create_temp()
        try:
            total = 0
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise InvalidRequest("La conexión se cerró antes de completar la subida.")
                    output.write(chunk)
                    total += len(chunk)
                    remaining -= len(chunk)
            destination = self.files.commit(temporary, filename)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        log_transfer("SUBIDA", self.client_address[0], destination.name, total)
        return {"name": destination.name, "size": total}

    def receive_multipart(self, length: int, content_type: str) -> list[dict]:
        params = Message()
        params["Content-Type"] = content_type
        boundary = params.get_param("boundary", header="Content-Type")
        if not boundary or len(boundary) > 200:
            raise InvalidRequest("El boundary multipart no es válido.")
        try:
            boundary_bytes = boundary.encode("ascii")
        except UnicodeEncodeError as error:
            raise InvalidRequest("El boundary multipart debe ser ASCII.") from error

        reader = RequestBody(self.rfile, length)
        opening = reader.readline(256)
        if opening != b"--" + boundary_bytes + b"\r\n":
            raise InvalidRequest("El cuerpo multipart no comienza con un boundary válido.")

        marker = b"\r\n--" + boundary_bytes
        saved = []
        parts = 0

        while True:
            parts += 1
            if parts > MAX_PARTS:
                raise InvalidRequest(f"La petición supera el máximo de {MAX_PARTS} partes.")

            headers = bytearray()
            while True:
                line = reader.readline(8192)
                if not line:
                    raise InvalidRequest("Faltan las cabeceras de una parte multipart.")
                if line == b"\r\n":
                    break
                headers.extend(line)
                if len(headers) > MAX_PART_HEADERS:
                    raise InvalidRequest("Las cabeceras multipart son demasiado grandes.")

            part = BytesParser(policy=default).parsebytes(bytes(headers) + b"\r\n")
            filename = part.get_filename()
            disposition = part.get_content_disposition()

            if disposition == "form-data" and filename is not None:
                if len(saved) >= MAX_FILES:
                    raise InvalidRequest(f"Se permiten como máximo {MAX_FILES} archivos por subida.")

                fd, temporary = self.files.create_temp()
                try:
                    with os.fdopen(fd, "wb") as output:
                        reader.copy_until(marker, output)
                        size = output.tell()
                    destination = self.files.commit(temporary, filename)
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise

                log_transfer("SUBIDA", self.client_address[0], destination.name, size)
                saved.append({"name": destination.name, "size": size})
            else:
                reader.copy_until(marker, None)

            ending = reader.readline(128)
            if ending == b"--\r\n" or ending == b"--":
                break
            if ending != b"\r\n":
                raise InvalidRequest("El cierre de una parte multipart no es válido.")

        if not saved:
            raise InvalidRequest("No se ha recibido ningún archivo.")
        return saved

    def download(self, filename: str) -> None:
        path = self.files.find(filename)
        if path is None:
            return self.fail(404, "No se ha encontrado el archivo.")

        try:
            size = path.stat().st_size
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            log_transfer("DESCARGA", self.client_address[0], path.name, size)
            with path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            log.warning("Descarga interrumpida: %s (%s)", path.name, self.client_address[0])
        except OSError:
            log.exception("No se pudo enviar %s", path.name)

    def index(self) -> None:
        files = self.files.list_files()
        rows = []
        for item in files:
            name = html.escape(item["name"])
            date = html.escape(item["modified"])
            url = quote(item["name"], safe="")
            rows.append(
                f'<div class="file"><div><strong>{name}</strong>'
                f'<small>{human_size(item["size"])} · {date}</small></div>'
                f'<a href="/download/{url}">Descargar</a></div>'
            )

        file_list = "".join(rows) or '<p class="muted">Todavía no hay archivos.</p>'
        page = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upload Server</title>
<style>
:root {{ color-scheme: dark; --bg:#0b0b12; --panel:#13131d; --line:#29293b; --accent:#9780ff; --text:#eeeef5; --muted:#85859b; }}
* {{ box-sizing:border-box; }}
body {{ max-width:760px; margin:64px auto; padding:0 20px; background:var(--bg); color:var(--text); font-family:system-ui,sans-serif; }}
header {{ margin-bottom:32px; }}
h1 {{ letter-spacing:-1px; }}
header p,small,.muted {{ color:var(--muted); }}
.panel {{ padding:24px; margin-bottom:20px; border:1px solid var(--line); border-radius:14px; background:var(--panel); }}
.drop {{ display:block; padding:38px 16px; border:2px dashed var(--line); border-radius:12px; text-align:center; cursor:pointer; transition:border-color .2s,background .2s; }}
.drop:hover,.drop.active {{ border-color:var(--accent); background:#9780ff0d; }}
input {{ display:none; }}
button {{ width:100%; margin-top:18px; padding:13px; border:0; border-radius:8px; background:var(--accent); color:white; font-weight:700; cursor:pointer; }}
button:disabled {{ opacity:.55; cursor:wait; }}
.file {{ display:flex; justify-content:space-between; align-items:center; gap:16px; padding:14px 0; border-bottom:1px solid var(--line); }}
.file:last-child {{ border:0; }}
.file strong {{ overflow-wrap:anywhere; }}
.file small {{ display:block; margin-top:5px; }}
a {{ color:var(--accent); text-decoration:none; white-space:nowrap; }}
progress {{ width:100%; margin-top:18px; accent-color:var(--accent); }}
#status {{ margin-top:12px; overflow-wrap:anywhere; color:var(--muted); }}
@media(max-width:520px) {{ body {{ margin-top:32px; }} .file {{ align-items:flex-start; }} }}
</style>
</head>
<body>
<header><h1>UPLOAD <span style="color:#9780ff">SERVER</span></h1><p>Transferencia de archivos · v{VERSION}</p></header>
<section class="panel">
<form id="form">
<label class="drop" id="drop"><h3>Suelta los archivos aquí</h3><p>o pulsa para seleccionarlos</p><input id="files" type="file" multiple><div id="selection"></div></label>
<button id="submit" type="submit">Subir archivos</button>
<progress id="progress" value="0" max="100" hidden></progress><div id="status" role="status" aria-live="polite"></div>
</form>
</section>
<section class="panel"><h3>Archivos recibidos ({len(files)})</h3>{file_list}</section>
<script>
const input=document.getElementById('files'),drop=document.getElementById('drop'),form=document.getElementById('form');
const status=document.getElementById('status'),progress=document.getElementById('progress'),submit=document.getElementById('submit');
input.onchange=()=>document.getElementById('selection').textContent=input.files.length+' archivo(s) seleccionado(s)';
['dragenter','dragover'].forEach(type=>drop.addEventListener(type,event=>{{event.preventDefault();drop.classList.add('active')}}));
['dragleave','drop'].forEach(type=>drop.addEventListener(type,event=>{{event.preventDefault();drop.classList.remove('active')}}));
drop.addEventListener('drop',event=>{{input.files=event.dataTransfer.files;input.dispatchEvent(new Event('change'))}});
form.onsubmit=event=>{{
 event.preventDefault(); if(!input.files.length){{status.textContent='Selecciona al menos un archivo.';return}}
 const data=new FormData(); for(const file of input.files)data.append('file',file);
 const xhr=new XMLHttpRequest(); xhr.open('POST','/upload'); submit.disabled=true; progress.hidden=false; progress.value=0;
 xhr.upload.onprogress=event=>{{if(event.lengthComputable){{progress.value=event.loaded/event.total*100;status.textContent='Subiendo… '+Math.round(progress.value)+'%'}}}};
 xhr.onload=()=>{{submit.disabled=false;let result;try{{result=JSON.parse(xhr.responseText)}}catch{{status.textContent='El servidor devolvió una respuesta no válida.';return}}
 if(xhr.status>=200&&xhr.status<300&&result.success){{status.textContent='Subida completada.';location.reload()}}else{{status.textContent=result.error||'Error HTTP '+xhr.status}}}};
 xhr.onerror=()=>{{submit.disabled=false;status.textContent='No se pudo conectar con el servidor.'}};
 xhr.ontimeout=()=>{{submit.disabled=false;status.textContent='La subida ha superado el tiempo de espera.'}};
 status.textContent='Preparando subida…';xhr.send(data);
}};
</script>
</body>
</html>"""
        self.send_html(page)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Servidor HTTP para transferir archivos",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("port", nargs="?", type=int, default=8000, help="Puerto HTTP")
    parser.add_argument("--host", default="0.0.0.0", help="Dirección de escucha")
    parser.add_argument("--dir", default="uploads", help="Carpeta donde guardar los archivos")
    parser.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB, help="Tamaño máximo de cada petición, en MB")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("El puerto debe estar entre 1 y 65535.")
    if args.max_mb < 1:
        parser.error("--max-mb debe ser mayor que cero.")
    return args


def main():
    args = parse_args()
    directory = Path(args.dir).expanduser().resolve()
    max_size = args.max_mb * 1024 * 1024

    try:
        files = FileManager(directory)
        server = UploadServer((args.host, args.port), UploadHandler, files)
        server.max_size = max_size
    except PermissionError:
        console.print("[bold red]Error:[/bold red] no hay permisos para crear la carpeta o abrir el puerto.")
        raise SystemExit(1)
    except OSError as error:
        console.print(f"[bold red]No se pudo iniciar el servidor:[/bold red] {error}")
        raise SystemExit(1)

    show_banner(args.host, args.port, directory, args.max_mb)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Cerrando servidor…[/bold yellow]")
    finally:
        server.server_close()
        console.print("[dim]Servidor detenido.[/dim]")


if __name__ == "__main__":
    main()
