"""Local preview server: static files + live reload (SSE) + room selection.

GET  /            index.html of the building
GET  /events      Server-Sent Events; emits `update` when indoor.geojson changes
POST /selection   stores the room clicked in the browser to selection.json
POST /alignment   stores floor-map point pairs picked in the browser to alignment.json, then
                  applies them (`main.py align --apply`: config basemap + build) and returns the report
POST /nudge       {"rotate_deg", "scale", "east_m", "north_m", "level"?}: moves every plan and floor map (only that floor's with `level`) by that (`nudge-map`) and builds
GET  /floor-export?level=N   the floor's rooms as GeoJSON to edit in geojson.io (`export-floor`)
POST /floor-import[?dry=1]   an edited floor GeoJSON → applied (`import-floor`) and built; dry=1 only reports the changes
POST /room-edits   stores room-shape edits to room_edits.json, then applies them (`edit-rooms`) and builds
POST /alignment/reset  {"level": n}: puts that floor's map back to its automatic placement (`align --reset`)
GET  /buildings   every built building under output/ and the one being served
POST /switch      {"slug": ...}: serve another built building (the page then reloads)
GET  /config      the current config (read-only)
GET  /cost.json   this building's estimated Claude usage cost, recomputed from the session logs
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POST_FILES = {"/selection": "selection.json", "/alignment": "alignment.json", "/room-edits": "room_edits.json"}
MAX_BODY = {"/room-edits": 1_000_000}
MAIN = Path(__file__).resolve().parents[1] / "main.py"
APPLY_TIMEOUT_SECONDS = 300
_apply_lock = threading.Lock()  # one config write + build at a time
POLL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15


class Served:
    """The building being served; shared by all requests, switched from the page's picker."""
    root: Path
    out_dir: Path


def list_buildings(root: Path) -> list[dict]:
    items = []
    for d in sorted(root.iterdir()):
        if d.name.startswith("_") or not (d / "index.html").exists():
            continue
        name = d.name
        try:
            name = json.loads((d / "indoor.geojson").read_text(encoding="utf-8"))["metadata"]["building"] or name
        except (OSError, ValueError, KeyError, TypeError):
            pass
        items.append({"slug": d.name, "name": name})
    return items


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
WILDCARD_HOSTS = {"", "0.0.0.0", "::"}


def _host_name(netloc: str) -> str:
    """The host part of a Host header / Origin netloc (port and IPv6 brackets handled)."""
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.split(":")[0]


class PreviewHandler(SimpleHTTPRequestHandler):
    served: type[Served]
    allowed_hosts: set[str] | None = None  # None: bound to every interface, so any Host is accepted

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.served.out_dir), **kwargs)

    @property
    def out_dir(self) -> Path:
        return self.served.out_dir

    def log_message(self, fmt, *args):  # keep the agent's background output short
        if not self.path.startswith("/events"):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, post: bool = False) -> bool:
        """Refuse DNS rebinding (a Host that is not ours) and cross-site POSTs (an Origin that is not this page):
        this server writes configs and plans, and any web page can reach localhost."""
        host = self.headers.get("Host", "")
        if self.allowed_hosts is not None and _host_name(host) not in self.allowed_hosts:
            self._json(403, {"error": f"unexpected Host {host!r}"})
            return False
        origin = self.headers.get("Origin")
        if post and origin is not None:  # browsers always send it on cross-site POSTs; curl / agents send none
            if origin.split("://", 1)[-1] != host:
                self._json(403, {"error": f"cross-origin request from {origin!r} refused"})
                return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        if self.path.startswith("/events"):
            return self._events()
        if self.path.split("?")[0] == "/cost.json":
            self._refresh_cost()  # then served as a static file
        if self.path.split("?")[0] in ("/", "/index.html"):
            from indoor_gen import build_preview
            build_preview.refresh(self.out_dir / "index.html")   # a template change shows without a rebuild
        if self.path == "/buildings":
            return self._json(200, {"current": self.out_dir.name,
                                    "items": list_buildings(self.served.root)})
        if self.path.startswith("/floor-export"):
            return self._floor_export()
        if self.path == "/config":
            info = json.loads((self.out_dir / "build_info.json").read_text(encoding="utf-8"))
            return self._json(200, json.loads(Path(info["config_path"]).read_text(encoding="utf-8")))
        return super().do_GET()

    def _refresh_cost(self):
        from indoor_gen import usage_cost

        root = MAIN.parent
        try:
            info = json.loads((self.out_dir / "build_info.json").read_text(encoding="utf-8"))
            cost = usage_cost.for_slug(usage_cost.estimate(root, usage_cost.known_slugs(root)), info["slug"])
            (self.out_dir / "cost.json").write_text(json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:  # keep serving the cost.json the last build wrote
            self.log_message("cost estimate failed: %s", e)

    def do_POST(self):
        if not self._allowed(post=True):
            return
        if self.path == "/alignment/reset":
            return self._reset_alignment()
        if self.path == "/switch":
            return self._switch()
        if self.path == "/nudge":
            return self._nudge()
        if self.path.split("?")[0] == "/floor-import":
            return self._floor_import()
        name = POST_FILES.get(self.path)
        if name is None:
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY.get(self.path, 100_000):
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        path = self.out_dir / name
        if body is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.path == "/alignment" and body is not None:
            return self._apply_alignment()
        if self.path == "/room-edits" and body is not None:
            return self._run_cli("edit-rooms", [])
        return self._json(200, {"ok": True})

    def _floor_export(self):
        from urllib.parse import parse_qs, urlparse

        from indoor_gen import geojsonio, room_edit

        try:
            level = int(parse_qs(urlparse(self.path).query)["level"][0])
            fc = geojsonio.export_floor(self.out_dir / "indoor.geojson", self.out_dir.name, level)
        except (KeyError, ValueError, IndexError):
            return self._json(400, {"error": "expected ?level=<int>"})
        except (room_edit.EditError, OSError) as e:
            return self._json(404, {"error": str(e)})
        body = json.dumps(fc, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{self.out_dir.name}-floor-{level}.geojson"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _floor_import(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY["/room-edits"] * 5:
            return self._json(413, {"error": "too large"})
        raw = self.rfile.read(length)
        try:
            json.loads(raw or b"null")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "error": "invalid JSON"})
        path = self.out_dir / "floor_import.geojson"
        path.write_bytes(raw)
        dry = "dry=1" in self.path
        return self._run_cli("import-floor", ["--file", str(path), *(["--dry-run"] if dry else [])])

    NUDGE_LIMITS = {"rotate_deg": (-10, 10, "--rotate-deg"), "scale": (0.2, 5.0, "--scale"),
                    "east_m": (-1000, 1000, "--east-m"), "north_m": (-1000, 1000, "--north-m")}

    def _nudge(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
            flags = []
            for key, (lo, hi, flag) in self.NUDGE_LIMITS.items():
                value = float(body.get(key, 1.0 if key == "scale" else 0.0))
                if not lo <= value <= hi:
                    return self._json(400, {"ok": False, "error": f"{key} must be between {lo} and {hi}"})
                flags += [flag, repr(value)]
            if body.get("level") is not None:
                flags += ["--level", str(int(body["level"]))]
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            return self._json(400, {"ok": False, "error": "expected {\"rotate_deg\", \"scale\", \"east_m\", \"north_m\"} numbers and an optional integer \"level\""})
        return self._run_cli("nudge-map", flags)

    def _switch(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            slug = json.loads(self.rfile.read(min(length, 1000)) or b"{}")["slug"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._json(400, {"error": "expected {\"slug\": <string>}"})
        if slug not in {b["slug"] for b in list_buildings(self.served.root)}:
            return self._json(404, {"error": f"no built building {slug!r}"})
        self.served.out_dir = self.served.root / slug
        return self._json(200, {"ok": True, "slug": slug})

    def _reset_alignment(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            level = json.loads(self.rfile.read(min(length, 1000)) or b"{}")["level"]
            if not isinstance(level, int):
                raise TypeError
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._json(400, {"error": "expected {\"level\": <int>}"})
        return self._run_align(["--reset", str(level)])

    def _apply_alignment(self):
        return self._run_align(["--apply"])

    def _run_align(self, flags: list[str]):
        return self._run_cli("align", flags)

    def _run_cli(self, command: str, flags: list[str]):
        info = json.loads((self.out_dir / "build_info.json").read_text(encoding="utf-8"))
        with _apply_lock:
            try:
                proc = subprocess.run([sys.executable, str(MAIN), command, *flags, info["config_path"]],
                                      capture_output=True, text=True, timeout=APPLY_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                return self._json(500, {"ok": False, "error": "updating the config timed out"})
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self._json(500, {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-500:]})
        return self._json(200 if report.get("ok") else 500, report)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        geojson = self.out_dir / "indoor.geojson"
        last = geojson.stat().st_mtime if geojson.exists() else 0
        last_ping = time.time()
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                time.sleep(POLL_SECONDS)
                mtime = geojson.stat().st_mtime if geojson.exists() else 0
                if mtime != last:
                    last = mtime
                    time.sleep(0.2)  # let the writer finish changes.json too
                    changes_path = self.out_dir / "changes.json"
                    changes = json.loads(changes_path.read_text(encoding="utf-8")) if changes_path.exists() else None
                    payload = json.dumps({"changes": changes}, ensure_ascii=False)
                    self.wfile.write(f"event: update\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif time.time() - last_ping > KEEPALIVE_SECONDS:
                    last_ping = time.time()
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(out_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    out_dir = out_dir.resolve()
    served = type("Served", (Served,), {"root": out_dir.parent, "out_dir": out_dir})
    allowed = None if host in WILDCARD_HOSTS else LOOPBACK_HOSTS | {host}
    handler = type("Handler", (PreviewHandler,), {"served": served, "allowed_hosts": allowed})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(out_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = make_server(out_dir, host, port)
    if host in WILDCARD_HOSTS:
        print("warning: listening on every interface; anyone on the network can edit this building's plans", flush=True)
    print(f"Preview: http://{host}:{port}/  (serving {Path(out_dir).resolve()})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
