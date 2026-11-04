"""Project artificial-stone slab patterns onto confirmed nesting results.

The current scope is intentionally limited to reusable artificial-stone
patterns: one calibrated image represents the complete usable board area and
is reused for every sheet of the same material/pattern.  Natural-stone
per-sheet imaging, defect avoidance and perspective edge recognition are out of
scope.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import ezdxf
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
from shapely import affinity
from shapely.geometry import Point, Polygon

from src.models import NestingResult, Part


TEXTURE_LAYER = "TEXTURE_IMAGE"
TEXTURE_DIR = "textures"
PREVIEW_DIR = "previews"
ASPECT_RATIO_TOLERANCE = 0.01


@dataclass(frozen=True)
class TexturePattern:
    pattern_id: str
    materials: tuple[str, ...]
    image_path: Path
    usable_width_mm: float
    usable_height_mm: float
    image_size_px: tuple[int, int]
    image_sha256: str
    max_pixels_per_mm: float | None = None


@dataclass(frozen=True)
class TextureRecord:
    sheet_index: int
    number: str
    pattern_id: str
    texture_path: Path
    texture_sha256: str
    source_bounds: tuple[float, float, float, float]
    nested_bounds: tuple[float, float, float, float]
    placement_rot: float
    image_size_px: tuple[int, int]
    clip_points_px: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class TexturePackage:
    output_dir: Path
    textured_dxf: Path
    manifest_path: Path
    texture_count: int
    preview_paths: tuple[Path, ...]
    total_pattern_area_mm2: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_material(value: str | None) -> str:
    return str(value or "").strip().upper()


def _safe_filename(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value)).strip(" .")
    return cleaned[:160] or fallback


def _resolve_relative_path(raw: str, base: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_texture_patterns(manifest_path: str | Path) -> list[TexturePattern]:
    """Load and validate a reusable artificial-stone pattern manifest."""
    path = Path(manifest_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_patterns = data.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise ValueError("texture manifest requires a non-empty patterns list")

    patterns: list[TexturePattern] = []
    pattern_ids: set[str] = set()
    for index, raw in enumerate(raw_patterns):
        if not isinstance(raw, dict):
            raise ValueError(f"texture pattern #{index + 1} must be an object")
        pattern_id = str(raw.get("pattern_id") or "").strip()
        if not pattern_id:
            raise ValueError(f"texture pattern #{index + 1} requires pattern_id")
        if pattern_id in pattern_ids:
            raise ValueError(f"duplicate pattern_id: {pattern_id}")
        materials = tuple(
            _normalized_material(value)
            for value in raw.get("materials", [])
            if _normalized_material(value)
        )
        if not materials:
            raise ValueError(f"{pattern_id}: materials must not be empty")
        image_path = _resolve_relative_path(
            str(raw.get("image_path") or ""),
            path.parent,
        )
        if not image_path.is_file():
            raise FileNotFoundError(f"{pattern_id}: image not found: {image_path}")
        width_mm = float(raw.get("usable_width_mm") or 0.0)
        height_mm = float(raw.get("usable_height_mm") or 0.0)
        max_pixels_per_mm = (
            float(raw["max_pixels_per_mm"])
            if raw.get("max_pixels_per_mm") is not None
            else None
        )
        if width_mm <= 0 or height_mm <= 0:
            raise ValueError(
                f"{pattern_id}: usable_width_mm/usable_height_mm must be positive"
            )
        if max_pixels_per_mm is not None and max_pixels_per_mm <= 0:
            raise ValueError(
                f"{pattern_id}: max_pixels_per_mm must be positive"
            )
        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image)
            image_size = image.size
        patterns.append(
            TexturePattern(
                pattern_id=pattern_id,
                materials=materials,
                image_path=image_path,
                usable_width_mm=width_mm,
                usable_height_mm=height_mm,
                image_size_px=image_size,
                image_sha256=_sha256(image_path),
                max_pixels_per_mm=max_pixels_per_mm,
            )
        )
        pattern_ids.add(pattern_id)

    owners: dict[str, str] = {}
    for pattern in patterns:
        for material in pattern.materials:
            previous = owners.get(material)
            if previous is not None and previous != pattern.pattern_id:
                raise ValueError(
                    f"material {material!r} maps to multiple texture patterns: "
                    f"{previous!r}, {pattern.pattern_id!r}"
                )
            owners[material] = pattern.pattern_id
    return patterns


def _part_material_keys(part: Part) -> set[str]:
    values = {
        _normalized_material(part.material_group),
        _normalized_material(getattr(part, "packing_group", None)),
    }
    values.discard("")
    return values or {"*"}


def pattern_for_part(part: Part, patterns: list[TexturePattern]) -> TexturePattern:
    keys = _part_material_keys(part)
    matches = [
        pattern
        for pattern in patterns
        if keys.intersection({_normalized_material(m) for m in pattern.materials})
    ]
    if len(matches) != 1:
        raise ValueError(
            f"part {part.number}: expected exactly one texture pattern, "
            f"found {[item.pattern_id for item in matches]}"
        )
    return matches[0]


def _validate_pattern_for_sheet(
    pattern: TexturePattern,
    *,
    sheet_width_mm: float,
    sheet_height_mm: float,
) -> None:
    # Nested coordinates already use the usable area.  Never compare with
    # nominal size or nominal + oversize.
    expected = (float(sheet_width_mm), float(sheet_height_mm))
    actual = (pattern.usable_width_mm, pattern.usable_height_mm)
    if not math.isclose(actual[0], expected[0], abs_tol=0.01) or not math.isclose(
        actual[1], expected[1], abs_tol=0.01
    ):
        raise ValueError(
            f"{pattern.pattern_id}: image area {actual[0]:g}x{actual[1]:g} mm "
            f"does not match usable sheet area {expected[0]:g}x{expected[1]:g} mm"
        )
    pixel_ratio = pattern.image_size_px[0] / max(pattern.image_size_px[1], 1)
    board_ratio = expected[0] / expected[1]
    if abs(pixel_ratio - board_ratio) / board_ratio > ASPECT_RATIO_TOLERANCE:
        raise ValueError(
            f"{pattern.pattern_id}: image aspect ratio {pixel_ratio:.6f} "
            f"does not match usable area {board_ratio:.6f}"
        )


def _source_to_board_pixel(
    source_point: tuple[float, float],
    *,
    part: Part,
    image_size_px: tuple[int, int],
    usable_width_mm: float,
    usable_height_mm: float,
) -> tuple[float, float]:
    source_outer = part.source_outer_polygon
    if source_outer is None:
        raise ValueError(f"part {part.number}: source_outer_polygon is required")
    origin = (source_outer.centroid.x, source_outer.centroid.y)
    rotated = affinity.rotate(
        source_outer,
        float(part.placement_rot or 0.0),
        origin=origin,
    )
    dx = part.outer_polygon.bounds[0] - rotated.bounds[0]
    dy = part.outer_polygon.bounds[1] - rotated.bounds[1]
    point = affinity.rotate(
        Point(*source_point),
        float(part.placement_rot or 0.0),
        origin=origin,
    )
    board_x = float(point.x) + dx
    board_y = float(point.y) + dy
    return (
        board_x * image_size_px[0] / usable_width_mm,
        image_size_px[1]
        - board_y * image_size_px[1] / usable_height_mm,
    )
def _affine_coefficients(
    part: Part,
    *,
    source_bounds: tuple[float, float, float, float],
    output_size: tuple[int, int],
    board_image_size: tuple[int, int],
    usable_width_mm: float,
    usable_height_mm: float,
) -> tuple[float, float, float, float, float, float]:
    min_x, min_y, max_x, max_y = source_bounds
    output_w, output_h = output_size

    def board_pixel(source_xy: tuple[float, float]) -> tuple[float, float]:
        return _source_to_board_pixel(
            source_xy,
            part=part,
            image_size_px=board_image_size,
            usable_width_mm=usable_width_mm,
            usable_height_mm=usable_height_mm,
        )

    mapped = [
        board_pixel((min_x, max_y)),
        board_pixel((max_x, max_y)),
        board_pixel((min_x, min_y)),
    ]
    design = np.asarray(
        [[0.0, 0.0, 1.0], [output_w, 0.0, 1.0], [0.0, output_h, 1.0]]
    )
    x_coeff = np.linalg.solve(design, np.asarray([point[0] for point in mapped]))
    y_coeff = np.linalg.solve(design, np.asarray([point[1] for point in mapped]))
    return (*x_coeff.tolist(), *y_coeff.tolist())


def _inverse_point_to_source(point: tuple[float, float], part: Part) -> tuple[float, float]:
    source_outer = part.source_outer_polygon
    if source_outer is None:
        raise ValueError(f"part {part.number}: source_outer_polygon is required")
    origin = (source_outer.centroid.x, source_outer.centroid.y)
    rotated = affinity.rotate(
        source_outer,
        float(part.placement_rot or 0.0),
        origin=origin,
    )
    dx = part.outer_polygon.bounds[0] - rotated.bounds[0]
    dy = part.outer_polygon.bounds[1] - rotated.bounds[1]
    shifted = Point(point[0] - dx, point[1] - dy)
    restored = affinity.rotate(
        shifted,
        -float(part.placement_rot or 0.0),
        origin=origin,
    )
    return (float(restored.x), float(restored.y))


def _draw_source_polygon(
    draw: ImageDraw.ImageDraw,
    polygon: Polygon,
    *,
    part: Part,
    source_bounds: tuple[float, float, float, float],
    output_size: tuple[int, int],
    fill: int,
) -> None:
    min_x, _, _, max_y = source_bounds
    output_w, output_h = output_size
    width = source_bounds[2] - source_bounds[0]
    height = source_bounds[3] - source_bounds[1]
    points = []
    for x, y in polygon.exterior.coords:
        px = (float(x) - min_x) / width * output_w
        py = (max_y - float(y)) / height * output_h
        points.append((px, py))
    if len(points) >= 3:
        draw.polygon(points, fill=fill)


def _texture_mask(
    part: Part,
    *,
    source_bounds: tuple[float, float, float, float],
    output_size: tuple[int, int],
) -> Image.Image:
    scale = 2
    mask = Image.new("L", (output_size[0] * scale, output_size[1] * scale), 0)
    draw = ImageDraw.Draw(mask)
    source_outer = part.source_outer_polygon
    if source_outer is None:
        raise ValueError(f"part {part.number}: source_outer_polygon is required")
    _draw_source_polygon(
        draw,
        source_outer,
        part=part,
        source_bounds=source_bounds,
        output_size=(output_size[0] * scale, output_size[1] * scale),
        fill=255,
    )
    for hole in part.holes:
        restored = Polygon([
            _inverse_point_to_source((float(x), float(y)), part)
            for x, y in hole.exterior.coords
        ])
        _draw_source_polygon(
            draw,
            restored,
            part=part,
            source_bounds=source_bounds,
            output_size=(output_size[0] * scale, output_size[1] * scale),
            fill=0,
        )
    return mask.resize(output_size, Image.Resampling.BILINEAR)


def _clip_points_pixels(
    part: Part,
    *,
    source_bounds: tuple[float, float, float, float],
    output_size: tuple[int, int],
) -> tuple[tuple[float, float], ...]:
    source_outer = part.source_outer_polygon
    if source_outer is None:
        raise ValueError(f"part {part.number}: source_outer_polygon is required")
    min_x, min_y, max_x, max_y = source_bounds
    width = max_x - min_x
    height = max_y - min_y
    points = []
    for x, y in source_outer.exterior.coords:
        points.append(
            (
                (float(x) - min_x) / width * output_size[0],
                (max_y - float(y)) / height * output_size[1],
            )
        )
    if points[0] != points[-1]:
        points.append(points[0])
    return tuple(points)


def extract_part_texture(
    part: Part,
    board_image: Image.Image,
    pattern: TexturePattern,
    output_path: str | Path,
    *,
    sheet_index: int,
) -> tuple[TextureRecord, tuple[float, float, float, float]]:
    """Extract one part in its original orientation and write an RGBA PNG."""
    source_outer = part.source_outer_polygon
    if source_outer is None:
        raise ValueError(f"part {part.number}: source_outer_polygon is required")
    source_bounds = tuple(float(value) for value in source_outer.bounds)
    source_w = source_bounds[2] - source_bounds[0]
    source_h = source_bounds[3] - source_bounds[1]
    if source_w <= 0 or source_h <= 0:
        raise ValueError(f"part {part.number}: invalid source bounds")

    board_w_mm = pattern.usable_width_mm
    board_h_mm = pattern.usable_height_mm
    board_w_px, board_h_px = board_image.size
    native_x = board_w_px / board_w_mm
    native_y = board_h_px / board_h_mm
    if pattern.max_pixels_per_mm is not None:
        native_x = min(native_x, pattern.max_pixels_per_mm)
        native_y = min(native_y, pattern.max_pixels_per_mm)
    output_w = max(1, int(math.ceil(source_w * native_x)))
    output_h = max(1, int(math.ceil(source_h * native_y)))
    coefficients = _affine_coefficients(
        part,
        source_bounds=source_bounds,
        output_size=(output_w, output_h),
        board_image_size=board_image.size,
        usable_width_mm=board_w_mm,
        usable_height_mm=board_h_mm,
    )
    texture = board_image.transform(
        (output_w, output_h),
        Image.Transform.AFFINE,
        coefficients,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    ).convert("RGBA")
    mask = _texture_mask(
        part,
        source_bounds=source_bounds,
        output_size=(output_w, output_h),
    )
    texture.putalpha(ImageChops.multiply(texture.getchannel("A"), mask))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    texture.save(output_path, format="PNG", optimize=True)
    record = TextureRecord(
        sheet_index=int(sheet_index),
        number=part.number,
        pattern_id=pattern.pattern_id,
        texture_path=output_path,
        texture_sha256=_sha256(output_path),
        source_bounds=source_bounds,
        nested_bounds=tuple(float(value) for value in part.outer_polygon.bounds),
        placement_rot=float(part.placement_rot or 0.0),
        image_size_px=(output_w, output_h),
        clip_points_px=_clip_points_pixels(
            part,
            source_bounds=source_bounds,
            output_size=(output_w, output_h),
        ),
    )
    return record, source_bounds


def _load_board_image(pattern: TexturePattern) -> Image.Image:
    with Image.open(pattern.image_path) as source:
        return ImageOps.exif_transpose(source).convert("RGBA")


def _load_numbered_document(path: Path):
    doc = ezdxf.readfile(str(path))
    numbers = []
    number_layers = {
        str(layer.dxf.name).upper()
        for layer in doc.layers
        if any(
            token in str(layer.dxf.name).upper()
            for token in ("NUMBER", "编号", "编")
        )
    }
    number_layers.add("NUMBERS")
    for entity in doc.modelspace():
        layer = str(entity.dxf.get("layer", "")).upper()
        if entity.dxftype() == "TEXT" and layer in number_layers:
            numbers.append(str(entity.dxf.text).strip())
        elif entity.dxftype() == "MTEXT" and layer in number_layers:
            numbers.append(str(entity.text).strip())
    counts = Counter(numbers)
    missing_layer = not numbers
    return doc, counts, missing_layer


def _source_polygons_by_number(source_dxf: Path) -> dict[str, Polygon]:
    """Map original numbered panels to their source geometry."""
    doc = ezdxf.readfile(str(source_dxf))
    msp = doc.modelspace()
    outer_layers = {
        str(layer.dxf.name).upper()
        for layer in doc.layers
        if str(layer.dxf.name).upper() in {"OUTER", "COUNTERTOPS"}
    }
    polygons = []
    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE" or not bool(entity.closed):
            continue
        layer = str(entity.dxf.get("layer", "")).upper()
        if outer_layers and layer not in outer_layers:
            continue
        points = [
            (float(x), float(y))
            for x, y in entity.get_points("xy")
        ]
        polygon = Polygon(points)
        if polygon.is_valid and not polygon.is_empty:
            polygons.append(polygon)

    number_layers = {
        str(layer.dxf.name).upper()
        for layer in doc.layers
        if any(
            token in str(layer.dxf.name).upper()
            for token in ("NUMBER", "编号", "编")
        )
    }
    number_layers.add("NUMBERS")
    result: dict[str, Polygon] = {}
    for entity in msp:
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        if str(entity.dxf.get("layer", "")).upper() not in number_layers:
            continue
        number = (
            str(entity.dxf.text).strip()
            if entity.dxftype() == "TEXT"
            else str(entity.text).strip()
        )
        try:
            point = Point(float(entity.dxf.insert.x), float(entity.dxf.insert.y))
        except (AttributeError, TypeError, ValueError):
            continue
        candidates = [
            polygon for polygon in polygons if polygon.covers(point)
        ]
        if not candidates:
            continue
        result[number] = min(candidates, key=lambda polygon: polygon.area)
    return result


def load_texture_nesting_result(
    nested_dxf_path: str | Path,
    source_numbered_dxf_path: str | Path,
    *,
    thickness: float = 20.0,
) -> NestingResult:
    """Rehydrate an existing nesting result and bind source part geometry."""
    from src.nesting_reviewer import load_nested_dxf

    result = load_nested_dxf(nested_dxf_path, thickness=thickness)
    source_geometry = _source_polygons_by_number(
        Path(source_numbered_dxf_path)
    )
    missing = []
    for sheet in result.sheets:
        for part in sheet.parts:
            source_outer = source_geometry.get(part.number)
            if source_outer is None:
                missing.append(part.number)
                continue
            rotation = None
            for angle in (0.0, 90.0, 180.0, 270.0):
                candidate = affinity.rotate(
                    source_outer,
                    angle,
                    origin=(
                        source_outer.centroid.x,
                        source_outer.centroid.y,
                    ),
                )
                candidate = affinity.translate(
                    candidate,
                    xoff=part.outer_polygon.bounds[0] - candidate.bounds[0],
                    yoff=part.outer_polygon.bounds[1] - candidate.bounds[1],
                )
                if part.outer_polygon.hausdorff_distance(candidate) <= 2.0:
                    rotation = angle
                    break
            if rotation is None:
                raise ValueError(
                    f"{part.number}: nested outline does not match source outline"
                )
            part.source_outer_polygon = source_outer
            part.placement_rot = rotation
            if not part.material_group:
                match = re.search(r"ST[/-](\d+)", part.number.upper())
                if match:
                    part.material_group = f"ST/{match.group(1)}"
    if missing:
        raise ValueError(
            "source DXF is missing panel numbers: "
            + ", ".join(sorted(set(missing))[:10])
        )
    return result


def _write_textured_dxf(
    base_dxf: Path,
    output_dxf: Path,
    records: list[TextureRecord],
) -> None:
    doc, number_counts, missing_layer = _load_numbered_document(base_dxf)
    if missing_layer:
        raise ValueError(
            f"{base_dxf}: NUMBERS layer has no numbered TEXT/MTEXT entities"
        )
    output_dxf.parent.mkdir(parents=True, exist_ok=True)
    doc.layers.add(TEXTURE_LAYER, color=8)
    doc.set_raster_variables(frame=0, quality=1, units="mm")
    msp = doc.modelspace()
    for index, record in enumerate(records, start=1):
        if number_counts.get(record.number, 0) != 1:
            raise ValueError(
                f"{record.number}: expected exactly one NUMBERS label in "
                f"{base_dxf.name}, found {number_counts.get(record.number, 0)}"
            )
        image_def = doc.add_image_def(
            # AutoCAD 2023 does not resolve the forward-slash relative paths
            # emitted by ezdxf for IMAGEDEF.  Absolute paths render reliably.
            str(record.texture_path.resolve()),
            record.image_size_px,
            name=f"TEXTURE_{index:05d}_"
                 f"{_safe_filename(record.pattern_id, 'PATTERN')}_"
                 f"{_safe_filename(record.number, 'PART')}",
        )
        min_x, min_y, max_x, max_y = record.source_bounds
        image = msp.add_image(
            image_def,
            insert=(min_x, min_y),
            size_in_units=(max_x - min_x, max_y - min_y),
            dxfattribs={"layer": TEXTURE_LAYER},
        )
        image.set_boundary_path(record.clip_points_px)
    doc.saveas(output_dxf)
    _verify_textured_dxf(output_dxf, records)


def _verify_textured_dxf(path: Path, records: list[TextureRecord]) -> None:
    doc = ezdxf.readfile(str(path))
    images = list(doc.modelspace().query("IMAGE"))
    if len(images) != len(records):
        raise ValueError(
            f"texture DXF read-back expected {len(records)} images, "
            f"found {len(images)}"
        )
    for image, record in zip(images, records):
        if image.dxf.layer != TEXTURE_LAYER:
            raise ValueError("texture DXF read-back found an image on wrong layer")
        if len(image.boundary_path) < 3:
            raise ValueError("texture DXF read-back found an unclipped image")
        image_def = image.image_def
        if image_def is None:
            raise ValueError("texture DXF read-back found a missing image definition")
        image_path = Path(image_def.dxf.filename)
        if not image_path.is_absolute():
            image_path = path.parent / image_path
        if not image_path.is_file():
            raise ValueError(
                f"texture DXF read-back image is missing: {image_path}"
            )
        if image_def.dxf.image_size.vec2 != record.image_size_px:
            raise ValueError(
                f"{record.number}: texture image size does not match manifest"
            )


def _render_sheet_preview(
    sheet_image: Image.Image,
    parts: list[Part],
    sheet_width_mm: float,
    sheet_height_mm: float,
    output_path: Path,
    *,
    sheet_index: int,
    max_pixels: int = 1800,
) -> Path:
    scale = min(1.0, max_pixels / max(sheet_image.size))
    preview = sheet_image.resize(
        (
            max(1, int(round(sheet_image.width * scale))),
            max(1, int(round(sheet_image.height * scale))),
        ),
        Image.Resampling.LANCZOS,
    ).convert("RGB")
    draw = ImageDraw.Draw(preview)
    px_per_mm_x = preview.width / sheet_width_mm
    px_per_mm_y = preview.height / sheet_height_mm
    font = ImageFont.load_default(size=max(12, int(18 * scale)))
    for part in parts:
        points = [
            (
                float(x) * px_per_mm_x,
                preview.height - float(y) * px_per_mm_y,
            )
            for x, y in part.outer_polygon.exterior.coords
        ]
        draw.line(points, fill=(220, 30, 30, 255), width=max(1, int(2 * scale)))
        for hole in part.holes:
            hole_points = [
                (
                    float(x) * px_per_mm_x,
                    preview.height - float(y) * px_per_mm_y,
                )
                for x, y in hole.exterior.coords
            ]
            draw.line(
                hole_points,
                fill=(30, 90, 220, 255),
                width=max(1, int(scale)),
            )
        centroid = part.outer_polygon.representative_point()
        draw.text(
            (
                float(centroid.x) * px_per_mm_x + 4,
                preview.height - float(centroid.y) * px_per_mm_y - 18,
            ),
            part.number,
            fill=(0, 0, 0),
            font=font,
            stroke_width=1,
            stroke_fill=(255, 255, 255),
        )
    draw.text(
        (8, 8),
        f"Sheet {sheet_index}",
        fill=(0, 0, 0),
        font=font,
        stroke_width=1,
        stroke_fill=(255, 255, 255),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preview.save(output_path, format="PNG", optimize=True)
    return output_path


def generate_textured_package(
    result: NestingResult,
    numbered_dxf_path: str | Path,
    pattern_manifest_path: str | Path,
    output_directory: str | Path,
    *,
    preview_max_pixels: int = 1800,
) -> TexturePackage:
    """Generate reusable artificial-stone textures and an in-place texture DXF."""
    if result.total_parts <= 0 or not result.sheets:
        raise ValueError("texture generation requires a non-empty nesting result")
    output_dir = Path(output_directory).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"texture output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    textures_dir = output_dir / TEXTURE_DIR
    previews_dir = output_dir / PREVIEW_DIR
    textures_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)

    patterns = load_texture_patterns(pattern_manifest_path)
    image_cache: dict[str, Image.Image] = {}
    records: list[TextureRecord] = []
    preview_paths: list[Path] = []
    seen_numbers: set[str] = set()
    pattern_area = 0.0

    for sheet in result.sheets:
        if not sheet.parts:
            continue
        sheet_patterns = {
            pattern_for_part(part, patterns).pattern_id
            for part in sheet.parts
        }
        if len(sheet_patterns) != 1:
            raise ValueError(
                f"sheet {sheet.index}: one physical sheet maps to multiple "
                f"texture patterns {sorted(sheet_patterns)}"
            )
        pattern_id = next(iter(sheet_patterns))
        pattern = next(item for item in patterns if item.pattern_id == pattern_id)
        _validate_pattern_for_sheet(
            pattern,
            sheet_width_mm=sheet.width,
            sheet_height_mm=sheet.height,
        )
        board_image = image_cache.get(pattern_id)
        if board_image is None:
            board_image = _load_board_image(pattern)
            image_cache[pattern_id] = board_image
        for part in sheet.parts:
            if part.number in seen_numbers:
                raise ValueError(f"duplicate part number in nesting result: {part.number}")
            seen_numbers.add(part.number)
            filename = (
                f"{len(records) + 1:05d}__"
                f"{_safe_filename(pattern.pattern_id, 'PATTERN')}__"
                f"{_safe_filename(part.number, 'PART')}.png"
            )
            record, _bounds = extract_part_texture(
                part,
                board_image,
                pattern,
                textures_dir / filename,
                sheet_index=sheet.index,
            )
            records.append(record)
            pattern_area += float(part.area)
        preview_paths.append(
            _render_sheet_preview(
                board_image,
                sheet.parts,
                sheet.width,
                sheet.height,
                previews_dir / f"Sheet_{sheet.index:03d}.png",
                sheet_index=sheet.index,
                max_pixels=preview_max_pixels,
            )
        )

    textured_dxf = output_dir / f"{Path(numbered_dxf_path).stem}_纹理原位.dxf"
    _write_textured_dxf(Path(numbered_dxf_path), textured_dxf, records)
    manifest = {
        "schema_version": "1.0.0",
        "scope": "reusable_artificial_stone",
        "coordinate_basis": {
            "image_maps_to": "usable_area",
            "oversize_included": False,
        },
        "patterns": [
            {
                **asdict(pattern),
                "image_path": str(pattern.image_path),
            }
            for pattern in patterns
        ],
        "textures": [
            {
                **asdict(record),
                "texture_path": str(record.texture_path.relative_to(output_dir)),
            }
            for record in records
        ],
        "previews": [str(path.relative_to(output_dir)) for path in preview_paths],
        "summary": {
            "texture_count": len(records),
            "sheet_count": len({record.sheet_index for record in records}),
            "total_pattern_area_mm2": round(pattern_area, 3),
            "textured_dxf": textured_dxf.name,
        },
    }
    manifest_path = output_dir / "texture_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return TexturePackage(
        output_dir=output_dir,
        textured_dxf=textured_dxf,
        manifest_path=manifest_path,
        texture_count=len(records),
        preview_paths=tuple(preview_paths),
        total_pattern_area_mm2=pattern_area,
    )
