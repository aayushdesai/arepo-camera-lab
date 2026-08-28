"""Command-line interface for the local camera lab."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import webbrowser

from . import demo, server, spline, viewer


def _serve(args: argparse.Namespace) -> int:
    state = server.ViewerState()
    if args.scene is not None:
        state.load(args.scene, args.snapshot, args.max_points)
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    server.run_server(state, args.port)
    return 0


def _demo(args: argparse.Namespace) -> int:
    path = args.output.expanduser().resolve()
    demo.write_demo_scene(path, args.cells)
    state = server.ViewerState()
    state.load(path, 0, min(args.cells, args.max_points))
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    server.run_server(state, args.port)
    return 0


def _build(args: argparse.Namespace) -> int:
    command = ["--scene", str(args.scene), "--output", str(args.output),
               "--max-points", str(args.max_points)]
    if args.snapshot is not None:
        command += ["--snapshot", str(args.snapshot)]
    if args.camera_path is not None:
        command += ["--camera-path", str(args.camera_path)]
    return viewer.main(command)


def _spline(args: argparse.Namespace) -> int:
    command = ["--keyframes", *[str(path) for path in args.keyframes],
               "--template", str(args.template), "--output", str(args.output),
               "--diagnostics", str(args.diagnostics), "--tension", str(args.tension)]
    return spline.main(command)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="arepo-camera-lab",
        description="Explore portable AREPO cell scenes and author smooth cameras locally.")
    commands = result.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the local live-loading viewer")
    serve.add_argument("--scene", type=Path)
    serve.add_argument("--snapshot", type=int)
    serve.add_argument("--max-points", type=int, default=400_000)
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(function=_serve)

    build = commands.add_parser("build", help="Write a dependency-free HTML viewer")
    build.add_argument("--scene", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--snapshot", type=int)
    build.add_argument("--max-points", type=int, default=400_000)
    build.add_argument("--camera-path", type=Path)
    build.set_defaults(function=_build)

    spline_parser = commands.add_parser("spline", help="Compile saved key poses")
    spline_parser.add_argument("--keyframes", type=Path, required=True, nargs="+")
    spline_parser.add_argument("--template", type=Path, required=True)
    spline_parser.add_argument("--output", type=Path, required=True)
    spline_parser.add_argument("--diagnostics", type=Path, required=True)
    spline_parser.add_argument("--tension", type=float, default=0.25)
    spline_parser.set_defaults(function=_spline)

    demo_parser = commands.add_parser("demo", help="Generate and open a synthetic disk/outflow scene")
    demo_parser.add_argument("--output", type=Path, default=Path("arepo-camera-lab-demo-v052.bin"))
    demo_parser.add_argument("--cells", type=int, default=250_000)
    demo_parser.add_argument("--max-points", type=int, default=250_000)
    demo_parser.add_argument("--port", type=int, default=8765)
    demo_parser.add_argument("--no-browser", action="store_true")
    demo_parser.set_defaults(function=_demo)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if hasattr(args, "max_points") and not server.MIN_POINTS <= args.max_points <= server.MAX_POINTS:
        parser().error(f"--max-points must be in [{server.MIN_POINTS}, {server.MAX_POINTS}]")
    try:
        return int(args.function(args))
    except (FileExistsError, OSError, ValueError) as error:
        print(f"arepo-camera-lab: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
