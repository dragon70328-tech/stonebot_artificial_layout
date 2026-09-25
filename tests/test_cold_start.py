"""冷启动与模板开关的行为测试。

背景（2026-09-23）：`output/` 下的历史运行目录被清理后，暴露了两件事：

1. `scripts/check_output_cases.py` 的回归输入依赖 `output/`，不可复现；
2. 运行时默认会命中「已验证模板」直接复刻历史布局，跳过排板算法。

这里锁定修复后的契约：默认只用算法排板；模板与已验证案例必须显式开启。
"""

from pathlib import Path

import ezdxf
import pytest

import main as app
import src.workflow as workflow
from src.drawing_profile import DrawingProfile
from src.units import UnitSystem


def _make_dxf(path: Path) -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("PANEL")
    doc.layers.add("NUM")
    msp = doc.modelspace()
    for index, x in enumerate((0, 500), start=1):
        msp.add_lwpolyline(
            [(x, 0), (x + 400, 0), (x + 400, 300), (x, 300), (x, 0)],
            dxfattribs={"layer": "PANEL"},
        )
        text = msp.add_text(f"01B-{index}", dxfattribs={"layer": "NUM"})
        text.set_placement((x + 200, 150))
    doc.saveas(path)


def _profile() -> DrawingProfile:
    return DrawingProfile(
        name="cold_start_test",
        version="1.0.0",
        panel_layer="PANEL",
        use_hatch=False,
        number_layers=["NUM"],
        build_hierarchy=False,
        exclude_entity_types=[],
        exclude_linetypes=[],
    )


def _run(dxf_path, **overrides):
    kwargs = dict(
        unit=UnitSystem.METRIC,
        trials=1,
        seed=0,
        budget=0,
        skip_unnumbered=False,
        layers=None,
        exclude_layers=None,
        drawing_profile=_profile(),
        profile=app.PROFILES["quick"],
        confirm_sheet_count=False,
        report_only=True,
        quick=True,
    )
    kwargs.update(overrides)
    return app.run(str(dxf_path), 1000, 800, 20, **kwargs)


def test_default_layout_source_is_algorithm_and_template_is_not_touched(
    tmp_path, monkeypatch
):
    dxf_path = tmp_path / "cold_default.dxf"
    _make_dxf(dxf_path)
    calls = []
    monkeypatch.setattr(
        app,
        "match_template",
        lambda *a, **k: calls.append((a, k)) or None,
    )

    outcome = _run(dxf_path)

    assert calls == [], "默认不得尝试模板匹配"
    assert outcome["layout_source"] == "algorithm"


def test_use_template_opt_in_enables_template_matching(tmp_path, monkeypatch):
    dxf_path = tmp_path / "cold_optin.dxf"
    _make_dxf(dxf_path)
    calls = []
    monkeypatch.setattr(
        app,
        "match_template",
        lambda *a, **k: calls.append((a, k)) or None,
    )

    outcome = _run(dxf_path, use_template=True)

    assert len(calls) == 1, "显式 --use-template 时才允许匹配模板"
    assert outcome["layout_source"] == "algorithm"  # 未命中模板仍走算法


def test_cold_start_forces_algorithm_even_with_use_template(
    tmp_path, monkeypatch
):
    dxf_path = tmp_path / "cold_start.dxf"
    _make_dxf(dxf_path)
    calls = []
    monkeypatch.setattr(
        app,
        "match_template",
        lambda *a, **k: calls.append((a, k)) or None,
    )

    outcome = _run(dxf_path, use_template=True, cold_start=True)

    assert calls == [], "冷启动必须忽略模板，即使同时传了 use_template"
    assert outcome["layout_source"] == "algorithm"


def test_resolve_drawing_profile_skips_validated_case_library(tmp_path, monkeypatch):
    dxf_path = tmp_path / "no_cases.dxf"
    _make_dxf(dxf_path)

    def _explode(*args, **kwargs):
        raise AssertionError("冷启动不得读取 validated_cases")

    monkeypatch.setattr(workflow, "load_cases", _explode)
    monkeypatch.setattr(workflow, "match_case", _explode)

    # 不应抛错：完全不进入案例库分支
    workflow.resolve_drawing_profile(str(dxf_path), use_case_library=False)


def test_audit_cold_start_skips_cache(tmp_path, monkeypatch):
    import src.cli_audit as audit_cli
    from src.drawing_profile import DrawingIssue

    source = tmp_path / "audit_cold.dxf"
    source.write_bytes(b"DXF-PLACEHOLDER")

    from types import SimpleNamespace

    monkeypatch.setattr(
        audit_cli, "resolve_drawing_profile", lambda path: SimpleNamespace(name="p")
    )
    monkeypatch.setattr(
        audit_cli, "audit_drawing", lambda path, prof: []
    )
    monkeypatch.setattr(audit_cli, "make_output_dir", lambda path: tmp_path)
    monkeypatch.setattr(
        audit_cli, "write_issue_evidence_svg", lambda path, issues, out: []
    )
    monkeypatch.setattr(
        audit_cli, "write_dxf_overview_svg", lambda source, output, issues=None: Path(output)
    )
    monkeypatch.setattr(
        audit_cli, "write_audit_dxf", lambda source, issues, output: Path(output)
    )

    def _explode(*args, **kwargs):
        raise AssertionError("冷启动不得读写审图缓存")

    monkeypatch.setattr(audit_cli, "load_cached_issues", _explode)
    monkeypatch.setattr(audit_cli, "save_cached_issues", _explode)

    audit_cli.run_audit(str(source), use_cache=False)
