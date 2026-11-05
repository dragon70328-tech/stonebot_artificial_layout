"""Project-level reusable artificial-stone texture pattern library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from src.slab_texture import load_texture_patterns


LIBRARY_FILE = "library.json"
IMAGE_DIR = "images"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return name or "pattern"


def _read_library(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": "1.0.0",
            "library_id": path.parent.name or "slab-patterns",
            "version": "1.0.0",
            "patterns": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("patterns"), list):
        raise ValueError(f"invalid pattern library: {path}")
    return payload


def register_pattern_asset(
    library_directory: str | Path,
    *,
    pattern_id: str,
    image_path: str | Path,
    materials: list[str],
    usable_width_mm: float,
    usable_height_mm: float,
    max_pixels_per_mm: float | None = None,
    overwrite: bool = False,
) -> Path:
    """Copy one standard image into the library and register its materials."""
    pattern_id = str(pattern_id or "").strip()
    if not pattern_id:
        raise ValueError("pattern_id must not be empty")
    materials = [str(value).strip() for value in materials if str(value).strip()]
    if not materials:
        raise ValueError("materials must not be empty")
    source = Path(image_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"pattern image not found: {source}")
    if usable_width_mm <= 0 or usable_height_mm <= 0:
        raise ValueError("usable dimensions must be positive")
    if max_pixels_per_mm is not None and max_pixels_per_mm <= 0:
        raise ValueError("max_pixels_per_mm must be positive")

    library_dir = Path(library_directory).expanduser().resolve()
    image_dir = library_dir / IMAGE_DIR
    image_dir.mkdir(parents=True, exist_ok=True)
    destination = image_dir / f"{_safe_name(pattern_id)}{source.suffix.lower()}"
    if destination.exists() and _sha256(destination) != _sha256(source):
        if not overwrite:
            raise FileExistsError(
                f"pattern image already exists with different content: {destination}"
            )
    if not destination.exists() or overwrite:
        shutil.copy2(source, destination)

    library_path = library_dir / LIBRARY_FILE
    payload = _read_library(library_path)
    entry = {
        "pattern_id": pattern_id,
        "materials": materials,
        "image_path": destination.relative_to(library_dir).as_posix(),
        "usable_width_mm": float(usable_width_mm),
        "usable_height_mm": float(usable_height_mm),
        "max_pixels_per_mm": (
            float(max_pixels_per_mm)
            if max_pixels_per_mm is not None
            else None
        ),
    }
    existing_index = next(
        (
            index
            for index, item in enumerate(payload["patterns"])
            if item.get("pattern_id") == pattern_id
        ),
        None,
    )
    if existing_index is None:
        payload["patterns"].append(entry)
    elif overwrite:
        payload["patterns"][existing_index] = entry
    else:
        raise FileExistsError(f"pattern_id already registered: {pattern_id}")

    temporary = library_path.with_name(f".{LIBRARY_FILE}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        load_texture_patterns(temporary)
        os.replace(temporary, library_path)
    finally:
        temporary.unlink(missing_ok=True)
    return library_path


def list_pattern_assets(library_directory: str | Path) -> list[dict[str, Any]]:
    library_path = Path(library_directory).expanduser().resolve() / LIBRARY_FILE
    return list(_read_library(library_path)["patterns"])


def validate_pattern_library(library_directory: str | Path) -> int:
    library_path = Path(library_directory).expanduser().resolve() / LIBRARY_FILE
    return len(load_texture_patterns(library_path))
