from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
import webbrowser
import zipfile
from collections import OrderedDict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .engine import ModelEngine
from .parser import PBIParseError, parse_project
from .platform import PlatformDB


STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 250 * 1024 * 1024
MAX_EXTRACTED = 1024 * 1024 * 1024
SESSION_COOKIE = "pbi_runner_session"
CACHE_VERSION = 1


class ReportRuntime:
    def __init__(self, source: Path, cache_path: Path | None = None):
        self.project = parse_project(source)
        self.engine = ModelEngine.for_project(source, self.project.get("model"))
        self.project["dataRuntime"] = self.engine.status()
        self.lock = threading.Lock()
        self.cache_path = cache_path
        self.query_cache: OrderedDict[str, dict] = OrderedDict()
        self._load_cache()

    def _source_signature(self) -> dict[str, int]:
        try:
            stat = self.engine.source.stat()
            return {"size": stat.st_size, "mtime": stat.st_mtime_ns}
        except OSError:
            return {"size": 0, "mtime": 0}

    @staticmethod
    def _cache_key(page_id: str, filters: list[dict]) -> str:
        return f"{page_id}:{json.dumps(filters or [], ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)}"

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if payload.get("version") != CACHE_VERSION or payload.get("source") != self._source_signature():
                return
            self.query_cache.update(payload.get("queries", {}))
            defaults = sum(key.endswith(":[]") for key in self.query_cache)
            if defaults == len(self.project.get("pages", [])):
                runtime = next(iter(self.query_cache.values())).get("runtime")
                if runtime:
                    self.project["dataRuntime"] = {**runtime, "prepared": True}
        except (OSError, ValueError, TypeError):
            self.query_cache.clear()

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        queries = {key: value for key, value in self.query_cache.items() if key.endswith(":[]")}
        payload = {"version": CACHE_VERSION, "source": self._source_signature(), "queries": queries}
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def query(self, page_id: str, filters: list[dict]) -> dict:
        page = next((item for item in self.project["pages"] if item["id"] == page_id), None)
        if not page:
            raise PBIParseError("Página não encontrada")
        key = self._cache_key(page_id, filters)
        with self.lock:
            cached = self.query_cache.pop(key, None)
            if cached is not None:
                self.query_cache[key] = cached
                return cached
            result = self.engine.query_page(page, filters)
            self.query_cache[key] = result
            while len(self.query_cache) > 64:
                self.query_cache.popitem(last=False)
            if not filters:
                self._save_cache()
            return result

    def prepare(self) -> None:
        with self.lock:
            self.engine._ensure_runtime()
            for page in self.project.get("pages", []):
                key = self._cache_key(page["id"], [])
                if key not in self.query_cache:
                    self.query_cache[key] = self.engine.query_page(page, [])
            for result in self.query_cache.values():
                result["runtime"] = {**result.get("runtime", {}), "prepared": True}
            self.project["dataRuntime"] = {**self.engine.status(), "prepared": True}
            self._save_cache()

    def close(self) -> None:
        self.engine.close()


class ApplicationState:
    def __init__(self, data_dir: Path):
        self.db = PlatformDB(data_dir)
        self._runtimes: OrderedDict[int, ReportRuntime] = OrderedDict()
        self._runtime_lock = threading.Lock()

    def runtime(self, actor: dict, report_id: int) -> ReportRuntime:
        report = self.db.get_report(actor, report_id)
        with self._runtime_lock:
            runtime = self._runtimes.pop(report_id, None)
            if runtime is None:
                source = Path(report["source_path"])
                runtime = ReportRuntime(source, self._report_dir(source) / "prepared-cache.json")
            self._runtimes[report_id] = runtime
            while len(self._runtimes) > 4:
                _, stale = self._runtimes.popitem(last=False)
                stale.close()
            return runtime

    def _report_dir(self, source: Path) -> Path:
        source = source.resolve()
        return next((parent for parent in source.parents if parent.parent == self.db.reports_dir.resolve()), source.parent)

    def store_report(self, actor: dict, workspace_id: int, filename: str, data: bytes) -> dict:
        self.db.require_workspace(actor, workspace_id, "editor")
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in {".pbix", ".zip"}:
            raise ValueError("Envie um arquivo .pbix ou um .zip contendo um projeto .pbip")
        report_dir = self.db.reports_dir / uuid.uuid4().hex
        report_dir.mkdir(parents=True)
        try:
            if suffix == ".pbix":
                source = report_dir / safe_name
                source.write_bytes(data)
                source_type = "PBIX"
            else:
                archive = report_dir / safe_name
                archive.write_bytes(data)
                project_dir = report_dir / "project"
                project_dir.mkdir()
                self._extract_zip(archive, project_dir)
                candidates = sorted(project_dir.rglob("*.pbip"), key=lambda item: (len(item.parts), str(item)))
                if not candidates:
                    raise ValueError("O ZIP não contém um arquivo .pbip")
                source = candidates[0]
                source_type = "PBIP"
            runtime = ReportRuntime(source, report_dir / "prepared-cache.json")
            try:
                name = runtime.project.get("name") or Path(safe_name).stem
                runtime.prepare()
            finally:
                runtime.close()
            return self.db.add_report(actor, workspace_id, name, safe_name, source_type, source)
        except Exception:
            shutil.rmtree(report_dir, ignore_errors=True)
            raise

    def delete_report(self, actor: dict, report_id: int) -> None:
        report = self.db.get_report(actor, report_id, "editor")
        with self._runtime_lock:
            runtime = self._runtimes.pop(report_id, None)
        if runtime:
            runtime.close()
        report_path = Path(report["source_path"]).resolve()
        report_dir = self._report_dir(report_path)
        self.db.delete_report(actor, report_id)
        if report_dir.exists() and report_dir.is_dir() and self.db.reports_dir.resolve() in report_dir.parents:
            shutil.rmtree(report_dir)

    @staticmethod
    def _extract_zip(archive: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(archive) as package:
                total = 0
                for member in package.infolist():
                    total += member.file_size
                    if total > MAX_EXTRACTED:
                        raise ValueError("O conteúdo descompactado excede 1 GB")
                    target = (destination / member.filename).resolve()
                    if target != destination and destination not in target.parents:
                        raise ValueError("ZIP contém um caminho inseguro")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
        except zipfile.BadZipFile as exc:
            raise ValueError("Arquivo ZIP inválido") from exc

    def close(self) -> None:
        with self._runtime_lock:
            for runtime in self._runtimes.values():
                runtime.close()
            self._runtimes.clear()


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "PBIRunner/0.2"

    @property
    def state(self) -> ApplicationState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, body: bytes, content_type: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, value: object, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status, headers)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    def _actor(self) -> dict:
        actor = self.state.db.user_for_token(self._token())
        if not actor:
            raise PermissionError("Faça login para continuar")
        return actor

    def _session_header(self, token: str | None, clear: bool = False) -> dict[str, str]:
        if clear:
            return {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"}
        return {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={14 * 86400}"}

    def _payload(self, length: int) -> dict:
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("JSON inválido") from exc

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            raise PermissionError("Origem da requisição não permitida")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/bootstrap":
                user = self.state.db.user_for_token(self._token())
                self._json({"configured": self.state.db.configured(), "user": user})
                return
            if path == "/api/workspaces":
                self._json(self.state.db.list_workspaces(self._actor()))
                return
            if path == "/api/users":
                self._json(self.state.db.list_users(self._actor()))
                return
            match = re.fullmatch(r"/api/workspaces/(\d+)", path)
            if match:
                self._json(self.state.db.get_workspace(self._actor(), int(match.group(1))))
                return
            match = re.fullmatch(r"/api/reports/(\d+)/project", path)
            if match:
                report_id = int(match.group(1))
                report = self.state.db.get_report(self._actor(), report_id)
                project = self.state.runtime(self._actor(), report_id).project
                self._json({**project, "reportId": report_id, "workspaceId": report["workspace_id"]})
                return
            relative = "index.html" if path == "/" else unquote(path.lstrip("/"))
            target = (STATIC / relative).resolve()
            if STATIC not in target.parents or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(target.read_bytes(), f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        except PermissionError as exc:
            self._error(str(exc), 401)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (PBIParseError, OSError, ValueError) as exc:
            self._error(str(exc))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            self._check_origin()
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_UPLOAD:
                self._error("Arquivo excede o limite de 250 MB", 413)
                return
            if path == "/api/setup":
                payload = self._payload(length)
                user, token = self.state.db.setup(payload.get("name", ""), payload.get("email", ""), payload.get("password", ""))
                self._json({"user": user}, 201, self._session_header(token))
                return
            if path == "/api/login":
                payload = self._payload(length)
                user, token = self.state.db.login(payload.get("email", ""), payload.get("password", ""))
                self._json({"user": user}, headers=self._session_header(token))
                return
            if path == "/api/logout":
                self.state.db.logout(self._token())
                self._json({"ok": True}, headers=self._session_header(None, clear=True))
                return
            actor = self._actor()
            if path == "/api/users":
                payload = self._payload(length)
                self._json(self.state.db.create_user(actor, payload.get("name", ""), payload.get("email", ""), payload.get("password", ""), payload.get("globalRole", "user")), 201)
                return
            if path == "/api/workspaces":
                payload = self._payload(length)
                self._json(self.state.db.create_workspace(actor, payload.get("name", ""), payload.get("description", "")), 201)
                return
            match = re.fullmatch(r"/api/workspaces/(\d+)/members", path)
            if match:
                payload = self._payload(length)
                self._json(self.state.db.set_member(actor, int(match.group(1)), payload.get("email", ""), payload.get("role", "viewer")))
                return
            match = re.fullmatch(r"/api/workspaces/(\d+)/reports", path)
            if match:
                filename = unquote(self.headers.get("X-Filename", "upload.pbix"))
                self._json(self.state.store_report(actor, int(match.group(1)), filename, self.rfile.read(length)), 201)
                return
            match = re.fullmatch(r"/api/reports/(\d+)/query-page", path)
            if match:
                payload = self._payload(length)
                runtime = self.state.runtime(actor, int(match.group(1)))
                self._json(runtime.query(str(payload.get("page", "")), payload.get("filters", [])))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._error(str(exc), 403 if self.state.db.user_for_token(self._token()) else 401)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (PBIParseError, OSError, ValueError, zipfile.BadZipFile) as exc:
            self._error(str(exc))

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            self._check_origin()
            match = re.fullmatch(r"/api/reports/(\d+)", path)
            if not match:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.state.delete_report(self._actor(), int(match.group(1)))
            self._json({"ok": True})
        except PermissionError as exc:
            self._error(str(exc), 403 if self.state.db.user_for_token(self._token()) else 401)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (OSError, ValueError) as exc:
            self._error(str(exc))


def default_data_dir() -> Path:
    configured = os.environ.get("PBI_RUNNER_DATA")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "share" / "pbi-runner"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="PBI Runner multiusuário para PBIX/PBIP")
    parser.add_argument("file", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="diretório do banco e dos relatórios")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    state = ApplicationState(args.data_dir.resolve())
    server = ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    server.state = state  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}"
    print(f"PBI Runner disponível em {url}")
    print(f"Dados persistentes em {args.data_dir.resolve()}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        state.close()
        server.server_close()


if __name__ == "__main__":
    main()
