from pathlib import Path

import pytest
from PIL import Image

from src.slab_pattern_library import (
    list_pattern_assets,
    register_pattern_asset,
    validate_pattern_library,
)
from src.slab_texture import load_texture_patterns


def _image(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (200, 100), color).save(path)


def test_register_and_reuse_pattern_library(tmp_path: Path):
    source = tmp_path / "pattern.png"
    _image(source, (180, 160, 140))
    library = tmp_path / "slab_patterns"

    library_path = register_pattern_asset(
        library,
        pattern_id="artificial-a",
        image_path=source,
        materials=["MAT-A", "MAT-B"],
        usable_width_mm=3400,
        usable_height_mm=1400,
        max_pixels_per_mm=0.5,
    )

    assert library_path.is_file()
    assets = list_pattern_assets(library)
    assert assets[0]["pattern_id"] == "artificial-a"
    assert assets[0]["image_path"] == "images/artificial-a.png"
    assert validate_pattern_library(library) == 1
    patterns = load_texture_patterns(library_path)
    assert patterns[0].materials == ("MAT-A", "MAT-B")
    assert patterns[0].image_path.is_file()


def test_pattern_library_rejects_material_ambiguity(tmp_path: Path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _image(first, (1, 2, 3))
    _image(second, (4, 5, 6))
    library = tmp_path / "slab_patterns"
    register_pattern_asset(
        library,
        pattern_id="first",
        image_path=first,
        materials=["MAT-A"],
        usable_width_mm=100,
        usable_height_mm=50,
    )

    with pytest.raises(ValueError, match="maps to multiple texture patterns"):
        register_pattern_asset(
            library,
            pattern_id="second",
            image_path=second,
            materials=["MAT-A"],
            usable_width_mm=100,
            usable_height_mm=50,
        )
