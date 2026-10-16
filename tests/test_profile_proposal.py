import json
import re

import ezdxf
import pytest

from src.profile_proposal import (
    induce_label_pattern,
    propose_profile,
    register_profile,
)


def _rect(x0, y0, size=1000.0):
    return [
        (x0, y0),
        (x0 + size, y0),
        (x0 + size, y0 + size),
        (x0, y0 + size),
        (x0, y0),
    ]


def _write_drawing(path, *, rects=5, zigzag=True, numbers=True):
    doc = ezdxf.new("R2010")
    doc.layers.add("编号")
    doc.layers.add("大样")
    doc.linetypes.add("ZIGZAG", pattern="A,1.0,-0.5,0.0,-0.5")
    msp = doc.modelspace()
    for index in range(rects):
        x0 = index * 2000.0
        msp.add_lwpolyline(_rect(x0, 0.0), dxfattribs={"layer": "0"})
        if numbers:
            text = msp.add_text(
                f"{101 + index}-A",
                dxfattribs={"layer": "编号", "height": 40.0},
            )
            text.set_placement((x0 + 500.0, 500.0))
    if zigzag:
        msp.add_lwpolyline(
            _rect(0.0, 5000.0),
            dxfattribs={"layer": "大样", "linetype": "ZIGZAG"},
        )
    doc.saveas(path)
    return path


# ---------------------------------------------------------------------------
# 编号正则归纳
# ---------------------------------------------------------------------------


def test_induce_label_pattern_material_shape():
    result = induce_label_pattern(["101-A", "102-B", "523-C"])
    assert result["coverage"] == 1.0
    assert "(?P<material>" in result["pattern"]
    assert "(?P<shape>" in result["pattern"]
    compiled = re.compile(result["pattern"])
    match = compiled.match("523-C")
    assert match.group("material") == "523"
    assert match.group("shape") == "C"
    assert not compiled.match("523-CC")


def test_induce_label_pattern_digits_only():
    result = induce_label_pattern(["1001-01", "1002-02", "3525-12"])
    assert result["coverage"] == 1.0
    # 纯数字+分隔符：不需要命名分组
    assert "?P<" not in result["pattern"]
    compiled = re.compile(result["pattern"])
    assert compiled.match("1001-01")
    assert not compiled.match("1001-1")


def test_induce_label_pattern_variable_length():
    result = induce_label_pattern(["P-12", "P-123", "P-99"])
    assert result["coverage"] == 1.0
    compiled = re.compile(result["pattern"])
    assert compiled.match("P-1") is None
    assert compiled.match("P-12")
    assert compiled.match("P-123")
    assert compiled.match("P-1234") is None


def test_induce_label_pattern_reports_uncovered():
    result = induce_label_pattern(
        ["101-A", "102-A", "103-A", "104-A", "105-A", "备注1"]
    )
    assert result["covered"] == 5
    assert "备注1" in result["uncovered_samples"]


# ---------------------------------------------------------------------------
# 提议主流程
# ---------------------------------------------------------------------------


def test_propose_profile_basic(tmp_path):
    path = _write_drawing(tmp_path / "sample.dxf", rects=5)
    proposal = propose_profile(path)

    draft = proposal.draft
    assert draft["name"] == "sample"
    assert draft["panel_layer"] == "0"
    assert draft["number_layers"] == ["编号"]
    assert draft["build_hierarchy"] is False
    assert "ZIGZAG" in draft["exclude_linetypes"]
    assert draft["expected_counts"] == {"panels": 5, "holes": 0, "numbers": 5}

    assert proposal.validation["panels"] == 5
    assert proposal.validation["numbers"] == 5
    assert proposal.questions

    # 证据里能看到被排除线型的实体数
    assert proposal.evidence["linetype_counts"].get("ZIGZAG") == 1


def test_propose_profile_recovers_near_closed_polylines(tmp_path):
    path = _write_drawing(tmp_path / "gaps.dxf", rects=3, zigzag=False)
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    # 水星式"视觉闭合但 closed=False"：末点落在首点附近（缝隙 ~0.036）
    for index in range(2):
        x0 = 10000.0 + index * 2000.0
        msp.add_lwpolyline(
            [
                (x0, 0.0),
                (x0 + 1000.0, 0.0),
                (x0 + 1000.0, 1000.0),
                (x0, 1000.0),
                (x0 + 0.03, 0.02),
            ],
            dxfattribs={"layer": "0"},
        )
    doc.saveas(path)

    proposal = propose_profile(path)
    assert proposal.draft["closed_tolerance"] >= 0.03
    # 3 条显式闭合 + 2 条容差恢复
    assert proposal.validation["panels"] == 5


def test_propose_profile_suggests_small_area_threshold(tmp_path):
    path = _write_drawing(tmp_path / "areas.dxf", rects=6, zigzag=False,
                          numbers=False)
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    # 6 个 100x100mm 小件（面积 1e4，与大件 1e6 形成 100 倍断层）
    for index in range(6):
        msp.add_lwpolyline(
            _rect(index * 300.0, 5000.0, size=100.0),
            dxfattribs={"layer": "0"},
        )
    doc.saveas(path)

    proposal = propose_profile(path)
    threshold = proposal.draft["small_area_threshold"]
    assert 1e4 < threshold < 1e6
    assert proposal.evidence["area_distribution"]["below_count"] == 6


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


def _make_draft(tmp_path, rects=5):
    path = _write_drawing(tmp_path / "reg.dxf", rects=rects)
    proposal = propose_profile(path)
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(
        json.dumps(proposal.draft, ensure_ascii=False), encoding="utf-8"
    )
    return path, draft_path


def test_register_profile_writes_profile_and_case(tmp_path):
    dxf_path, draft_path = _make_draft(tmp_path)
    root = tmp_path / "profiles"

    result = register_profile(dxf_path, draft_path, profiles_root=root)

    profile = json.loads(
        result["profile_path"].read_text(encoding="utf-8")
    )
    assert profile["name"] == "reg"
    assert profile["expected_counts"] == {"panels": 5, "holes": 0, "numbers": 5}
    assert profile["fingerprint"]["entity_counts"]["LWPOLYLINE"] >= 5

    case = json.loads(result["case_path"].read_text(encoding="utf-8"))
    assert case["case_id"] == "reg"
    assert case["profile_name"] == "reg"
    assert len(case["source_sha256"]) == 64
    assert case["expected_counts"]["panels"] == 5

    # 同名再登记：默认拒绝，force 覆盖
    with pytest.raises(FileExistsError):
        register_profile(dxf_path, draft_path, profiles_root=root)
    again = register_profile(dxf_path, draft_path, profiles_root=root,
                             force=True)
    assert again["profile_path"] == result["profile_path"]


def test_register_profile_rejects_count_mismatch(tmp_path):
    dxf_path, draft_path = _make_draft(tmp_path)
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["expected_counts"]["panels"] = 999
    draft_path.write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8"
    )
    root = tmp_path / "profiles"

    with pytest.raises(ValueError, match="expected_counts"):
        register_profile(dxf_path, draft_path, profiles_root=root)

    result = register_profile(dxf_path, draft_path, profiles_root=root,
                              force=True)
    assert result["mismatches"]["panels"] == {"expected": 999, "actual": 5}
    profile = json.loads(
        result["profile_path"].read_text(encoding="utf-8")
    )
    assert "登记警告" in profile["notes"]
    # 复算结果覆盖草稿里的错误数字
    assert profile["expected_counts"]["panels"] == 5
