"""A bounded, owned native renderer process for the existing browser controls."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading


class MeshBridge:
    def __init__(self, config: dict):
        self.lock = threading.Lock()
        self.message = "Starting the native Voronoi renderer"
        self.report = None
        self.error = None
        self.closed = False
        self.temporary = tempfile.TemporaryDirectory(prefix="arepo-native-viewer-")
        folder = Path(self.temporary.name)
        path = folder / "config.json"
        path.write_text(json.dumps(config, allow_nan=False))
        self.log = (folder / "renderer.log").open("w+")
        env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-m", "arepo_camera_lab.mesh_worker", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, bufsize=1, env=env, start_new_session=True)
        self.ready = False

    def _read(self):
        line = self.process.stdout.readline()
        if not line:
            if self.closed:
                raise ValueError("Native renderer stopped")
            self.log.flush()
            self.log.seek(0)
            diagnostics = self.log.read()[-1500:]
            raise ValueError("Native renderer exited: " + diagnostics)
        return json.loads(line)

    def request(self, request: dict):
        if not self.lock.acquire(blocking=False):
            raise ValueError("A mesh frame is already rendering")
        try:
            self.error = None
            if self.closed:
                raise ValueError("Native renderer stopped")
            if not self.ready:
                while True:
                    response = self._read()
                    if response["kind"] == "ready":
                        self.ready = True
                        break
                    if response["kind"] == "error":
                        raise ValueError(response["error"])
                    self.message = response.get("message", self.message)
            self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
            self.process.stdin.flush()
            while True:
                response = self._read()
                if response["kind"] == "progress":
                    self.message = response["message"]
                    continue
                if response["kind"] == "error":
                    raise ValueError(response["error"])
                if "report" in response:
                    self.report = response["report"]
                    self.message = "Native mesh ready"
                return response
        except Exception as error:
            self.error = str(error)
            raise
        finally:
            self.lock.release()

    def status(self):
        return {"message": self.message, "error": self.error,
                "busy": self.lock.locked(), "report": self.report}

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process.poll() is None:
            # Only this process group, created by this instance, is terminated.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()
        self.temporary.cleanup()
