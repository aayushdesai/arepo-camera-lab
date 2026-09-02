"""Owned local VTK process; JSON lines on stdin/stdout, diagnostics on stderr."""
from __future__ import annotations
import base64
import contextlib
import json
from pathlib import Path
import sys


def main():
    from .mesh_render import NativeMeshRenderer
    output = sys.stdout
    def send(value):
        output.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
        output.flush()
    renderer = None
    with contextlib.redirect_stdout(sys.stderr):
        try:
            config_path = Path(sys.argv[1])
            config = json.loads(config_path.read_text())
            renderer = NativeMeshRenderer(config, config_path.parent,
                lambda message: send({"kind": "progress", "message": message}))
            send({"kind": "ready"})
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    if request.get("action") == "quit":
                        break
                    if request.get("action") == "pick":
                        send({"kind": "result", "pick": renderer.pick(request)})
                    else:
                        png, report = renderer.render(request)
                        send({"kind": "result", "png": base64.b64encode(png).decode(),
                              "report": report, "capabilities": renderer.capabilities})
                except Exception as error:
                    send({"kind": "error", "error": str(error)})
        except Exception as error:
            send({"kind": "error", "error": str(error)})
            return 1
        finally:
            if renderer is not None:
                renderer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
