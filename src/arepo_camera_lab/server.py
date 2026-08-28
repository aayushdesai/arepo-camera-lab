"""Loopback-only server for loading portable scenes without rebuilding by hand."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

from . import viewer


MIN_POINTS = 1_000


APP_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AREPO Camera Lab</title>
<style>
:root { color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
* { box-sizing: border-box; }
body { margin: 0; background: #090c10; color: #e9eef2; overflow: hidden; }
header { height: 64px; display: grid; grid-template-columns: minmax(260px,1fr) 106px 162px 92px; gap: 8px; align-items: center; padding: 10px 12px; border-bottom: 1px solid #303842; background: #12171d; }
input, button { height: 38px; min-width: 0; border: 1px solid #3d4853; border-radius: 4px; background: #181f26; color: #eef3f6; padding: 0 10px; }
button { cursor: pointer; font-weight: 650; }
button:hover { background: #242d35; }
#statusBand { position: fixed; top: 64px; left: 0; right: 0; z-index: 2; height: 28px; display: grid; grid-template-columns: 1fr 220px; gap: 12px; align-items: center; padding: 4px 12px; background: rgba(8,11,14,.94); color: #a9bac5; font-size: 11px; }
#progress { width: 100%; height: 9px; accent-color: #62a8cf; }
iframe { position: fixed; top: 92px; left: 0; width: 100vw; height: calc(100vh - 92px); border: 0; background: #07090c; }
@media (max-width: 850px) { header { height: 112px; grid-template-columns: 1fr 1fr; } #statusBand { top: 112px; } iframe { top: 140px; height: calc(100vh - 140px); } #scene { grid-column: 1 / -1; } }
</style>
</head>
<body>
<header>
  <input id="scene" aria-label="Portable v052 scene path" placeholder="/absolute/path/to/scene_v052.bin">
  <input id="snapshot" aria-label="Snapshot number" type="number" min="0" placeholder="snapshot">
  <input id="points" aria-label="Cell point budget; zero loads all cells" type="number" min="0" step="10000" value="400000" placeholder="points; 0 = all cells">
  <button id="load">Load scene</button>
</header>
<div id="statusBand"><span id="status">Enter a portable v052 full-cell scene path. Files stay on this computer.</span><progress id="progress" max="1" value="0"></progress></div>
<iframe id="viewer" title="Interactive AREPO camera viewer" src="/viewer"></iframe>
<script>
const scene=document.getElementById('scene'),snapshot=document.getElementById('snapshot'),points=document.getElementById('points'),status=document.getElementById('status'),progress=document.getElementById('progress'),frame=document.getElementById('viewer'),load=document.getElementById('load');
let displayedRevision=-1;
async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'}),data=await response.json();load.disabled=Boolean(data.loading);progress.value=data.progress??0;if(data.loading){status.textContent=`${data.message} (${Math.round((data.progress??0)*100)}%)`;return;}if(data.error){status.textContent='Load failed: '+data.error;return;}if(data.loaded){scene.value=data.scene_path;snapshot.value=data.snapshot??'';points.value=data.requested_points===0?0:data.point_count;status.textContent=`Ready: ${data.point_count.toLocaleString()} of ${data.num_cells.toLocaleString()} cells from snapshot ${data.snapshot??'unknown'}.`;progress.value=1;if(data.revision!==displayedRevision){displayedRevision=data.revision;frame.src='/viewer?revision='+data.revision;}}}catch(error){status.textContent='Status error: '+error.message;}}
load.onclick=async()=>{load.disabled=true;status.textContent='Queueing scene load...';progress.value=0;try{const response=await fetch('/api/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:scene.value,snapshot:snapshot.value===''?null:+snapshot.value,max_points:+points.value})}),data=await response.json();if(!response.ok)throw new Error(data.error||'load failed');}catch(error){status.textContent='Load failed: '+error.message;load.disabled=false;}};
scene.onkeydown=e=>{if(e.key==='Enter')load.click();};
refresh();setInterval(refresh,400);
</script>
</body>
</html>'''


@dataclass
class ViewerState:
    payload: dict[str, Any] | None = None
    html: str | None = None
    scene_path: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    progress: dict[str, Any] = field(default_factory=lambda: {
        "loaded": False, "loading": False, "progress": 0.0,
        "message": "Waiting for a scene", "error": None, "revision": 0})
    worker: threading.Thread | None = None

    @staticmethod
    def _validate_request(path: Path, max_points: int,
                          scene_sha256: str | None) -> tuple[Path, str | None]:
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"scene does not exist: {path}")
        if max_points != 0 and max_points < MIN_POINTS:
            raise ValueError(f"point budget must be zero (all cells) or at least {MIN_POINTS}")
        if scene_sha256 is not None:
            scene_sha256 = scene_sha256.lower()
            if len(scene_sha256) != 64 or any(
                    character not in "0123456789abcdef" for character in scene_sha256):
                raise ValueError("scene SHA-256 must be 64 hexadecimal characters")
        return path, scene_sha256

    def _phase(self, progress: float, message: str, **fields: Any) -> None:
        with self.lock:
            self.progress.update({"loading": True, "progress": progress,
                                  "message": message, "error": None, **fields})

    def load(self, path: Path, snapshot: int | None, max_points: int,
             scene_sha256: str | None = None) -> dict[str, Any]:
        path, scene_sha256 = self._validate_request(path, max_points, scene_sha256)
        try:
            self._phase(0.03, "Reading scene header", requested_points=max_points,
                        requested_snapshot=snapshot)
            header = viewer.read_header(path)
            effective_points = int(header["num_cells"]) if max_points == 0 else max_points
            self._phase(0.10, "Mapping full cell records", num_cells=int(header["num_cells"]))
            cells = viewer.read_cells(path, header)
            self._phase(0.20, "Inferring physical center and rotation axis")
            center, axis = viewer.infer_center_axis(cells, header, None, None)
            self._phase(0.38, "Selecting deterministic cell points")
            selected = viewer.sample_cells(cells, effective_points)
            if scene_sha256 is None:
                self._phase(0.52, "Hashing the complete scene for provenance")
                scene_sha256 = viewer.sha256(path)
            else:
                self._phase(0.58, "Using the supplied verified scene hash")
            self._phase(0.64, "Deriving physical display channels")
            payload = viewer.build_payload(
                path, header, cells, selected, center, axis, None,
                scene_sha256, snapshot, None)
            self._phase(0.90, "Encoding browser buffers and viewer HTML")
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            html = viewer.HTML_TEMPLATE.replace("__PAYLOAD__", encoded)
            with self.lock:
                revision = int(self.progress.get("revision", 0)) + 1
                self.payload = payload
                self.html = html
                self.scene_path = path
                self.progress = {
                    "loaded": True, "loading": False, "progress": 1.0,
                    "message": "Scene ready", "error": None,
                    "revision": revision, "requested_points": max_points,
                }
            return self.status()
        except Exception as error:
            with self.lock:
                self.progress.update({"loading": False, "error": str(error),
                                      "message": "Scene load failed"})
            raise

    def start_load(self, path: Path, snapshot: int | None, max_points: int,
                   scene_sha256: str | None = None) -> dict[str, Any]:
        path, scene_sha256 = self._validate_request(path, max_points, scene_sha256)
        with self.lock:
            if self.progress.get("loading"):
                raise ValueError("another scene load is already in progress")
            self.progress.update({"loading": True, "progress": 0.0,
                                  "message": "Scene load queued", "error": None,
                                  "requested_points": max_points,
                                  "requested_snapshot": snapshot})
            queued = dict(self.progress)
        def work() -> None:
            try:
                self.load(path, snapshot, max_points, scene_sha256)
            except Exception:
                pass
        self.worker = threading.Thread(target=work, name="arepo-camera-loader",
                                       daemon=True)
        self.worker.start()
        return queued

    def status(self) -> dict[str, Any]:
        with self.lock:
            result = dict(self.progress)
            if self.payload is not None:
                scene = self.payload["scene"]
                result.update({
                    "loaded": True, "scene_path": str(self.scene_path),
                    "snapshot": scene["snapshot"],
                    "point_count": self.payload["point_count"],
                    "num_cells": scene["num_cells"],
                    "scene_sha256": scene["sha256"],
                })
            return result

    def viewer_html(self) -> str:
        with self.lock:
            html = self.html
        if html is None:
            return "<!doctype html><title>AREPO Camera Lab</title><body style='background:#07090c;color:#9fb0be;font:14px monospace;padding:24px'>Load a scene from the toolbar.</body>"
        return html


def _handler(state: ViewerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArepoCameraLab/0.1"

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route == "/":
                self._send(HTTPStatus.OK, APP_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/viewer":
                self._send(HTTPStatus.OK, state.viewer_html().encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/status":
                self._json(HTTPStatus.OK, state.status())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/load":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length))
                path = Path(str(request["path"]))
                snapshot_value = request.get("snapshot")
                snapshot = None if snapshot_value is None else int(snapshot_value)
                result = state.start_load(
                    path, snapshot, int(request.get("max_points", 400_000)),
                    request.get("scene_sha256"))
                self._json(HTTPStatus.ACCEPTED, result)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            print(f"camera-lab: {format % args}")

    return Handler


def run_server(state: ViewerState, port: int) -> None:
    address = ("127.0.0.1", port)
    server = ThreadingHTTPServer(address, _handler(state))
    print(f"AREPO_CAMERA_LAB_READY http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
