"""画像排序：指纹相似度是主信号，并列必须显式告警。

背景：`rank_profiles` 原本只看"图层名是否命中"，对"0 层 + 编号层 + 建层级"
这一类图纸给同一个分（CGR45 与 canada3l 同为 105 分），并列后按
`load_profiles` 的字典序取第一个 → 冷启动时 CGR45 被误配成 canada3l 画像。
"""

from pathlib import Path

import ezdxf
import pytest

import src.workflow as workflow
from src.drawing_profile import DrawingProfile, rank_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CGR45_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "dxf" / "CGR45final.dxf"


CGR45_LIKE_FINGERPRINT = {
    "dxf_version": "AC1032",
    "insunits": 4,
    "entity_counts": {
        "TEXT": 263,
        "LWPOLYLINE": 709,
        "DIMENSION": 1266,
        "LINE": 77,
        "CIRCLE": 153,
    },
    "layer_counts": {"0": 1725, "大样": 429, "尺寸标注": 111, "编号": 203},
    "linetype_counts": {"BYLAYER": 2039, "ZIGZAG": 383, "MOBIAN": 46},
    "has_hatch": False,
    "has_lwpolyline": True,
    "has_line": True,
    "has_circle": True,
}


CANADA3L_FINGERPRINT = {
    "dxf_version": "AC1032",
    "insunits": 4,
    "entity_counts": {
        "CIRCLE": 72,
        "DIMENSION": 690,
        "INSERT": 8,
        "LINE": 128,
        "LWPOLYLINE": 365,
        "MTEXT": 32,
        "TEXT": 166,
    },
    "layer_counts": {
        "0": 420,
        "大样": 191,
        "标注": 690,
        "框": 8,
        "编号": 96,
        "说明": 56,
    },
    "linetype_counts": {
        "ACAD_ISO02W100": 16,
        "BYLAYER": 1238,
        "MB": 30,
        "ZIGZAG": 177,
    },
    "has_hatch": False,
    "has_lwpolyline": True,
    "has_line": True,
    "has_circle": True,
}


def _profile(name: str, fingerprint=None) -> DrawingProfile:
    return DrawingProfile(
        name=name,
        version="1.0.0",
        panel_layer="0",
        panel_layers=["0"],
        number_layers=["编号"],
        use_hatch=False,
        build_hierarchy=True,
        exclude_entity_types=[],
        exclude_linetypes=["ZIGZAG", "DASH", "PHANTOM"],
        fingerprint=fingerprint or {},
    )


def test_structural_rules_alone_cannot_separate_same_family_profiles():
    """先固化缺陷前提：仅靠图层规则时两张画像同分。"""
    ranked = rank_profiles(
        CGR45_LIKE_FINGERPRINT,
        [_profile("canada3l"), _profile("cgr45")],
    )

    assert ranked[0][1] == ranked[1][1]


def test_fingerprint_similarity_breaks_the_tie():
    ranked = rank_profiles(
        CGR45_LIKE_FINGERPRINT,
        [
            _profile("canada3l"),
            _profile("cgr45", fingerprint=CGR45_LIKE_FINGERPRINT),
        ],
    )

    assert ranked[0][0].name == "cgr45"
    assert ranked[0][1] > ranked[1][1]


def test_cgr45_profile_declares_validated_fingerprint():
    from src.drawing_profile import load_profile

    profile = load_profile(PROJECT_ROOT / "drawing_profiles" / "cgr45.json")

    assert profile.fingerprint.get("layer_counts")
    assert "path" not in profile.fingerprint, "不得把本地绝对路径写入画像"


def test_canada3l_profile_declares_validated_fingerprint():
    from src.drawing_profile import load_profile

    profile = load_profile(
        PROJECT_ROOT / "drawing_profiles" / "canada3l.json"
    )

    assert profile.fingerprint == CANADA3L_FINGERPRINT
    assert "path" not in profile.fingerprint


def test_canada3l_fingerprint_wins_over_cgr45_like_profile():
    from src.drawing_profile import load_profile

    canada3l = load_profile(
        PROJECT_ROOT / "drawing_profiles" / "canada3l.json"
    )
    cgr45 = load_profile(PROJECT_ROOT / "drawing_profiles" / "cgr45.json")

    ranked = rank_profiles(CANADA3L_FINGERPRINT, [cgr45, canada3l])

    assert ranked[0][0].name == "canada3l"
    assert ranked[0][1] > ranked[1][1]


@pytest.mark.skipif(
    not CGR45_FIXTURE.exists(),
    reason="需要 tests/fixtures/dxf/CGR45final.dxf 金标准样本",
)
def test_cold_start_ranking_matches_cgr45_fixture():
    profile = workflow.resolve_drawing_profile(
        str(CGR45_FIXTURE), use_case_library=False
    )

    assert profile is not None
    assert profile.name == "cgr45"


COUNTERTOP2_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "dxf" / "countertop2.dxf"
)


def test_countertop2_profile_declares_matching_fingerprint():
    from src.drawing_profile import fingerprint_similarity, load_profile

    profile = load_profile(PROJECT_ROOT / "drawing_profiles" / "countertop2.json")

    assert profile.fingerprint, "countertop2 画像应带已验证指纹"
    # 与 cgr45 的关键结构差异是"无 CIRCLE 孔洞"，相似度必须低于命中阈值，
    # 否则 cgr45 画像会抢走 countertop2 的图纸。
    cgr45 = load_profile(PROJECT_ROOT / "drawing_profiles" / "cgr45.json")
    assert (
        fingerprint_similarity(profile.fingerprint, cgr45.fingerprint) < 60.0
    )


@pytest.mark.skipif(
    not COUNTERTOP2_FIXTURE.exists(),
    reason="需要 tests/fixtures/dxf/countertop2.dxf 金标准样本",
)
def test_cold_start_ranking_matches_countertop2_fixture():
    profile = workflow.resolve_drawing_profile(
        str(COUNTERTOP2_FIXTURE), use_case_library=False
    )

    assert profile is not None
    assert profile.name == "countertop2"


def test_resolve_drawing_profile_warns_on_tie(tmp_path, monkeypatch, capsys):
    dxf_path = tmp_path / "tie.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("编号")  # 图层 0 默认已存在
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        close=True,
        dxfattribs={"layer": "0"},
    )
    # 编号层必须实际出现，否则两张画像都会因 number_layers 未命中而直接 0 分，
    # 无法构造"规则同分"的并列场景。
    text = msp.add_text("203-A", dxfattribs={"layer": "编号"})
    text.set_placement((50, 50))
    doc.saveas(dxf_path)

    tied = [_profile("alpha"), _profile("beta")]
    monkeypatch.setattr(workflow, "load_profiles", lambda directory: tied)
    monkeypatch.setattr(workflow, "load_cases", lambda directory: [])

    profile = workflow.resolve_drawing_profile(
        str(dxf_path), use_case_library=False
    )

    captured = capsys.readouterr().out
    assert "画像匹配并列" in captured
    assert profile is not None  # 仍然返回一个结果，但歧义已被显式暴露
