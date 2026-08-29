"""Loopback-only server for loading portable scenes without rebuilding by hand."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
from typing import Any

from . import cleanup as session_cleanup
from . import review, viewer
from .catalog import SceneCatalog
from .cleanup import CachedInput
from .transfer import acquire_verified_file


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
header { height: 104px; display: grid; grid-template-columns: 180px minmax(260px,1fr) 150px 92px 122px; grid-template-rows: 38px 38px; gap: 8px; align-items: center; padding: 10px 12px; border-bottom: 1px solid #303842; background: #12171d; }
input, select, button { height: 38px; min-width: 0; border: 1px solid #3d4853; border-radius: 4px; background: #181f26; color: #eef3f6; padding: 0 10px; }
button { cursor: pointer; font-weight: 650; }
button:hover { background: #242d35; }
#fields { grid-column: 1 / -1; }
#statusBand { position: fixed; top: 104px; left: 0; right: 0; z-index: 2; height: 28px; display: grid; grid-template-columns: 1fr 220px; gap: 12px; align-items: center; padding: 4px 12px; background: rgba(8,11,14,.94); color: #a9bac5; font-size: 11px; }
#progress { width: 100%; height: 9px; accent-color: #62a8cf; }
iframe { position: fixed; top: 132px; left: 0; width: 100vw; height: calc(100vh - 132px); border: 0; background: #07090c; }
#visibleData { position: fixed; right: 14px; bottom: 12px; z-index: 4; padding: 7px 10px; border: 1px solid #46535e; border-radius: 4px; background: rgba(8,11,14,.92); color: #e8f1f5; font-size: 12px; font-weight: 650; }
#visibleData.offline { border-color: #8a4c4c; color: #ffc7c7; }
@media (max-width: 850px) { header { height: 152px; grid-template-columns: 1fr 1fr; grid-template-rows: 38px 38px 38px; } #scene, #fields { grid-column: 1 / -1; } #statusBand { top: 152px; } iframe { top: 180px; height: calc(100vh - 180px); } }
</style>
</head>
<body>
<header>
  <select id="snapshot" aria-label="Available AREPO snapshots" title="Only hash-bound simulation outputs with complete physical-field sidecars are listed"><option value="">No catalog loaded</option></select>
  <input id="scene" aria-label="Loaded portable v052 scene path" readonly placeholder="verified cached scene path appears here">
  <input id="points" aria-label="Cell point budget; zero loads all cells" type="number" min="0" step="10000" value="400000" placeholder="points; 0 = all cells">
  <button id="load">Load selected</button>
  <button id="archive" title="Checksum-upload pose outputs, verify cluster sources, remove local cache, and stop the server" disabled>Archive &amp; close</button>
  <input id="fields" aria-label="Loaded physical-field sidecar path" readonly placeholder="verified physical-field sidecar path appears here">
</header>
<div id="statusBand"><span id="status">Loading the verified snapshot catalog...</span><progress id="progress" max="1" value="0"></progress></div>
<iframe id="viewer" title="Interactive AREPO camera viewer" src="/viewer"></iframe>
<div id="visibleData" class="offline">VISIBLE DATA: NOT LOADED</div>
<script>
const scene=document.getElementById('scene'),fields=document.getElementById('fields'),snapshot=document.getElementById('snapshot'),points=document.getElementById('points'),status=document.getElementById('status'),progress=document.getElementById('progress'),frame=document.getElementById('viewer'),load=document.getElementById('load'),archive=document.getElementById('archive'),visibleData=document.getElementById('visibleData');
const LAST_STATUS='arepo_camera_lab_last_server_status_v001';
let displayedRevision=-1,selectionDirty=false,pointsDirty=false;
function remember(data){try{sessionStorage.setItem(LAST_STATUS,JSON.stringify(data));}catch(error){}}
function lastStatus(){try{return JSON.parse(sessionStorage.getItem(LAST_STATUS)||'null');}catch(error){return null;}}
function showVisible(data){const index=data.snapshot??'UNKNOWN';visibleData.textContent=`VISIBLE DATA: AREPO SNAPSHOT ${index} | ${Number(data.point_count).toLocaleString()} CELLS`;visibleData.classList.remove('offline');}
async function loadCatalog(){try{const response=await fetch('/api/catalog',{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();snapshot.replaceChildren();for(const entry of data.frames){const option=document.createElement('option');option.value=entry.snapshot;option.textContent=`${entry.snapshot} | ${entry.label}`;option.dataset.scene=entry.scene_source;option.dataset.fields=entry.field_sidecar_source;snapshot.appendChild(option);}snapshot.disabled=data.frames.length===0;load.disabled=data.frames.length===0;if(data.frames.length===0)status.textContent='No verified snapshot catalog is configured.';}catch(error){snapshot.disabled=true;load.disabled=true;}}
async function refresh(){try{const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();load.disabled=Boolean(data.loading)||Boolean(data.cleanup_running)||snapshot.options.length===0;snapshot.disabled=Boolean(data.loading)||Boolean(data.cleanup_running)||snapshot.options.length===0;archive.disabled=!data.cleanup_configured||Boolean(data.loading)||Boolean(data.cleanup_running);progress.value=data.progress??0;if(data.cleanup_running){status.textContent=data.message||'Verifying and archiving the camera-lab session...';return;}if(data.cleanup_error){status.textContent='Archive failed; local data retained: '+data.cleanup_error;return;}if(data.loading){if(data.requested_snapshot!=null)snapshot.value=String(data.requested_snapshot);status.textContent=`${data.message} (${Math.round((data.progress??0)*100)}%) | visible snapshot remains ${data.snapshot??'none'}`;return;}if(data.error){snapshot.disabled=snapshot.options.length===0;status.textContent='Load failed: '+data.error;return;}if(data.loaded){const revisionChanged=data.revision!==displayedRevision;scene.value=data.scene_path;fields.value=data.field_sidecar_path??'';if(revisionChanged||!selectionDirty)snapshot.value=String(data.snapshot??'');if(revisionChanged||!pointsDirty)points.value=data.requested_points===0?0:data.requested_points;status.textContent=`Ready: ${data.point_count.toLocaleString()} of ${data.num_cells.toLocaleString()} cells from AREPO snapshot ${data.snapshot??'unknown'} | ${data.scene_path}`;progress.value=1;showVisible(data);remember(data);if(revisionChanged){displayedRevision=data.revision;selectionDirty=false;pointsDirty=false;frame.src='/viewer?revision='+data.revision;}}}catch(error){const previous=lastStatus();if(previous&&previous.loaded){scene.value=previous.scene_path??'';fields.value=previous.field_sidecar_path??'';}load.disabled=true;snapshot.disabled=true;archive.disabled=true;visibleData.classList.add('offline');visibleData.textContent=previous&&previous.loaded?`SERVER OFFLINE | LAST VISIBLE SNAPSHOT ${previous.snapshot??'UNKNOWN'}`:'LOCAL SERVER OFFLINE | VISIBLE DATA UNKNOWN';status.textContent=`Local camera-lab server unavailable. This control page must be opened from http://127.0.0.1, not as a file. Start 'arepo-camera-lab serve ...', open the printed URL, and refresh. (${error.message})`;}}
snapshot.onchange=()=>{selectionDirty=true;status.textContent=`Selected AREPO snapshot ${snapshot.value}; press Load selected.`;};
points.oninput=()=>{pointsDirty=true;};
load.onclick=async()=>{if(snapshot.value==='')return;const requestedSnapshot=snapshot.value;load.disabled=true;snapshot.disabled=true;selectionDirty=true;pointsDirty=true;status.textContent=`Queueing verified AREPO snapshot ${requestedSnapshot}...`;progress.value=0;try{const response=await fetch('/api/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({snapshot:+requestedSnapshot,max_points:+points.value})}),data=await response.json();if(!response.ok)throw new Error(data.error||'load failed');}catch(error){status.textContent='Load failed: '+error.message;load.disabled=false;snapshot.disabled=false;selectionDirty=true;}};
archive.onclick=async()=>{archive.disabled=true;load.disabled=true;status.textContent='Verifying cluster sources and archiving session outputs...';try{const response=await fetch('/api/shutdown',{method:'POST'}),data=await response.json();if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);}catch(error){status.textContent=`Could not start archive: ${error.message}`;archive.disabled=false;}};
loadCatalog().then(refresh);setInterval(refresh,400);
</script>
</body>
</html>'''


@dataclass
class ViewerState:
    payload: dict[str, Any] | None = None
    html: str | None = None
    scene_path: Path | None = None
    field_sidecar_path: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    progress: dict[str, Any] = field(default_factory=lambda: {
        "loaded": False, "loading": False, "progress": 0.0,
        "message": "Waiting for a scene", "error": None, "revision": 0})
    worker: threading.Thread | None = None
    catalog: SceneCatalog | None = None
    catalog_path: Path | None = None
    cache_directory: Path = field(
        default_factory=lambda: Path.home() / ".cache/arepo-camera-lab")
    session_directory: Path = field(
        default_factory=lambda: Path.home() / ".local/share/arepo-camera-lab/session")
    cached_inputs: dict[Path, CachedInput] = field(default_factory=dict)
    cleanup_configured: bool = False
    cleanup_destination: str | None = None
    cleanup_worker: threading.Thread | None = None
    cleanup_receipt: Path | None = None
    review_bundle: dict[str, Any] | None = None
    requested_pose_id: str | None = None

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
             scene_sha256: str | None = None,
             field_sidecar_path: Path | None = None,
             requested_pose_id: str | None = None) -> dict[str, Any]:
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
            field_sidecar = viewer.read_field_sidecar(field_sidecar_path) \
                if field_sidecar_path is not None else None
            payload = viewer.build_payload(
                path, header, cells, selected, center, axis, None,
                scene_sha256, snapshot, None, field_sidecar)
            payload["scene"]["point_budget"] = max_points
            if self.review_bundle is not None:
                payload["review_workspace"] = review.public_workspace(
                    self.review_bundle, self.catalog, requested_pose_id)
            self._phase(0.90, "Encoding browser buffers and viewer HTML")
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            html = viewer.HTML_TEMPLATE.replace("__PAYLOAD__", encoded)
            with self.lock:
                revision = int(self.progress.get("revision", 0)) + 1
                self.payload = payload
                self.html = html
                self.scene_path = path
                self.field_sidecar_path = field_sidecar_path
                self.requested_pose_id = requested_pose_id
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
                   scene_sha256: str | None = None,
                   field_sidecar_path: Path | None = None,
                   requested_pose_id: str | None = None) -> dict[str, Any]:
        path, scene_sha256 = self._validate_request(path, max_points, scene_sha256)
        if field_sidecar_path is not None:
            field_sidecar_path = field_sidecar_path.expanduser().resolve()
            if not field_sidecar_path.is_file():
                raise ValueError(f"field sidecar does not exist: {field_sidecar_path}")
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
                self.load(path, snapshot, max_points, scene_sha256,
                          field_sidecar_path, requested_pose_id)
            except Exception:
                pass
        self.worker = threading.Thread(target=work, name="arepo-camera-loader",
                                       daemon=True)
        self.worker.start()
        return queued

    def start_catalog_load(self, snapshot: int, max_points: int,
                           requested_pose_id: str | None = None) -> dict[str, Any]:
        if self.catalog is None:
            raise ValueError("no snapshot catalog is configured")
        try:
            frame = self.catalog.frames[int(snapshot)]
        except KeyError as error:
            raise ValueError(f"snapshot {snapshot} is not in the verified catalog") from error
        with self.lock:
            if self.progress.get("loading"):
                raise ValueError("another scene load is already in progress")
            self.progress.update({
                "loading": True, "progress": 0.0,
                "message": f"Queueing verified snapshot {snapshot}", "error": None,
                "requested_points": max_points, "requested_snapshot": snapshot,
            })
            queued = dict(self.progress)

        def work() -> None:
            try:
                self._phase(0.01, f"Rsyncing scene for snapshot {snapshot}")
                scene_path, _ = acquire_verified_file(
                    frame.scene_source, frame.scene_sha256,
                    self.cache_directory / "scenes")
                self._phase(0.04, f"Rsyncing physical fields for snapshot {snapshot}")
                sidecar_path, _ = acquire_verified_file(
                    frame.field_sidecar_source, frame.field_sidecar_sha256,
                    self.cache_directory / "fields")
                sidecar = viewer.read_field_sidecar(sidecar_path)
                actual_fields = set(sidecar["fields"])
                required_fields = set(self.catalog.required_auxiliary_fields)
                missing = sorted(required_fields - actual_fields)
                extra = sorted(actual_fields - required_fields)
                if missing or extra:
                    raise ValueError(
                        f"snapshot {snapshot} sidecar field contract mismatch; "
                        f"missing={missing}, extra={extra}")
                with self.lock:
                    self.cached_inputs[scene_path] = CachedInput(
                        scene_path, frame.scene_source, frame.scene_sha256)
                    self.cached_inputs[sidecar_path] = CachedInput(
                        sidecar_path, frame.field_sidecar_source,
                        frame.field_sidecar_sha256)
                self.load(scene_path, snapshot, max_points, frame.scene_sha256,
                          sidecar_path, requested_pose_id)
            except Exception as error:
                with self.lock:
                    self.progress.update({
                        "loading": False, "error": str(error),
                        "message": f"Snapshot {snapshot} load failed",
                    })

        self.worker = threading.Thread(
            target=work, name=f"arepo-camera-catalog-loader-{snapshot}",
            daemon=True)
        self.worker.start()
        return queued

    def status(self) -> dict[str, Any]:
        with self.lock:
            result = dict(self.progress)
            result["cleanup_configured"] = self.cleanup_configured
            result["cleanup_running"] = bool(
                self.cleanup_worker is not None and self.cleanup_worker.is_alive())
            result["cleanup_receipt"] = str(self.cleanup_receipt) \
                if self.cleanup_receipt is not None else None
            result["session_directory"] = str(self.session_directory)
            if self.payload is not None:
                scene = self.payload["scene"]
                result.update({
                    "loaded": True, "scene_path": str(self.scene_path),
                    "snapshot": scene["snapshot"],
                    "point_count": self.payload["point_count"],
                    "num_cells": scene["num_cells"],
                    "scene_sha256": scene["sha256"],
                    "field_sidecar_path": str(self.field_sidecar_path) \
                        if self.field_sidecar_path is not None else None,
                })
            return result

    def start_cleanup(self, http_server: Any) -> dict[str, Any]:
        if not self.cleanup_configured or self.cleanup_destination is None:
            raise ValueError(
                "restart the server with --cleanup-on-close and a unique "
                "--sync-back-destination")
        with self.lock:
            if self.progress.get("loading"):
                raise ValueError("wait for the current scene load before archiving")
            if self.cleanup_worker is not None and self.cleanup_worker.is_alive():
                raise ValueError("archive and cleanup is already running")
            self.progress.update({
                "cleanup_running": True, "cleanup_error": None,
                "message": "Verifying and archiving the camera-lab session",
            })
            cached_inputs = (session_cleanup.catalog_cached_inputs(
                self.catalog_path, self.cache_directory)
                if self.catalog_path is not None else
                list(self.cached_inputs.values()))

        def work() -> None:
            try:
                receipt = session_cleanup.archive_and_cleanup(
                    self.session_directory, self.cleanup_destination,
                    cached_inputs)
            except Exception as error:
                with self.lock:
                    self.progress.update({
                        "cleanup_running": False,
                        "cleanup_error": str(error),
                        "message": "Archive failed; server and local cache remain available",
                    })
                return
            with self.lock:
                self.cleanup_receipt = receipt
                self.progress.update({
                    "cleanup_running": False, "cleanup_error": None,
                    "message": f"Archive verified: {receipt.name}; stopping server",
                })
            http_server.shutdown()

        self.cleanup_worker = threading.Thread(
            target=work, name="arepo-camera-archive-cleanup", daemon=False)
        self.cleanup_worker.start()
        return {"status": "archive_started", "cached_inputs": len(cached_inputs)}

    def catalog_payload(self) -> dict[str, Any]:
        if self.catalog is None:
            return {"schema": None, "required_auxiliary_fields": [], "frames": []}
        return self.catalog.public_payload()

    def save_pose(self, pose: dict[str, Any]) -> Path:
        with self.lock:
            if self.payload is None:
                raise ValueError("no scene is loaded")
            scene = dict(self.payload["scene"])
        snapshot = int(pose["snapshot"])
        if snapshot != scene["snapshot"]:
            raise ValueError(
                f"pose snapshot {snapshot} does not match visible snapshot "
                f"{scene['snapshot']}")
        if str(pose.get("scene_sha256")) != str(scene["sha256"]):
            raise ValueError("pose scene SHA-256 does not match the visible scene")
        for name, length in (("position_cm", 3), ("look_at_cm", 3),
                             ("view_direction", 3), ("up", 3)):
            values = pose.get(name)
            if not isinstance(values, list) or len(values) != length or not all(
                    math.isfinite(float(value)) for value in values):
                raise ValueError(f"pose {name} must contain {length} finite values")
        if not math.isfinite(float(pose.get("screen_half_extent_cm", 0.0))) or \
                float(pose["screen_half_extent_cm"]) <= 0.0:
            raise ValueError("pose screen_half_extent_cm must be positive and finite")
        directory = self.session_directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, 10000):
            path = directory / f"camera_pose_snapshot_{snapshot:04d}_{index:03d}.json"
            if path.exists():
                continue
            payload = {
                "schema": "stellar_camera_keyframes_v001",
                "scene": scene,
                "keyframes": [pose],
            }
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, allow_nan=False)
                handle.write("\n")
            return path
        raise FileExistsError("camera-pose numbering is exhausted")

    def save_review_bundle(self, payload: dict[str, Any]) -> Path:
        if self.review_bundle is None:
            raise ValueError("restart the server with --pose-bundle before saving reviews")
        normalized = review.normalize_bundle(payload)
        expected = self.review_bundle["geometry_fingerprint_sha256"]
        if normalized["geometry_fingerprint_sha256"] != expected:
            raise ValueError("review output changed immutable camera geometry")
        directory = self.session_directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(1, 10000):
            path = directory / f"stellar_camera_review_bundle_{index:04d}.json"
            if path.exists():
                continue
            with path.open("x", encoding="utf-8") as handle:
                json.dump(normalized, handle, indent=2, allow_nan=False)
                handle.write("\n")
            return path
        raise FileExistsError("camera-review numbering is exhausted")

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
            elif route == "/api/catalog":
                self._json(HTTPStatus.OK, state.catalog_payload())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in ("/api/load", "/api/pose", "/api/review-bundle",
                                 "/api/shutdown"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                if self.path == "/api/shutdown":
                    result = state.start_cleanup(self.server)
                    self._json(HTTPStatus.ACCEPTED, result)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 10_000_000:
                    raise ValueError("invalid request size")
                request = json.loads(self.rfile.read(length))
                if self.path == "/api/pose":
                    path = state.save_pose(request)
                    self._json(HTTPStatus.CREATED, {"path": str(path)})
                    return
                if self.path == "/api/review-bundle":
                    path = state.save_review_bundle(request)
                    self._json(HTTPStatus.CREATED, {"path": str(path)})
                    return
                snapshot_value = request.get("snapshot")
                snapshot = None if snapshot_value is None else int(snapshot_value)
                if state.catalog is not None:
                    if snapshot is None:
                        raise ValueError("select a catalog snapshot before loading")
                    result = state.start_catalog_load(
                        snapshot, int(request.get("max_points", 400_000)),
                        request.get("pose_id"))
                else:
                    path = Path(str(request["path"]))
                    result = state.start_load(
                        path, snapshot, int(request.get("max_points", 400_000)),
                        request.get("scene_sha256"),
                        Path(str(request["field_sidecar"]))
                        if request.get("field_sidecar") else None,
                        request.get("pose_id"))
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
