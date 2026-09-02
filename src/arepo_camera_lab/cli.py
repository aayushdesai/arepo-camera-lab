"""Command-line interface for the local camera lab."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import webbrowser

from . import catalog as scene_catalog
from . import cleanup, demo, fields, gallery, movie, orbit, render_intent, review, routes, server, spline, viewer, vtk_backend


def _serve(args: argparse.Namespace) -> int:
    state = server.ViewerState()
    state.session_directory = args.session_directory.expanduser().resolve()
    state.cleanup_destination = (args.sync_back_destination or
                                 cleanup.saved_destination(state.session_directory))
    state.cleanup_configured = state.cleanup_destination is not None
    if state.cleanup_destination is not None:
        cleanup.split_remote(state.cleanup_destination)
    if args.cleanup_on_close and not state.cleanup_configured:
        raise ValueError("--cleanup-on-close requires --sync-back-destination or archive_settings.json")
    if args.catalog is not None:
        state.catalog_path = args.catalog.expanduser().resolve()
        state.catalog = scene_catalog.load_catalog(args.catalog)
        state.cache_directory = args.cache_directory.expanduser().resolve()
        if args.pose_bundle is not None:
            state.review_bundle = review.load_bundle(
                args.pose_bundle, args.pose_bundle_sha256)
            review.validate_catalog_bindings(state.review_bundle, state.catalog)
        selected = args.snapshot if args.snapshot is not None else min(state.catalog.frames)
        requested_pose_id = args.pose_id
        if state.review_bundle is not None and requested_pose_id is None:
            requested_pose_id = next((
                pose["pose_id"]
                for pose in state.review_bundle["geometry"]["alternatives"]
                if int(pose["snapshot"]) == int(selected)), None)
        if requested_pose_id is not None and state.review_bundle is not None:
            matching = [pose for pose in state.review_bundle["geometry"]["alternatives"]
                        if pose["pose_id"] == requested_pose_id]
            if not matching:
                raise ValueError(f"pose ID is absent from bundle: {requested_pose_id}")
            selected = int(matching[0]["snapshot"])
        state.start_catalog_load(
            selected, args.max_points, requested_pose_id=requested_pose_id)
    elif args.scene is not None:
        if args.pose_bundle is not None:
            raise ValueError("serve --pose-bundle requires --catalog for cross-snapshot loading")
        state.start_load(args.scene, args.snapshot, args.max_points,
                         args.scene_sha256, args.field_sidecar)
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    server.run_server(state, args.port)
    return 0


def _demo(args: argparse.Namespace) -> int:
    path = args.output.expanduser().resolve()
    demo.write_demo_scene(path, args.cells)
    state = server.ViewerState()
    state.start_load(path, 0, min(args.cells, args.max_points))
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
    if args.field_sidecar is not None:
        command += ["--field-sidecar", str(args.field_sidecar)]
    return viewer.main(command)


def _spline(args: argparse.Namespace) -> int:
    command = ["--keyframes", *[str(path) for path in args.poses],
               "--template", str(args.template), "--output", str(args.output),
               "--diagnostics", str(args.diagnostics), "--tension", str(args.tension),
               "--orientation-mode", args.orientation_mode]
    return spline.main(command)


def _routes(args: argparse.Namespace) -> int:
    command = ["--poses", str(args.poses),
               "--output-directory", str(args.output_directory),
               "--conflict", *[str(snapshot) for snapshot in args.conflict]]
    return routes.main(command)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="arepo-camera-lab",
        description="Explore portable AREPO cell scenes and author smooth cameras locally.")
    commands = result.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the local live-loading viewer")
    serve_source = serve.add_mutually_exclusive_group(required=True)
    serve_source.add_argument("--scene", type=Path)
    serve_source.add_argument(
        "--catalog", type=Path,
        help="Verified snapshot catalog; only complete scene/field pairs become selectable")
    serve.add_argument("--snapshot", type=int)
    serve.add_argument("--max-points", type=int, default=400_000,
                       help="Cell points to display; zero loads every cell")
    serve.add_argument("--scene-sha256",
                       help="Trusted manifest digest; otherwise hash the complete scene")
    serve.add_argument("--field-sidecar", type=Path,
                       help="Optional ID-bound NPZ with B, pressure, entropy, or sound speed")
    serve.add_argument(
        "--pose-bundle", type=Path,
        help="Immutable v001 camera alternatives or v002 reviewed bundle to restore")
    serve.add_argument(
        "--pose-bundle-sha256",
        help="Required SHA-256 for an exact imported pose bundle")
    serve.add_argument(
        "--pose-id", help="Initial exact pose ID from --pose-bundle")
    serve.add_argument(
        "--cache-directory", type=Path,
        default=Path.home() / ".cache/arepo-camera-lab",
        help="Content-addressed rsync cache used by catalog entries")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--session-directory", type=Path,
        default=Path.home() / ".local/share/arepo-camera-lab/session",
        help="No-clobber server-side camera-pose output directory")
    serve.add_argument(
        "--cleanup-on-close", action="store_true",
        help="Enable Archive & close; Quit and Ctrl-C always stop locally without cleanup")
    serve.add_argument(
        "--sync-back-destination",
        help="Archive parent host:/path; each attempt creates a new no-clobber child")
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(function=_serve)

    build = commands.add_parser("build", help="Write a dependency-free HTML viewer")
    build.add_argument("--scene", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--snapshot", type=int)
    build.add_argument("--max-points", type=int, default=400_000,
                       help="Cell points to display; zero is reserved for live-server all-cells mode")
    build.add_argument("--camera-path", type=Path)
    build.add_argument("--field-sidecar", type=Path)
    build.set_defaults(function=_build)

    spline_parser = commands.add_parser("spline", help="Compile saved camera poses")
    spline_parser.add_argument("--poses", "--keyframes", dest="poses", type=Path,
                               required=True, nargs="+",
                               help="One or more downloaded camera-pose JSON files")
    spline_parser.add_argument("--template", type=Path, required=True)
    spline_parser.add_argument("--output", type=Path, required=True)
    spline_parser.add_argument("--diagnostics", type=Path, required=True)
    spline_parser.add_argument("--tension", type=float, default=0.25)
    spline_parser.add_argument(
        "--orientation-mode", choices=spline.ORIENTATION_MODES,
        default="slerp-smootherstep")
    spline_parser.set_defaults(function=_spline)

    routes_parser = commands.add_parser(
        "routes", help="Select diagnostic and continuous routes from saved poses")
    routes_parser.add_argument("--poses", type=Path, required=True)
    routes_parser.add_argument("--output-directory", type=Path, required=True)
    routes_parser.add_argument("--conflict", type=int, nargs=2,
                               default=(820, 821),
                               metavar=("LEFT_SNAPSHOT", "RIGHT_SNAPSHOT"))
    routes_parser.set_defaults(function=_routes)

    orbit_parser = commands.add_parser(
        "orbit",
        help="Select exact alternatives and compile one monotonic physical-axis orbit")
    orbit.add_arguments(orbit_parser)
    orbit_parser.set_defaults(function=orbit.run)

    gallery_parser = commands.add_parser(
        "capture-gallery",
        help="Render every saved camera alternative across all physical channels")
    gallery.add_arguments(gallery_parser)
    gallery_parser.set_defaults(function=gallery.run)

    movie_parser = commands.add_parser(
        "capture-spline-movie",
        help="Render a reviewed WebGL point-cloud preview along a v055 spline")
    movie.add_arguments(movie_parser)
    movie_parser.set_defaults(function=movie.run)

    intent_parser = commands.add_parser(
        "compile-render-intent",
        help="Compile immutable reviewed poses and styles for WebGL/native rendering")
    render_intent.add_arguments(intent_parser)
    intent_parser.set_defaults(function=render_intent.run)

    fields_parser = commands.add_parser(
        "fields", help="Build an explicit physical-field sidecar from HDF5")
    fields.add_arguments(fields_parser)
    fields_parser.set_defaults(function=fields.run)

    vtk_parser = commands.add_parser(
        "vtk", help="Open the native VTK full-cell physics explorer")
    vtk_backend.add_arguments(vtk_parser)
    vtk_parser.set_defaults(function=vtk_backend.run)

    from . import mesh_capture
    mesh_parser = commands.add_parser(
        "capture-mesh", help="Render native Voronoi faces with time, scale, and field annotations")
    mesh_capture.add_arguments(mesh_parser)
    mesh_parser.set_defaults(function=mesh_capture.run)

    cleanup_parser = commands.add_parser(
        "cleanup", help="Rsync session outputs to a no-clobber cluster path and remove verified cache files")
    cleanup.add_arguments(cleanup_parser)
    cleanup_parser.set_defaults(function=cleanup.run)

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
    if args.command == "build" and args.max_points == 0:
        parser().error("build requires a finite --max-points; use serve with zero for all cells")
    if hasattr(args, "max_points") and args.max_points != 0 and \
            args.max_points < server.MIN_POINTS:
        parser().error(
            f"--max-points must be zero (all cells) or at least {server.MIN_POINTS}")
    try:
        return int(args.function(args))
    except (FileExistsError, OSError, ValueError) as error:
        print(f"arepo-camera-lab: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
