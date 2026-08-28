"""Manifest-backed simulation-output catalog for the local camera lab."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .transfer import validate_sha256


CATALOG_SCHEMA = "arepo_camera_lab_catalog_v001"


@dataclass(frozen=True)
class CatalogFrame:
    snapshot: int
    scene_source: str
    scene_sha256: str
    field_sidecar_source: str
    field_sidecar_sha256: str
    label: str

    def public_record(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot,
            "label": self.label,
            "scene_source": self.scene_source,
            "scene_sha256": self.scene_sha256,
            "field_sidecar_source": self.field_sidecar_source,
            "field_sidecar_sha256": self.field_sidecar_sha256,
        }


@dataclass(frozen=True)
class SceneCatalog:
    frames: dict[int, CatalogFrame]
    required_auxiliary_fields: tuple[str, ...]
    source_path: Path | None = None

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema": CATALOG_SCHEMA,
            "required_auxiliary_fields": list(self.required_auxiliary_fields),
            "frames": [self.frames[index].public_record()
                       for index in sorted(self.frames)],
        }


def _frame(value: dict[str, Any]) -> CatalogFrame:
    snapshot = int(value["snapshot"])
    if snapshot < 0:
        raise ValueError("catalog snapshot indices must be nonnegative")
    scene_source = str(value["scene_source"]).strip()
    field_source = str(value["field_sidecar_source"]).strip()
    if not scene_source or not field_source:
        raise ValueError("each catalog frame requires scene and field-sidecar sources")
    return CatalogFrame(
        snapshot=snapshot,
        scene_source=scene_source,
        scene_sha256=validate_sha256(str(value["scene_sha256"]), "scene SHA-256"),
        field_sidecar_source=field_source,
        field_sidecar_sha256=validate_sha256(
            str(value["field_sidecar_sha256"]), "field-sidecar SHA-256"),
        label=str(value.get("label") or f"AREPO snapshot {snapshot}"),
    )


def load_catalog(path: Path) -> SceneCatalog:
    path = path.expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != CATALOG_SCHEMA:
        raise ValueError(f"catalog schema must be {CATALOG_SCHEMA}")
    required = tuple(str(name) for name in payload.get(
        "required_auxiliary_fields", ()))
    if not required or len(set(required)) != len(required):
        raise ValueError(
            "catalog requires a nonempty unique required_auxiliary_fields list")
    records = [_frame(value) for value in payload.get("frames", ())]
    frames = {record.snapshot: record for record in records}
    if not frames:
        raise ValueError("catalog contains no frames")
    if len(frames) != len(records):
        raise ValueError("catalog contains duplicate snapshot indices")
    return SceneCatalog(frames=frames, required_auxiliary_fields=required,
                        source_path=path)
