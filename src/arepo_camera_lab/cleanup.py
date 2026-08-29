"""No-clobber rsync session archival and verified local-cache cleanup."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
from typing import Any

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
    if not host or not path.startswith("/"):
        raise ValueError(f"remote rsync location must be host:/absolute/path: {source}")
    return host, path


def remote_sha256(source: str) -> str:
    host, path = split_remote(source)
    completed = subprocess.run(
        ["ssh", host, f"sha256sum -- {shlex.quote(path)}"],
        check=False, text=True, capture_output=True)
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
        ["ssh", host, create], check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise FileExistsError(
            f"refusing to overwrite remote session destination {destination}: "
            f"{completed.stderr.strip()}")
    transfer = subprocess.run([
        "rsync", "-a", "--checksum", "--info=progress2",
        f"{local_directory}/", f"{destination}/"], check=False)
    if transfer.returncode != 0:
        raise OSError(
            f"session rsync failed with exit code {transfer.returncode}; "
            f"local and partial remote data are preserved")
    audit = subprocess.run([
        "rsync", "-a", "--checksum", "--dry-run", "--itemize-changes",
        "--omit-dir-times", f"{local_directory}/", f"{destination}/"],
        check=False, text=True, capture_output=True)
    changes = [line for line in audit.stdout.splitlines() if line.strip()]
    if audit.returncode != 0 or changes:
        raise OSError(
            f"session rsync verification failed; local cache is preserved; "
            f"changes={changes[:5]}")


def archive_and_cleanup(outputs_directory: Path, destination: str,
                        cached_inputs: list[CachedInput]) -> Path:
    outputs_directory = outputs_directory.expanduser().resolve()
    outputs_directory.mkdir(parents=True, exist_ok=True)
    verified: list[dict[str, Any]] = []
    for item in cached_inputs:
        local_path = item.local_path.expanduser().resolve()
        expected = validate_sha256(item.sha256)
        if not local_path.is_file():
            raise ValueError(f"cached input does not exist: {local_path}")
        local_digest = viewer.sha256(local_path)
        if local_digest != expected:
            raise ValueError(f"cached input digest mismatch: {local_path}")
        remote_digest = remote_sha256(item.remote_source)
        if remote_digest != expected:
            raise ValueError(f"cluster source digest mismatch: {item.remote_source}")
        verified.append({
            "local_path": str(local_path),
            "remote_source": item.remote_source,
            "sha256": expected,
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt = outputs_directory / f"cleanup_receipt_{stamp}.json"
    if receipt.exists():
        raise FileExistsError(f"refusing to overwrite cleanup receipt {receipt}")
    payload = {
        "schema": CLEANUP_SCHEMA,
        "created_utc": stamp,
        "destination": destination,
        "verified_cluster_inputs": verified,
        "outputs_before_receipt": _local_manifest(outputs_directory),
        "cache_deleted_only_after_verified_sync": True,
    }
    with receipt.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, allow_nan=False)
        handle.write("\n")
    sync_directory_no_clobber(outputs_directory, destination)
    for item in cached_inputs:
        item.local_path.expanduser().resolve().unlink()
    print(f"Archived session to {destination}", flush=True)
    print(f"Removed {len(cached_inputs)} verified local cache file(s)", flush=True)
    return receipt


def catalog_cached_inputs(catalog_path: Path,
                          cache_directory: Path) -> list[CachedInput]:
    catalog = scene_catalog.load_catalog(catalog_path)
    cache_directory = cache_directory.expanduser().resolve()
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
