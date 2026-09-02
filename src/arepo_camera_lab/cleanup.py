"""No-clobber rsync session archival and verified local-cache cleanup."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import shlex
import shutil
import uuid
import subprocess
from typing import Any, Callable

from . import catalog as scene_catalog
from . import viewer
from .transfer import validate_sha256, verified_cache_path


CLEANUP_SCHEMA = "arepo_camera_lab_cleanup_receipt_v001"


@dataclass(frozen=True)
class CachedInput:
    local_path: Path
    remote_source: str
    sha256: str


def split_remote(source: str) -> tuple[str, str]:
    if ":" not in source:
        raise ValueError(f"remote rsync location must be host:/absolute/path: {source}")
    host, path = source.split(":", 1)
    if not host or host.startswith("-") or any(c.isspace() for c in host) or not path.startswith("/") or any(c in path for c in "\r\n\0"):
        raise ValueError(f"remote rsync location must be host:/absolute/path: {source}")
    return host, path


def remote_sha256(source: str) -> str:
    host, path = split_remote(source)
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
         f"sha256sum -- {shlex.quote(path)}"],
        check=False, text=True, capture_output=True, timeout=300)
    if completed.returncode != 0:
        raise OSError(
            f"remote SHA-256 failed for {source}: {completed.stderr.strip()}")
    fields = completed.stdout.strip().split()
    if not fields:
        raise OSError(f"remote SHA-256 returned no digest for {source}")
    return validate_sha256(fields[0], "remote SHA-256")


def _local_manifest(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(value for value in directory.rglob("*") if value.is_file()):
        records.append({
            "path": str(path.relative_to(directory)),
            "size": path.stat().st_size,
            "sha256": viewer.sha256(path),
        })
    return records


def sync_directory_no_clobber(local_directory: Path, destination: str) -> None:
    local_directory = local_directory.expanduser().resolve()
    if not local_directory.is_dir():
        raise ValueError(f"session output directory does not exist: {local_directory}")
    host, remote_path = split_remote(destination)
    parent = str(PurePosixPath(remote_path).parent)
    create = (
        f"mkdir -p -- {shlex.quote(parent)} && "
        f"mkdir -- {shlex.quote(remote_path)}")
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, create],
        check=False, text=True, capture_output=True, timeout=30)
    if completed.returncode != 0:
        raise FileExistsError(
            f"refusing to overwrite remote session destination {destination}: "
            f"{completed.stderr.strip()}")
    transfer = subprocess.run([
        "rsync", "-a", "--checksum", "--ignore-existing", "--protect-args",
        "--timeout=120", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
        f"{local_directory}/", f"{destination}/"],
        check=False, text=True, capture_output=True)
    if transfer.returncode != 0:
        raise OSError(
            f"session rsync failed with exit code {transfer.returncode}; "
            f"local and partial remote data are preserved. {transfer.stderr.strip()}")
    audit = subprocess.run([
        "rsync", "-a", "--checksum", "--dry-run", "--itemize-changes",
        "--omit-dir-times", "--protect-args", "--timeout=120",
        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
        f"{local_directory}/", f"{destination}/"],
        check=False, text=True, capture_output=True)
    changes = [line for line in audit.stdout.splitlines() if line.strip()]
    if audit.returncode != 0 or changes:
        raise OSError(
            f"session rsync verification failed; local cache is preserved; "
            f"changes={changes[:5]}; {audit.stderr.strip()}")


def saved_destination(outputs_directory: Path) -> str | None:
    """Read an explicitly saved archive destination for this local session."""
    path = outputs_directory.expanduser() / "archive_settings.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "arepo_camera_lab_archive_settings_v001":
        raise ValueError(f"invalid archive settings: {path}")
    destination = str(payload["destination"])
    split_remote(destination)
    return destination


def _managed_outputs(directory: Path) -> list[Path]:
    schemas = {
        "camera_pose_snapshot_": "stellar_camera_keyframes_v001",
        "stellar_camera_review_bundle_": "stellar_camera_review_bundle_v002",
        "camera_lab_session_": "arepo_camera_lab_browser_session_v001",
        "cleanup_receipt_": CLEANUP_SCHEMA,
        "cleanup_completed_": "arepo_camera_lab_cleanup_receipt_v002",
    }
    selected = []
    for path in sorted(directory.iterdir()):
        expected = next((schema for prefix, schema in schemas.items()
                         if path.name.startswith(prefix) and path.suffix == ".json"), None)
        if expected is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"managed session product must be a regular file: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        allowed = {expected}
        if path.name.startswith(("cleanup_receipt_", "cleanup_completed_")):
            allowed.add("arepo_camera_lab_cleanup_receipt_v002")
        if payload.get("schema") not in allowed:
            raise ValueError(f"unexpected managed session schema: {path}")
        selected.append(path)
    return selected


def _cache_signature(path: Path) -> tuple[int, ...]:
    if path.is_symlink():
        raise ValueError(f"refusing to remove a symlink cache: {path}")
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1:
        raise ValueError(f"cache is not an exclusive regular file: {path}")
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def archive_and_cleanup(outputs_directory: Path, destination: str,
                        cached_inputs: list[CachedInput], *,
                        progress: Callable[[float, str], None] | None = None) -> Path:
    """Archive managed products, then remove only unchanged verified cache copies."""
    outputs_directory = outputs_directory.expanduser().resolve()
    outputs_directory.mkdir(parents=True, exist_ok=True)
    split_remote(destination)
    report = progress or (lambda value, message: None)
    # A retry always gets a new namespace; failed remote attempts stay intact.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    destination = destination.rstrip("/") + "/archive_" + stamp
    unique: dict[Path, CachedInput] = {}
    signatures: dict[Path, tuple[int, ...]] = {}
    verified: list[dict[str, Any]] = []
    for item in cached_inputs:
        supplied = item.local_path.expanduser()
        if supplied.is_symlink():
            raise ValueError(f"refusing to remove a symlink cache: {supplied}")
        path = supplied.parent.resolve() / supplied.name
        expected_path = verified_cache_path(item.remote_source, item.sha256, path.parent)
        if path != expected_path:
            raise ValueError(f"input is not a managed content-addressed cache: {path}")
        if path in unique and unique[path] != item:
            raise ValueError(f"conflicting cache bindings: {path}")
        unique[path] = item
    for index, (path, item) in enumerate(unique.items(), 1):
        report(0.65 * (index - 1) / max(len(unique), 1),
               f"Verifying local and cluster cache copy {index}/{len(unique)}")
        before = _cache_signature(path)
        expected = validate_sha256(item.sha256)
        if viewer.sha256(path) != expected:
            raise ValueError(f"cached input digest mismatch: {path}")
        if remote_sha256(item.remote_source) != expected:
            raise ValueError(f"cluster source digest mismatch: {item.remote_source}")
        if _cache_signature(path) != before:
            raise ValueError(f"cache changed during verification: {path}")
        signatures[path] = before
        verified.append({"local_path": str(path), "remote_source": item.remote_source,
                         "sha256": expected, "bytes": before[2]})

    report(0.65, "Saving an immutable copy of the session products")
    if (outputs_directory / ".archives").is_symlink():
        raise ValueError("archive staging directory must not be a symlink")
    staging = outputs_directory / ".archives" / ("archive_" + stamp)
    staging.mkdir(parents=True, exist_ok=False)
    originals: dict[Path, str] = {}
    for path in _managed_outputs(outputs_directory):
        digest = viewer.sha256(path)
        shutil.copy2(path, staging / path.name)
        if viewer.sha256(staging / path.name) != digest:
            raise ValueError(f"session product changed while staging: {path}")
        originals[path] = digest
    payload = {
        "schema": "arepo_camera_lab_cleanup_receipt_v002",
        "created_utc": stamp, "destination": destination,
        "verified_cluster_inputs": verified,
        "outputs_before_receipt": _local_manifest(staging),
        "cache_deleted_only_after_verified_sync": True,
    }
    receipt_name = f"cleanup_receipt_{stamp}.json"
    with (staging / receipt_name).open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")
    report(0.75, "Uploading session products and checking the remote copy")
    sync_directory_no_clobber(staging, destination)
    if set(_managed_outputs(outputs_directory)) != set(originals):
        raise ValueError("session products changed during archive; cache is preserved")
    for path, digest in originals.items():
        if viewer.sha256(path) != digest:
            raise ValueError(f"session product changed during archive; cache is preserved: {path}")
    for path, before in signatures.items():
        if _cache_signature(path) != before:
            raise ValueError(f"cache changed before cleanup; cache is preserved: {path}")
    report(0.95, "Archive verified; removing matching local cache copies")
    for path in unique:
        path.unlink()
    completion = outputs_directory / f"cleanup_completed_{stamp}.json"
    with completion.open("x", encoding="utf-8") as handle:
        json.dump({**payload, "status": "complete", "removed_cache_files": len(unique),
                   "removed_bytes": sum(row["bytes"] for row in verified)},
                  handle, indent=2, allow_nan=False)
        handle.write("\n")
    report(1.0, "Archive and cache cleanup complete")
    print(f"Archived session to {destination}", flush=True)
    print(f"Removed {len(unique)} verified local cache file(s)", flush=True)
    return completion



def catalog_cached_inputs(catalog_path: Path,
                          cache_directory: Path) -> list[CachedInput]:
    catalog = scene_catalog.load_catalog(catalog_path)
    cache_directory = cache_directory.expanduser().resolve()
    for kind in ("scenes", "fields"):
        if (cache_directory / kind).is_symlink():
            raise ValueError(f"refusing a symlink cache directory: {cache_directory / kind}")
    inputs: list[CachedInput] = []
    for snapshot in sorted(catalog.frames):
        frame = catalog.frames[snapshot]
        scene = verified_cache_path(
            frame.scene_source, frame.scene_sha256,
            cache_directory / "scenes")
        fields = verified_cache_path(
            frame.field_sidecar_source, frame.field_sidecar_sha256,
            cache_directory / "fields")
        if scene.is_file():
            inputs.append(CachedInput(
                scene, frame.scene_source, frame.scene_sha256))
        if fields.is_file():
            inputs.append(CachedInput(
                fields, frame.field_sidecar_source,
                frame.field_sidecar_sha256))
    return inputs


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--outputs-directory", type=Path, required=True)
    parser.add_argument("--sync-back-destination", required=True)
    parser.add_argument("--catalog", type=Path,
                        help="Clean every verified catalog cache currently present")
    parser.add_argument("--cache-directory", type=Path,
                        default=Path.home() / ".cache/arepo-camera-lab")
    parser.add_argument("--cached-scene", type=Path)
    parser.add_argument("--remote-scene-source")
    parser.add_argument("--scene-sha256")
    parser.add_argument("--cached-field-sidecar", type=Path)
    parser.add_argument("--remote-field-sidecar-source")
    parser.add_argument("--field-sidecar-sha256")


def run(args: argparse.Namespace) -> int:
    if args.catalog is not None:
        if any(value is not None for value in (
                args.cached_scene, args.remote_scene_source, args.scene_sha256,
                args.cached_field_sidecar,
                args.remote_field_sidecar_source,
                args.field_sidecar_sha256)):
            raise ValueError(
                "--catalog cannot be combined with individual cached inputs")
        inputs = catalog_cached_inputs(args.catalog, args.cache_directory)
        archive_and_cleanup(
            args.outputs_directory, args.sync_back_destination, inputs)
        return 0
    if not all(value is not None for value in (
            args.cached_scene, args.remote_scene_source, args.scene_sha256)):
        raise ValueError(
            "cleanup requires --catalog or the complete cached-scene triple")
    inputs = [CachedInput(
        args.cached_scene, args.remote_scene_source, args.scene_sha256)]
    field_values = (
        args.cached_field_sidecar, args.remote_field_sidecar_source,
        args.field_sidecar_sha256)
    if any(value is not None for value in field_values):
        if not all(value is not None for value in field_values):
            raise ValueError(
                "field-sidecar cleanup requires local path, remote source, and SHA-256")
        inputs.append(CachedInput(
            args.cached_field_sidecar, args.remote_field_sidecar_source,
            args.field_sidecar_sha256))
    archive_and_cleanup(
        args.outputs_directory, args.sync_back_destination, inputs)
    return 0
