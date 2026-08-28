"""Verified rsync acquisition helpers for large local camera-lab inputs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from . import viewer


def validate_sha256(value: str, label: str = "SHA-256") -> str:
    digest = value.lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef"
                                for character in digest):
        raise ValueError(f"{label} must be 64 hexadecimal characters")
    return digest


def source_filename(source: str) -> str:
    remote_path = source.split(":", 1)[-1]
    name = Path(remote_path).name
    if not name:
        raise ValueError(f"source does not name a file: {source}")
    return name


def acquire_verified_file(source: str, expected_sha256: str,
                          directory: Path) -> tuple[Path, str]:
    """Rsync one exact file into a content-addressed cache and verify it."""
    expected = validate_sha256(expected_sha256)
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source_name = source_filename(source)
    source_path = Path(source_name)
    cached = directory / f"{source_path.stem}_{expected[:16]}{source_path.suffix}"
    if cached.is_file():
        actual = viewer.sha256(cached)
        if actual != expected:
            raise ValueError(f"cached file digest mismatch: {cached}")
        print(f"[rsync] Reusing verified cache: {cached}", flush=True)
        return cached, expected

    partial = directory / f".{source_path.stem}_{expected[:16]}.rsync-partial"
    print(f"[rsync] Fetching requested file: {source}", flush=True)
    command = [
        "rsync", "-a", "--info=progress2", "--partial", "--append-verify",
        source, str(partial),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise OSError(
            f"rsync failed with exit code {completed.returncode}; resumable partial "
            f"is preserved at {partial}")
    actual = viewer.sha256(partial)
    if actual != expected:
        raise ValueError(
            f"rsynced file digest {actual} does not match expected {expected}; "
            f"untrusted file is preserved at {partial}")
    os.replace(partial, cached)
    print(f"[rsync] Verified cache ready: {cached}", flush=True)
    return cached, expected

