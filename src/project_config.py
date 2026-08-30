"""Project-level configuration for one nesting job.

The model intentionally mirrors the YAML/JSON shape described in
``docs/constraint-externalization.md`` so the CLI can later migrate from many
flags to a single project file without changing the underlying engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.constraints import NestingProfile


@dataclass
class SheetConfig:
    width: float
    height: float
    thickness: float = 20.0
    special_width: float | None = None
    special_height: float | None = None

    @property
    def special_size(self) -> tuple[float, float] | None:
        if self.special_width is None or self.special_height is None:
            return None
        return (self.special_width, self.special_height)


@dataclass
class ProfileConfig:
    rotation: list[int] = field(default_factory=lambda: [0, 90, 180, 270])
    arbitrary_rotation: bool = False
    min_gap: float = 0.0
    group_mode: str | None = None
    slide_to_edge: bool = True
    align_edges: bool = True
    first_part_left_edge: bool = False
    sheet_thickness: float = 20.0
    edge_margin: float = 0.0
    processing_class: str = "bridge"

    def to_nesting_profile(self) -> NestingProfile:
        return NestingProfile(
            rotation=self.rotation,
            arbitrary_rotation=self.arbitrary_rotation,
            min_gap=self.min_gap,
            group_mode=self.group_mode,
            slide_to_edge=self.slide_to_edge,
            align_edges=self.align_edges,
            first_part_left_edge=self.first_part_left_edge,
            sheet_thickness=self.sheet_thickness,
            edge_margin=self.edge_margin,
            processing_class=self.processing_class,
        )


@dataclass
class OptimizerConfig:
    trials: int = 1
    seed: int = 0
    budget_seconds: float = 180.0
    quick: bool = False
    pairing: bool = False
    quality: str = "best"


QUALITY_PRESETS: dict[str, dict] = {
    "fast": {"trials": 1, "budget_seconds": 0.0, "quick": True},
    "balanced": {"trials": 1, "budget_seconds": 60.0, "quick": False},
    "best": {"trials": 1, "budget_seconds": 180.0, "quick": False},
}


def apply_quality_to_optimizer(optimizer: OptimizerConfig, quality: str) -> None:
    """Apply a named quality preset without overwriting explicit overrides."""
    preset = QUALITY_PRESETS.get(quality)
    if preset is None:
        return
    optimizer.quality = quality
    optimizer.trials = preset["trials"]
    optimizer.budget_seconds = preset["budget_seconds"]
    optimizer.quick = preset["quick"]


@dataclass
class ReaderConfig:
    skip_unnumbered: bool = True
    layers: list[str] | None = None
    exclude_layers: list[str] | None = None
    exclude_linetypes: list[str] | None = None
    number_layer_keyword: str = "编"
    room_label_keyword: str = "户型"
    room_label_exclude_keyword: str = "套"
    room_label_normalizations: dict[str, str] = field(
        default_factory=lambda: {"^B7a": "B7-a"}
    )
    room_max_distance: float = 5000.0

    def to_read_options(self) -> dict:
        """Options consumed by ``dxf_reader.read_dxf`` for no-profile intake."""
        return {
            "exclude_linetypes": self.exclude_linetypes,
            "number_layer_keyword": self.number_layer_keyword,
            "room_label_keyword": self.room_label_keyword,
            "room_label_exclude_keyword": self.room_label_exclude_keyword,
            "room_label_normalizations": self.room_label_normalizations,
            "room_max_distance": self.room_max_distance,
        }


@dataclass
class ProjectConfig:
    sheet: SheetConfig
    profile: ProfileConfig
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    project_id: str = ""

    def to_nesting_profile(self) -> NestingProfile:
        return self.profile.to_nesting_profile()

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        if "sheet" not in data:
            raise ValueError("ProjectConfig 缺少 sheet 配置")
        if "profile" not in data:
            raise ValueError("ProjectConfig 缺少 profile 配置")

        sheet_data = data["sheet"]
        profile_data = data["profile"]
        optimizer_data = data.get("optimizer", {})
        reader_data = data.get("reader", {})

        return cls(
            project_id=data.get("project_id", ""),
            sheet=SheetConfig(
                width=float(sheet_data["width"]),
                height=float(sheet_data["height"]),
                thickness=float(sheet_data.get("thickness", 20.0)),
                special_width=(
                    float(sheet_data["special_width"])
                    if sheet_data.get("special_width") is not None
                    else None
                ),
                special_height=(
                    float(sheet_data["special_height"])
                    if sheet_data.get("special_height") is not None
                    else None
                ),
            ),
            profile=ProfileConfig(
                rotation=list(profile_data.get("rotation", [0, 90, 180, 270])),
                arbitrary_rotation=bool(
                    profile_data.get("arbitrary_rotation", False)
                ),
                min_gap=float(profile_data.get("min_gap", 0.0)),
                group_mode=profile_data.get("group_mode"),
                slide_to_edge=bool(profile_data.get("slide_to_edge", True)),
                align_edges=bool(profile_data.get("align_edges", True)),
                first_part_left_edge=bool(
                    profile_data.get("first_part_left_edge", False)
                ),
                sheet_thickness=float(
                    profile_data.get("sheet_thickness", sheet_data.get("thickness", 20.0))
                ),
                edge_margin=float(profile_data.get("edge_margin", 0.0)),
                processing_class=str(
                    profile_data.get("processing_class", "bridge")
                ),
            ),
            optimizer=OptimizerConfig(
                trials=int(
                    optimizer_data.get(
                        "trials",
                        QUALITY_PRESETS.get(
                            optimizer_data.get("quality", "best"),
                            QUALITY_PRESETS["best"],
                        )["trials"],
                    )
                ),
                seed=int(optimizer_data.get("seed", 0)),
                budget_seconds=float(
                    optimizer_data.get(
                        "budget_seconds",
                        QUALITY_PRESETS.get(
                            optimizer_data.get("quality", "best"),
                            QUALITY_PRESETS["best"],
                        )["budget_seconds"],
                    )
                ),
                quick=bool(
                    optimizer_data.get(
                        "quick",
                        QUALITY_PRESETS.get(
                            optimizer_data.get("quality", "best"),
                            QUALITY_PRESETS["best"],
                        )["quick"],
                    )
                ),
                pairing=bool(optimizer_data.get("pairing", False)),
                quality=str(optimizer_data.get("quality", "best")),
            ),
            reader=ReaderConfig(
                skip_unnumbered=bool(reader_data.get("skip_unnumbered", True)),
                layers=(
                    list(reader_data["layers"])
                    if reader_data.get("layers") is not None
                    else None
                ),
                exclude_layers=(
                    list(reader_data["exclude_layers"])
                    if reader_data.get("exclude_layers") is not None
                    else None
                ),
                exclude_linetypes=(
                    list(reader_data["exclude_linetypes"])
                    if reader_data.get("exclude_linetypes") is not None
                    else None
                ),
                number_layer_keyword=str(
                    reader_data.get("number_layer_keyword", "编")
                ),
                room_label_keyword=str(
                    reader_data.get("room_label_keyword", "户型")
                ),
                room_label_exclude_keyword=str(
                    reader_data.get("room_label_exclude_keyword", "套")
                ),
                room_label_normalizations=(
                    dict(reader_data["room_label_normalizations"])
                    if reader_data.get("room_label_normalizations") is not None
                    else {"^B7a": "B7-a"}
                ),
                room_max_distance=float(
                    reader_data.get("room_max_distance", 5000.0)
                ),
            ),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "ProjectConfig":
        path = Path(path)
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ProjectConfig JSON 解析失败：{path}") from exc
        return cls.from_dict(data)
