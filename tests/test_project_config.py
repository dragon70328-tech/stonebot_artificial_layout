import json
from pathlib import Path
from types import SimpleNamespace

import main as app
from src.project_config import ProjectConfig
from src.project_config import QUALITY_PRESETS, apply_quality_to_optimizer


def _sample_config() -> ProjectConfig:
    return ProjectConfig.from_dict({
        "project_id": "test-job",
        "sheet": {
            "width": 3200.0,
            "height": 1600.0,
            "thickness": 20.0,
            "special_width": 3225.0,
            "special_height": 1625.0,
        },
        "profile": {
            "rotation": [0, 90],
            "arbitrary_rotation": False,
            "min_gap": 5.0,
            "group_mode": None,
            "slide_to_edge": True,
            "align_edges": True,
            "first_part_left_edge": False,
            "processing_class": "bridge",
        },
        "optimizer": {
            "trials": 2,
            "seed": 7,
            "budget_seconds": 30.0,
            "quick": False,
            "pairing": False,
        },
        "reader": {
            "skip_unnumbered": True,
            "layers": ["ST-01"],
            "exclude_layers": ["DIM"],
        },
    })


def test_project_config_loads_nested_json():
    config = _sample_config()
    assert config.project_id == "test-job"
    assert config.sheet.special_size == (3225.0, 1625.0)
    assert config.profile.min_gap == 5.0
    assert config.optimizer.seed == 7
    assert config.reader.layers == ["ST-01"]


def test_project_config_to_nesting_profile():
    profile = _sample_config().to_nesting_profile()
    assert profile.rotation == [0, 90]
    assert profile.min_gap == 5.0
    assert profile.slide_to_edge is True
    assert profile.processing_class == "bridge"


def test_project_config_reader_options_are_externalized():
    config = _sample_config()
    options = config.reader.to_read_options()
    assert options["exclude_linetypes"] is None
    assert options["number_layer_keyword"] == "编"
    assert options["room_label_keyword"] == "户型"
    assert options["room_label_exclude_keyword"] == "套"
    assert options["room_max_distance"] == 5000.0


def test_quality_preset_fast_updates_optimizer():
    config = _sample_config()
    apply_quality_to_optimizer(config.optimizer, "fast")
    assert config.optimizer.quality == "fast"
    assert config.optimizer.trials == QUALITY_PRESETS["fast"]["trials"]
    assert config.optimizer.budget_seconds == QUALITY_PRESETS["fast"]["budget_seconds"]
    assert config.optimizer.quick is True


def test_project_config_quality_preset_controls_defaults():
    config = ProjectConfig.from_dict({
        "sheet": {"width": 3000.0, "height": 1400.0},
        "profile": {},
        "optimizer": {"quality": "balanced"},
    })
    assert config.optimizer.budget_seconds == 60.0
    assert config.optimizer.quick is False


def test_project_config_from_file(tmp_path: Path):
    path = tmp_path / "project.json"
    path.write_text(json.dumps({
        "sheet": {"width": 3000.0, "height": 1400.0},
        "profile": {"min_gap": 10.0},
    }), encoding="utf-8")
    config = ProjectConfig.from_file(path)
    assert config.sheet.width == 3000.0
    assert config.profile.min_gap == 10.0


def test_cli_overrides_apply_to_project_config():
    config = _sample_config()
    args = SimpleNamespace(
        thickness=None,
        special_size="3300x1650",
        process="waterjet",
        rotation="0",
        free_rotation=False,
        no_rotation=True,
        min_gap=12.0,
        group="one_set_per_sheet",
        no_slide=True,
        no_align=True,
        trials=1,
        seed=0,
        budget=180.0,
        quick=False,
        pairing=True,
        include_unnumbered=True,
        layers="ST-02",
        exclude_layers="DIM,PHANTOM",
    )
    app.apply_cli_overrides_to_project_config(config, args)

    assert config.sheet.special_size == (3300.0, 1650.0)
    assert config.profile.processing_class == "waterjet_laser"
    assert config.profile.rotation == [0]
    assert config.profile.min_gap == 12.0
    assert config.profile.group_mode == "one_set_per_sheet"
    assert config.profile.slide_to_edge is False
    assert config.profile.align_edges is False
    assert config.optimizer.pairing is True
    assert config.reader.skip_unnumbered is False
    assert config.reader.layers == ["ST-02"]
    assert config.reader.exclude_layers == ["DIM", "PHANTOM"]
