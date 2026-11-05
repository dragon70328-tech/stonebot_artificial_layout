import json
from pathlib import Path

import ezdxf
from PIL import Image
from shapely.geometry import box

import main as app
from src.constraints import PROFILE_QUICK
from src.drawing_profile import DrawingProfile
from src.dxf_writer import write_nested_dxf
from src.models import NestingResult, Part, Sheet
from src.slab_texture import (
    generate_textured_package,
    load_texture_nesting_result,
    relocate_texture_references,
)
from src.units import UnitSystem


def _write_pattern_manifest(tmp_path: Path) -> Path:
    image_path = tmp_path / "pattern.png"
    image = Image.new("RGB", (200, 100), (0, 0, 255))
    for x in range(100):
        for y in range(100):
            image.putpixel((x, y), (255, 0, 0))
    image.save(image_path)
    manifest = tmp_path / "patterns.json"
    manifest.write_text(
        json.dumps(
            {
                "patterns": [
                    {
                        "pattern_id": "checker",
                        "materials": ["MA", "MAT-AB"],
                        "image_path": "pattern.png",
                        "usable_width_mm": 100.0,
                        "usable_height_mm": 50.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _write_numbered_dxf(
    tmp_path: Path,
    numbers: tuple[str, ...] = ("MA-001",),
) -> Path:
    path = tmp_path / "numbered.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("OUTER")
    doc.layers.add("NUMBERS")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(20, 20), (30, 20), (30, 30), (20, 30)],
        close=True,
        dxfattribs={"layer": "OUTER"},
    )
    for index, number in enumerate(numbers):
        msp.add_text(
            number,
            dxfattribs={
                "layer": "NUMBERS",
                "height": 2.0,
                "insert": (25, 25 + index * 3),
            },
        )
    doc.saveas(path)
    return path


def _texture_result() -> NestingResult:
    source_outer = box(20, 20, 30, 30)
    placed_outer = box(70, 30, 80, 40)
    placed_hole = box(73, 33, 75, 35)
    part = Part(
        id=1,
        number="MA-001",
        polygon=placed_outer.difference(placed_hole),
        outer_polygon=placed_outer,
        holes=[placed_hole],
        material_group="MA",
        packing_group="MAT-AB",
        area=100.0,
        source_outer_polygon=source_outer,
        placement_rot=0.0,
    )
    sheet = Sheet(
        index=1,
        width=100.0,
        height=50.0,
        thickness=20.0,
        parts=[part],
    )
    return NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=1,
        total_sheets=1,
        total_part_area=100.0,
        total_sheet_area=5000.0,
    )


def _texture_result_two_sheets() -> NestingResult:
    source_outer = box(20, 20, 30, 30)
    sheets = []
    for index, number in enumerate(("MA-001", "MA-002"), start=1):
        placed_outer = box(70, 30 + index * 0.5, 80, 40 + index * 0.5)
        part = Part(
            id=index,
            number=number,
            polygon=placed_outer,
            outer_polygon=placed_outer,
            material_group="MA",
            packing_group="MAT-AB",
            area=100.0,
            source_outer_polygon=source_outer,
            placement_rot=0.0,
        )
        sheets.append(
            Sheet(
                index=index,
                width=100.0,
                height=50.0,
                thickness=20.0,
                parts=[part],
            )
        )
    return NestingResult(
        sheets=sheets,
        unit="metric",
        total_parts=2,
        total_sheets=2,
        total_part_area=200.0,
        total_sheet_area=10000.0,
    )


def _texture_result_rotated() -> NestingResult:
    source_outer = box(10, 10, 20, 15)
    placed_outer = box(50, 20, 55, 30)
    part = Part(
        id=1,
        number="MA-001",
        polygon=placed_outer,
        outer_polygon=placed_outer,
        material_group="MA",
        packing_group="MAT-AB",
        area=50.0,
        source_outer_polygon=source_outer,
        placement_rot=90.0,
    )
    sheet = Sheet(
        index=1,
        width=100.0,
        height=50.0,
        thickness=20.0,
        parts=[part],
    )
    return NestingResult(
        sheets=[sheet],
        unit="metric",
        total_parts=1,
        total_sheets=1,
        total_part_area=50.0,
        total_sheet_area=5000.0,
    )


def test_generate_textured_package_masks_by_source_geometry(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    numbered = _write_numbered_dxf(tmp_path)
    output_dir = tmp_path / "textured"

    package = generate_textured_package(
        _texture_result(),
        numbered,
        manifest,
        output_dir,
    )

    assert package.texture_count == 1
    texture_path = next((output_dir / "textures").glob("*.png"))
    with Image.open(texture_path) as texture:
        assert texture.mode == "RGBA"
        assert texture.size == (20, 20)
        assert texture.getpixel((8, 12))[3] == 0
        assert texture.getpixel((1, 1))[2] > 200
    assert package.textured_dxf.is_file()
    assert package.manifest_path.is_file()
    assert len(package.preview_paths) == 1

    doc = ezdxf.readfile(str(package.textured_dxf))
    images = list(doc.modelspace().query("IMAGE"))
    assert len(images) == 1
    assert images[0].dxf.layer == "TEXTURE_IMAGE"
    assert len(images[0].boundary_path) >= 4
    image_def = images[0].image_def
    assert image_def is not None
    image_path = Path(image_def.dxf.filename)
    assert image_path.is_absolute()
    assert image_path.is_file()


def test_texture_pattern_rejects_wrong_usable_dimensions(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["patterns"][0]["usable_width_mm"] = 120.0
    manifest.write_text(json.dumps(data), encoding="utf-8")
    numbered = _write_numbered_dxf(tmp_path)

    import pytest

    with pytest.raises(ValueError, match="does not match usable sheet area"):
        generate_textured_package(
            _texture_result(),
            numbered,
            manifest,
            tmp_path / "bad",
        )


def test_one_standard_pattern_image_is_reused_for_multiple_sheets(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    numbered = _write_numbered_dxf(tmp_path, ("MA-001", "MA-002"))
    output_dir = tmp_path / "reused"

    package = generate_textured_package(
        _texture_result_two_sheets(),
        numbered,
        manifest,
        output_dir,
    )

    assert package.texture_count == 2
    assert len(package.preview_paths) == 2
    manifest_data = json.loads(package.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest_data["patterns"]) == 1
    hashes = {
        item["texture_sha256"]
        for item in manifest_data["textures"]
    }
    assert len(hashes) == 1


def test_rotated_nested_part_is_restored_to_source_orientation(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    image = Image.new("RGB", (200, 100), (255, 0, 0))
    for x in range(100, 200):
        for y in range(50):
            image.putpixel((x, y), (0, 255, 0))
        for y in range(50, 100):
            image.putpixel((x, y), (255, 255, 0))
    image.save(tmp_path / "pattern.png")
    numbered = _write_numbered_dxf(tmp_path)
    output_dir = tmp_path / "rotated"

    package = generate_textured_package(
        _texture_result_rotated(),
        numbered,
        manifest,
        output_dir,
    )

    texture_path = next((output_dir / "textures").glob("*.png"))
    with Image.open(texture_path) as texture:
        assert texture.size == (20, 10)
        assert texture.getchannel("A").getbbox() == (0, 0, 20, 10)
        top_left = texture.getpixel((1, 1))
        top_right = texture.getpixel((18, 1))
        assert top_left[0] > 200 and top_left[1] > 200
        assert top_right[1] > 200 and top_right[0] < 60
    assert package.textured_dxf.is_file()


def test_max_pixels_per_mm_limits_texture_resolution(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["patterns"][0]["max_pixels_per_mm"] = 0.5
    manifest.write_text(json.dumps(data), encoding="utf-8")
    numbered = _write_numbered_dxf(tmp_path)
    output_dir = tmp_path / "limited"

    generate_textured_package(
        _texture_result(),
        numbered,
        manifest,
        output_dir,
    )

    texture_path = next((output_dir / "textures").glob("*.png"))
    with Image.open(texture_path) as texture:
        # The original part is 10 x 10 mm, so 0.5 px/mm gives 5 x 5.
        assert texture.size == (5, 5)


def test_relative_texture_dxf_can_be_relocated_after_move(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    numbered = _write_numbered_dxf(tmp_path)
    output_dir = tmp_path / "portable"
    package = generate_textured_package(
        _texture_result(),
        numbered,
        manifest,
        output_dir,
        image_reference_mode="relative",
    )
    doc = ezdxf.readfile(str(package.textured_dxf))
    image_def = next(iter(doc.objects.query("IMAGEDEF")))
    assert not Path(image_def.dxf.filename).is_absolute()

    moved_dir = tmp_path / "moved"
    output_dir.rename(moved_dir)
    moved_dxf = moved_dir / package.textured_dxf.name
    updated = relocate_texture_references(moved_dxf)

    assert updated == 1
    relocated_doc = ezdxf.readfile(str(moved_dxf))
    relocated_def = next(iter(relocated_doc.objects.query("IMAGEDEF")))
    relocated_path = Path(relocated_def.dxf.filename)
    assert relocated_path.is_absolute()
    assert relocated_path.parent == moved_dir / "textures"
    assert relocated_path.is_file()


def test_archive_contains_absolute_and_portable_dxf(tmp_path: Path):
    manifest = _write_pattern_manifest(tmp_path)
    numbered = _write_numbered_dxf(tmp_path)
    output_dir = tmp_path / "archived"

    package = generate_textured_package(
        _texture_result(),
        numbered,
        manifest,
        output_dir,
        create_archive=True,
    )

    assert package.portable_dxf is not None
    assert package.portable_dxf.is_file()
    assert package.archive_path is not None
    assert package.archive_path.is_file()
    import zipfile

    with zipfile.ZipFile(package.archive_path) as archive:
        names = set(archive.namelist())
        assert f"{output_dir.name}/{package.textured_dxf.name}" in names
        assert f"{output_dir.name}/{package.portable_dxf.name}" in names
        assert any(name.endswith("/texture_manifest.json") for name in names)
        assert any("/textures/" in name for name in names)


def test_existing_nested_dxf_can_be_rehydrated_with_source_geometry(
    tmp_path: Path,
):
    source = _write_numbered_dxf(tmp_path)
    nested = tmp_path / "nested.dxf"
    write_nested_dxf(
        _texture_result(),
        str(nested),
        board_size=(100.0, 50.0),
    )

    result = load_texture_nesting_result(nested, source)

    part = result.sheets[0].parts[0]
    assert part.number == "MA-001"
    assert part.source_outer_polygon is not None
    assert tuple(round(value, 6) for value in part.source_outer_polygon.bounds) == (
        20.0,
        20.0,
        30.0,
        30.0,
    )
    assert part.placement_rot == 0.0


def test_nesting_run_generates_texture_package(tmp_path: Path):
    source = tmp_path / "input.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUMBERS")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (20, 0), (20, 10), (0, 10)],
        close=True,
        dxfattribs={"layer": "PANEL"},
    )
    msp.add_text(
        "P-1",
        dxfattribs={"layer": "NUMBERS", "height": 2.0, "insert": (10, 5)},
    )
    doc.saveas(source)

    image_path = tmp_path / "pattern.png"
    Image.new("RGB", (100, 50), (180, 160, 140)).save(image_path)
    manifest = tmp_path / "patterns.json"
    manifest.write_text(
        json.dumps(
            {
                "patterns": [
                    {
                        "pattern_id": "plain",
                        "materials": ["*"],
                        "image_path": "pattern.png",
                        "usable_width_mm": 100.0,
                        "usable_height_mm": 50.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "output"
    profile = DrawingProfile(
        name="texture_cli_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUMBERS"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )
    outcome = app.run(
        str(source),
        100.0,
        50.0,
        20.0,
        UnitSystem.METRIC,
        1,
        0,
        0.0,
        False,
        None,
        None,
        drawing_profile=profile,
        profile=PROFILE_QUICK,
        confirm_sheet_count=False,
        output_root=output_root,
        auto_review=False,
        beam=False,
        texture_patterns=str(manifest),
    )

    assert outcome["texture_package"] is not None
    assert list(output_root.rglob("*_纹理原位.dxf"))
    assert list(output_root.rglob("textures/*.png"))
    assert list(output_root.rglob("previews/Sheet_*.png"))
